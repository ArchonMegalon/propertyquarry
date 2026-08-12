from __future__ import annotations

import copy
from datetime import datetime
import time
from typing import Any

from app.domain.models import now_utc_iso


def _to_iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value or "")


def _normalize_float(value: object, *, default: float = 0.5) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return max(0.0, min(1.0, out))


class PostgresPreferenceProfileRepository:
    def __init__(self, database_url: str) -> None:
        self._database_url = str(database_url or "").strip()
        if not self._database_url:
            raise ValueError("database_url is required for PostgresPreferenceProfileRepository")
        self._ensure_schema()

    def _connect(self):  # type: ignore[no-untyped-def]
        try:
            import psycopg
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("psycopg is required for postgres preference-profile backend") from exc
        return psycopg.connect(self._database_url, autocommit=True)

    def _json_value(self, value: object):  # type: ignore[no-untyped-def]
        from psycopg.types.json import Json

        return Json(copy.deepcopy(value))

    def _ensure_schema(self) -> None:
        from app.repositories.postgres_schema import repository_schema_ddl_enabled

        if not repository_schema_ddl_enabled():
            return
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS person_profiles (
                        principal_id TEXT NOT NULL,
                        person_id TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        profile_scope TEXT NOT NULL DEFAULT 'personal',
                        consent_mode TEXT NOT NULL DEFAULT 'explicit_only',
                        learning_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        high_stakes_domains_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute("ALTER TABLE person_profiles DROP CONSTRAINT IF EXISTS person_profiles_pkey")
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_person_profiles_principal_person
                    ON person_profiles(principal_id, person_id)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS preference_nodes (
                        principal_id TEXT NOT NULL,
                        person_id TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        category TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        strength TEXT NOT NULL DEFAULT 'medium',
                        confidence DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                        source_mode TEXT NOT NULL DEFAULT 'explicit',
                        status TEXT NOT NULL DEFAULT 'active',
                        decay_policy TEXT NOT NULL DEFAULT 'reinforce_only',
                        last_confirmed_at TEXT NOT NULL DEFAULT '',
                        last_observed_at TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMPTZ NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute("ALTER TABLE preference_nodes DROP CONSTRAINT IF EXISTS preference_nodes_pkey")
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_preference_nodes_principal_person_node
                    ON preference_nodes(principal_id, person_id, node_id)
                    """
                )
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_preference_nodes_identity
                    ON preference_nodes(principal_id, person_id, domain, category, key)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS preference_evidence_events (
                        principal_id TEXT NOT NULL,
                        person_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        object_type TEXT NOT NULL,
                        object_id TEXT NOT NULL,
                        source_ref TEXT NOT NULL DEFAULT '',
                        raw_signal_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        interpreted_signal_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        signal_strength DOUBLE PRECISION NOT NULL DEFAULT 0.5,
                        reversible BOOLEAN NOT NULL DEFAULT TRUE,
                        recorded_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute("ALTER TABLE preference_evidence_events DROP CONSTRAINT IF EXISTS preference_evidence_events_pkey")
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_preference_evidence_events_principal_person_event
                    ON preference_evidence_events(principal_id, person_id, event_id)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS preference_decision_assessments (
                        principal_id TEXT NOT NULL,
                        person_id TEXT NOT NULL,
                        assessment_id TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        object_type TEXT NOT NULL,
                        object_id TEXT NOT NULL,
                        fit_score DOUBLE PRECISION NOT NULL,
                        confidence DOUBLE PRECISION NOT NULL,
                        predicted_reaction TEXT NOT NULL,
                        recommendation TEXT NOT NULL,
                        match_reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                        mismatch_reasons_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                        unknowns_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                        blocking_constraints_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                        assessment_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        generated_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute("ALTER TABLE preference_decision_assessments DROP CONSTRAINT IF EXISTS preference_decision_assessments_pkey")
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_preference_decision_assessments_principal_person_assessment
                    ON preference_decision_assessments(principal_id, person_id, assessment_id)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS preference_profile_corrections (
                        principal_id TEXT NOT NULL,
                        person_id TEXT NOT NULL,
                        correction_id TEXT NOT NULL,
                        target_type TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        old_value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        new_value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        reason TEXT NOT NULL DEFAULT '',
                        corrected_by TEXT NOT NULL DEFAULT '',
                        corrected_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cur.execute("ALTER TABLE preference_profile_corrections DROP CONSTRAINT IF EXISTS preference_profile_corrections_pkey")
                cur.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_preference_profile_corrections_principal_person_correction
                    ON preference_profile_corrections(principal_id, person_id, correction_id)
                    """
                )

    def ensure_person_profile(
        self,
        *,
        principal_id: str,
        person_id: str,
        display_name: str | None = None,
        profile_scope: str | None = None,
        consent_mode: str | None = None,
        learning_enabled: bool | None = None,
        high_stakes_domains_enabled: bool | None = None,
    ) -> dict[str, object]:
        principal = str(principal_id or "").strip()
        person = str(person_id or "").strip() or "self"
        now = now_utc_iso()
        existing = self.get_person_profile(principal_id=principal, person_id=person) or {}
        resolved_display_name = str(display_name if display_name is not None else existing.get("display_name") or person).strip() or person
        resolved_profile_scope = str(profile_scope if profile_scope is not None else existing.get("profile_scope") or "personal").strip().lower() or "personal"
        resolved_consent_mode = str(consent_mode if consent_mode is not None else existing.get("consent_mode") or "explicit_only").strip().lower() or "explicit_only"
        resolved_learning_enabled = bool(existing.get("learning_enabled")) if learning_enabled is None else bool(learning_enabled)
        resolved_high_stakes = bool(existing.get("high_stakes_domains_enabled")) if high_stakes_domains_enabled is None else bool(high_stakes_domains_enabled)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO person_profiles
                    (principal_id, person_id, display_name, profile_scope, consent_mode, learning_enabled, high_stakes_domains_enabled, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (principal_id, person_id) DO UPDATE
                    SET display_name = EXCLUDED.display_name,
                        profile_scope = EXCLUDED.profile_scope,
                        consent_mode = EXCLUDED.consent_mode,
                        learning_enabled = EXCLUDED.learning_enabled,
                        high_stakes_domains_enabled = EXCLUDED.high_stakes_domains_enabled,
                        updated_at = EXCLUDED.updated_at
                    RETURNING principal_id, person_id, display_name, profile_scope, consent_mode,
                              learning_enabled, high_stakes_domains_enabled, created_at, updated_at
                    """,
                    (
                        principal,
                        person,
                        resolved_display_name,
                        resolved_profile_scope,
                        resolved_consent_mode,
                        resolved_learning_enabled,
                        resolved_high_stakes,
                        now,
                        now,
                    ),
                )
                row = cur.fetchone()
        return self._profile_from_row(row)

    def _profile_from_row(self, row: tuple[Any, ...] | None) -> dict[str, object]:
        if not row:
            return {}
        return {
            "principal_id": str(row[0]),
            "person_id": str(row[1]),
            "display_name": str(row[2]),
            "profile_scope": str(row[3]),
            "consent_mode": str(row[4]),
            "learning_enabled": bool(row[5]),
            "high_stakes_domains_enabled": bool(row[6]),
            "created_at": _to_iso(row[7]),
            "updated_at": _to_iso(row[8]),
        }

    def get_person_profile(self, *, principal_id: str, person_id: str) -> dict[str, object] | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT principal_id, person_id, display_name, profile_scope, consent_mode,
                           learning_enabled, high_stakes_domains_enabled, created_at, updated_at
                    FROM person_profiles
                    WHERE principal_id = %s AND person_id = %s
                    """,
                    (str(principal_id or "").strip(), str(person_id or "").strip() or "self"),
                )
                row = cur.fetchone()
        result = self._profile_from_row(row)
        return result or None

    def upsert_preference_node(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str,
        category: str,
        key: str,
        value_json: object,
        strength: str = "medium",
        confidence: float = 0.5,
        source_mode: str = "explicit",
        status: str = "active",
        decay_policy: str = "reinforce_only",
        last_confirmed_at: str = "",
        last_observed_at: str = "",
        node_id: str | None = None,
    ) -> dict[str, object]:
        self.ensure_person_profile(principal_id=principal_id, person_id=person_id)
        principal = str(principal_id or "").strip()
        person = str(person_id or "").strip() or "self"
        normalized_domain = str(domain or "").strip().lower() or "general"
        normalized_category = str(category or "").strip().lower() or "soft_preference"
        normalized_key = str(key or "").strip().lower()
        resolved_node_id = str(node_id or "").strip() or f"pref_node:{person}:{normalized_domain}:{normalized_category}:{normalized_key}"
        now = now_utc_iso()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO preference_nodes
                    (principal_id, person_id, node_id, domain, category, key, value_json, strength, confidence,
                     source_mode, status, decay_policy, last_confirmed_at, last_observed_at, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (principal_id, person_id, domain, category, key) DO UPDATE
                    SET node_id = EXCLUDED.node_id,
                        value_json = EXCLUDED.value_json,
                        strength = EXCLUDED.strength,
                        confidence = EXCLUDED.confidence,
                        source_mode = EXCLUDED.source_mode,
                        status = EXCLUDED.status,
                        decay_policy = EXCLUDED.decay_policy,
                        last_confirmed_at = EXCLUDED.last_confirmed_at,
                        last_observed_at = EXCLUDED.last_observed_at,
                        updated_at = EXCLUDED.updated_at
                    RETURNING principal_id, person_id, node_id, domain, category, key, value_json, strength, confidence,
                              source_mode, status, decay_policy, last_confirmed_at, last_observed_at, created_at, updated_at
                    """,
                    (
                        principal,
                        person,
                        resolved_node_id,
                        normalized_domain,
                        normalized_category,
                        normalized_key,
                        self._json_value(value_json),
                        str(strength or "medium").strip().lower() or "medium",
                        _normalize_float(confidence, default=0.5),
                        str(source_mode or "explicit").strip().lower() or "explicit",
                        str(status or "active").strip().lower() or "active",
                        str(decay_policy or "reinforce_only").strip().lower() or "reinforce_only",
                        str(last_confirmed_at or "").strip(),
                        str(last_observed_at or "").strip(),
                        now,
                        now,
                    ),
                )
                row = cur.fetchone()
        return self._node_from_row(row)

    def _node_from_row(self, row: tuple[Any, ...] | None) -> dict[str, object]:
        if not row:
            return {}
        return {
            "principal_id": str(row[0]),
            "person_id": str(row[1]),
            "node_id": str(row[2]),
            "domain": str(row[3]),
            "category": str(row[4]),
            "key": str(row[5]),
            "value_json": copy.deepcopy(row[6] or {}),
            "strength": str(row[7]),
            "confidence": float(row[8] or 0.0),
            "source_mode": str(row[9]),
            "status": str(row[10]),
            "decay_policy": str(row[11]),
            "last_confirmed_at": str(row[12] or ""),
            "last_observed_at": str(row[13] or ""),
            "created_at": _to_iso(row[14]),
            "updated_at": _to_iso(row[15]),
        }

    def list_preference_nodes(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str | None = None,
        category: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        clauses = ["principal_id = %s", "person_id = %s"]
        params: list[object] = [str(principal_id or "").strip(), str(person_id or "").strip() or "self"]
        if str(domain or "").strip():
            clauses.append("domain = %s")
            params.append(str(domain or "").strip().lower())
        if str(category or "").strip():
            clauses.append("category = %s")
            params.append(str(category or "").strip().lower())
        if str(status or "").strip():
            clauses.append("status = %s")
            params.append(str(status or "").strip().lower())
        params.append(max(1, min(500, int(limit or 200))))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT principal_id, person_id, node_id, domain, category, key, value_json, strength, confidence,
                           source_mode, status, decay_policy, last_confirmed_at, last_observed_at, created_at, updated_at
                    FROM preference_nodes
                    WHERE {' AND '.join(clauses)}
                    ORDER BY updated_at DESC, node_id DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
        return [self._node_from_row(row) for row in rows]

    def record_evidence_event(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str,
        event_type: str,
        object_type: str,
        object_id: str,
        source_ref: str = "",
        raw_signal_json: dict[str, object] | None = None,
        interpreted_signal_json: dict[str, object] | None = None,
        signal_strength: float = 0.5,
        reversible: bool = True,
        event_id: str | None = None,
    ) -> dict[str, object]:
        self.ensure_person_profile(principal_id=principal_id, person_id=person_id)
        principal = str(principal_id or "").strip()
        person = str(person_id or "").strip() or "self"
        resolved_id = str(event_id or "").strip() or f"pref_event:{person}:{int(time.time() * 1000)}"
        now = now_utc_iso()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO preference_evidence_events
                    (principal_id, person_id, event_id, domain, event_type, object_type, object_id, source_ref,
                     raw_signal_json, interpreted_signal_json, signal_strength, reversible, recorded_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING principal_id, person_id, event_id, domain, event_type, object_type, object_id, source_ref,
                              raw_signal_json, interpreted_signal_json, signal_strength, reversible, recorded_at
                    """,
                    (
                        principal,
                        person,
                        resolved_id,
                        str(domain or "").strip().lower() or "general",
                        str(event_type or "").strip().lower() or "observed",
                        str(object_type or "").strip().lower() or "candidate",
                        str(object_id or "").strip(),
                        str(source_ref or "").strip(),
                        self._json_value(raw_signal_json or {}),
                        self._json_value(interpreted_signal_json or {}),
                        _normalize_float(signal_strength, default=0.5),
                        bool(reversible),
                        now,
                    ),
                )
                row = cur.fetchone()
        return self._event_from_row(row)

    def _event_from_row(self, row: tuple[Any, ...] | None) -> dict[str, object]:
        if not row:
            return {}
        return {
            "principal_id": str(row[0]),
            "person_id": str(row[1]),
            "event_id": str(row[2]),
            "domain": str(row[3]),
            "event_type": str(row[4]),
            "object_type": str(row[5]),
            "object_id": str(row[6]),
            "source_ref": str(row[7] or ""),
            "raw_signal_json": copy.deepcopy(row[8] or {}),
            "interpreted_signal_json": copy.deepcopy(row[9] or {}),
            "signal_strength": float(row[10] or 0.0),
            "reversible": bool(row[11]),
            "recorded_at": _to_iso(row[12]),
        }

    def list_evidence_events(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        clauses = ["principal_id = %s", "person_id = %s"]
        params: list[object] = [str(principal_id or "").strip(), str(person_id or "").strip() or "self"]
        if str(domain or "").strip():
            clauses.append("domain = %s")
            params.append(str(domain or "").strip().lower())
        params.append(max(1, min(500, int(limit or 50))))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT principal_id, person_id, event_id, domain, event_type, object_type, object_id, source_ref,
                           raw_signal_json, interpreted_signal_json, signal_strength, reversible, recorded_at
                    FROM preference_evidence_events
                    WHERE {' AND '.join(clauses)}
                    ORDER BY recorded_at DESC, event_id DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
        return [self._event_from_row(row) for row in rows]

    def record_decision_assessment(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str,
        object_type: str,
        object_id: str,
        fit_score: float,
        confidence: float,
        predicted_reaction: str,
        recommendation: str,
        match_reasons_json: list[str],
        mismatch_reasons_json: list[str],
        unknowns_json: list[str],
        blocking_constraints_json: list[str],
        assessment_json: dict[str, object] | None = None,
        assessment_id: str | None = None,
    ) -> dict[str, object]:
        self.ensure_person_profile(principal_id=principal_id, person_id=person_id)
        principal = str(principal_id or "").strip()
        person = str(person_id or "").strip() or "self"
        resolved_id = str(assessment_id or "").strip() or f"decision_assessment:{person}:{int(time.time() * 1000)}"
        now = now_utc_iso()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO preference_decision_assessments
                    (principal_id, person_id, assessment_id, domain, object_type, object_id, fit_score, confidence,
                     predicted_reaction, recommendation, match_reasons_json, mismatch_reasons_json, unknowns_json,
                     blocking_constraints_json, assessment_json, generated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (principal_id, person_id, assessment_id) DO UPDATE SET
                        domain = EXCLUDED.domain,
                        object_type = EXCLUDED.object_type,
                        object_id = EXCLUDED.object_id,
                        fit_score = EXCLUDED.fit_score,
                        confidence = EXCLUDED.confidence,
                        predicted_reaction = EXCLUDED.predicted_reaction,
                        recommendation = EXCLUDED.recommendation,
                        match_reasons_json = EXCLUDED.match_reasons_json,
                        mismatch_reasons_json = EXCLUDED.mismatch_reasons_json,
                        unknowns_json = EXCLUDED.unknowns_json,
                        blocking_constraints_json = EXCLUDED.blocking_constraints_json,
                        assessment_json = EXCLUDED.assessment_json,
                        generated_at = EXCLUDED.generated_at
                    RETURNING principal_id, person_id, assessment_id, domain, object_type, object_id, fit_score, confidence,
                              predicted_reaction, recommendation, match_reasons_json, mismatch_reasons_json, unknowns_json,
                              blocking_constraints_json, assessment_json, generated_at
                    """,
                    (
                        principal,
                        person,
                        resolved_id,
                        str(domain or "").strip().lower() or "general",
                        str(object_type or "").strip().lower() or "candidate",
                        str(object_id or "").strip(),
                        float(fit_score),
                        _normalize_float(confidence, default=0.5),
                        str(predicted_reaction or "").strip().lower() or "consider",
                        str(recommendation or "").strip().lower() or "mention",
                        self._json_value([str(v or "").strip() for v in match_reasons_json if str(v or "").strip()]),
                        self._json_value([str(v or "").strip() for v in mismatch_reasons_json if str(v or "").strip()]),
                        self._json_value([str(v or "").strip() for v in unknowns_json if str(v or "").strip()]),
                        self._json_value([str(v or "").strip() for v in blocking_constraints_json if str(v or "").strip()]),
                        self._json_value(assessment_json or {}),
                        now,
                    ),
                )
                row = cur.fetchone()
        return self._assessment_from_row(row)

    def _assessment_from_row(self, row: tuple[Any, ...] | None) -> dict[str, object]:
        if not row:
            return {}
        return {
            "principal_id": str(row[0]),
            "person_id": str(row[1]),
            "assessment_id": str(row[2]),
            "domain": str(row[3]),
            "object_type": str(row[4]),
            "object_id": str(row[5]),
            "fit_score": float(row[6] or 0.0),
            "confidence": float(row[7] or 0.0),
            "predicted_reaction": str(row[8]),
            "recommendation": str(row[9]),
            "match_reasons_json": list(row[10] or []),
            "mismatch_reasons_json": list(row[11] or []),
            "unknowns_json": list(row[12] or []),
            "blocking_constraints_json": list(row[13] or []),
            "assessment_json": copy.deepcopy(row[14] or {}),
            "generated_at": _to_iso(row[15]),
        }

    def list_decision_assessments(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        clauses = ["principal_id = %s", "person_id = %s"]
        params: list[object] = [str(principal_id or "").strip(), str(person_id or "").strip() or "self"]
        if str(domain or "").strip():
            clauses.append("domain = %s")
            params.append(str(domain or "").strip().lower())
        params.append(max(1, min(500, int(limit or 50))))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT principal_id, person_id, assessment_id, domain, object_type, object_id, fit_score, confidence,
                           predicted_reaction, recommendation, match_reasons_json, mismatch_reasons_json, unknowns_json,
                           blocking_constraints_json, assessment_json, generated_at
                    FROM preference_decision_assessments
                    WHERE {' AND '.join(clauses)}
                    ORDER BY generated_at DESC, assessment_id DESC
                    LIMIT %s
                    """,
                    tuple(params),
                )
                rows = cur.fetchall()
        return [self._assessment_from_row(row) for row in rows]

    def record_profile_correction(
        self,
        *,
        principal_id: str,
        person_id: str,
        target_type: str,
        target_id: str,
        old_value_json: object,
        new_value_json: object,
        reason: str = "",
        corrected_by: str = "",
        correction_id: str | None = None,
    ) -> dict[str, object]:
        self.ensure_person_profile(principal_id=principal_id, person_id=person_id)
        principal = str(principal_id or "").strip()
        person = str(person_id or "").strip() or "self"
        resolved_id = str(correction_id or "").strip() or f"profile_correction:{person}:{int(time.time() * 1000)}"
        now = now_utc_iso()
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO preference_profile_corrections
                    (principal_id, person_id, correction_id, target_type, target_id, old_value_json, new_value_json, reason, corrected_by, corrected_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING principal_id, person_id, correction_id, target_type, target_id, old_value_json,
                              new_value_json, reason, corrected_by, corrected_at
                    """,
                    (
                        principal,
                        person,
                        resolved_id,
                        str(target_type or "").strip().lower() or "preference_node",
                        str(target_id or "").strip(),
                        self._json_value(old_value_json),
                        self._json_value(new_value_json),
                        str(reason or "").strip(),
                        str(corrected_by or "").strip(),
                        now,
                    ),
                )
                row = cur.fetchone()
        return self._correction_from_row(row)

    def _correction_from_row(self, row: tuple[Any, ...] | None) -> dict[str, object]:
        if not row:
            return {}
        return {
            "principal_id": str(row[0]),
            "person_id": str(row[1]),
            "correction_id": str(row[2]),
            "target_type": str(row[3]),
            "target_id": str(row[4]),
            "old_value_json": copy.deepcopy(row[5] or {}),
            "new_value_json": copy.deepcopy(row[6] or {}),
            "reason": str(row[7] or ""),
            "corrected_by": str(row[8] or ""),
            "corrected_at": _to_iso(row[9]),
        }

    def list_profile_corrections(
        self,
        *,
        principal_id: str,
        person_id: str,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT principal_id, person_id, correction_id, target_type, target_id, old_value_json,
                           new_value_json, reason, corrected_by, corrected_at
                    FROM preference_profile_corrections
                    WHERE principal_id = %s AND person_id = %s
                    ORDER BY corrected_at DESC, correction_id DESC
                    LIMIT %s
                    """,
                    (
                        str(principal_id or "").strip(),
                        str(person_id or "").strip() or "self",
                        max(1, min(500, int(limit or 50))),
                    ),
                )
                rows = cur.fetchall()
        return [self._correction_from_row(row) for row in rows]

    def erase_principal(self, principal_id: str) -> dict[str, int]:
        principal = str(principal_id or "").strip()
        if not principal:
            return {"profiles": 0, "nodes": 0, "evidence_events": 0, "assessments": 0, "corrections": 0}
        tables = (
            ("preference_profile_corrections", "corrections"),
            ("preference_decision_assessments", "assessments"),
            ("preference_evidence_events", "evidence_events"),
            ("preference_nodes", "nodes"),
            ("person_profiles", "profiles"),
        )
        counts: dict[str, int] = {}
        with self._connect() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    for table, label in tables:
                        cur.execute(f"DELETE FROM {table} WHERE principal_id = %s", (principal,))
                        counts[label] = int(cur.rowcount or 0)
        return counts

    def export_principal(self, principal_id: str) -> dict[str, list[dict[str, object]]]:
        principal = str(principal_id or "").strip()
        if not principal:
            return {"profiles": [], "nodes": [], "evidence_events": [], "assessments": [], "corrections": []}
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT principal_id, person_id, display_name, profile_scope, consent_mode,
                           learning_enabled, high_stakes_domains_enabled, created_at, updated_at
                    FROM person_profiles WHERE principal_id = %s ORDER BY person_id
                    """,
                    (principal,),
                )
                profile_rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT principal_id, person_id, node_id, domain, category, key, value_json, strength, confidence,
                           source_mode, status, decay_policy, last_confirmed_at, last_observed_at, created_at, updated_at
                    FROM preference_nodes WHERE principal_id = %s ORDER BY updated_at DESC, node_id DESC
                    """,
                    (principal,),
                )
                node_rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT principal_id, person_id, event_id, domain, event_type, object_type, object_id, source_ref,
                           raw_signal_json, interpreted_signal_json, signal_strength, reversible, recorded_at
                    FROM preference_evidence_events WHERE principal_id = %s ORDER BY recorded_at DESC, event_id DESC
                    """,
                    (principal,),
                )
                evidence_rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT principal_id, person_id, assessment_id, domain, object_type, object_id, fit_score, confidence,
                           predicted_reaction, recommendation, match_reasons_json, mismatch_reasons_json, unknowns_json,
                           blocking_constraints_json, assessment_json, generated_at
                    FROM preference_decision_assessments WHERE principal_id = %s ORDER BY generated_at DESC, assessment_id DESC
                    """,
                    (principal,),
                )
                assessment_rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT principal_id, person_id, correction_id, target_type, target_id, old_value_json,
                           new_value_json, reason, corrected_by, corrected_at
                    FROM preference_profile_corrections WHERE principal_id = %s ORDER BY corrected_at DESC, correction_id DESC
                    """,
                    (principal,),
                )
                correction_rows = cur.fetchall()
        return {
            "profiles": [self._profile_from_row(row) for row in profile_rows],
            "nodes": [self._node_from_row(row) for row in node_rows],
            "evidence_events": [self._event_from_row(row) for row in evidence_rows],
            "assessments": [self._assessment_from_row(row) for row in assessment_rows],
            "corrections": [self._correction_from_row(row) for row in correction_rows],
        }
