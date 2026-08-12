from __future__ import annotations

import copy
from typing import Dict, List, Protocol

from app.domain.models import now_utc_iso


class PreferenceProfileRepository(Protocol):
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
        ...

    def get_person_profile(self, *, principal_id: str, person_id: str) -> dict[str, object] | None:
        ...

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
        ...

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
        ...

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
        ...

    def list_evidence_events(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        ...

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
        ...

    def list_decision_assessments(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        ...

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
        ...

    def list_profile_corrections(
        self,
        *,
        principal_id: str,
        person_id: str,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        ...

    def erase_principal(self, principal_id: str) -> dict[str, int]:
        ...

    def export_principal(self, principal_id: str) -> dict[str, list[dict[str, object]]]:
        ...


def _normalize_profile_scope(value: str) -> str:
    raw = str(value or "").strip().lower()
    return raw or "personal"


def _normalize_consent_mode(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"explicit_only", "behavioral_learning", "paused"}:
        return raw
    return "explicit_only"


def _normalize_strength(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"low", "medium", "high"}:
        return raw
    return "medium"


def _normalize_status(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"active", "inactive", "archived", "pending_review"}:
        return raw
    return "active"


def _normalize_float(value: object, *, default: float = 0.5) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return max(0.0, min(1.0, out))


class InMemoryPreferenceProfileRepository:
    def __init__(self) -> None:
        self._profiles: Dict[tuple[str, str], dict[str, object]] = {}
        self._nodes: Dict[tuple[str, str, str], dict[str, object]] = {}
        self._node_order: List[tuple[str, str, str]] = []
        self._node_identity: Dict[tuple[str, str, str, str, str], tuple[str, str, str]] = {}
        self._events: Dict[tuple[str, str, str], dict[str, object]] = {}
        self._event_order: List[tuple[str, str, str]] = []
        self._assessments: Dict[tuple[str, str, str], dict[str, object]] = {}
        self._assessment_order: List[tuple[str, str, str]] = []
        self._corrections: Dict[tuple[str, str, str], dict[str, object]] = {}
        self._correction_order: List[tuple[str, str, str]] = []

    def _profile_key(self, principal_id: str, person_id: str) -> tuple[str, str]:
        return (str(principal_id or "").strip(), str(person_id or "").strip() or "self")

    def _row_copy(self, row: dict[str, object] | None) -> dict[str, object] | None:
        return copy.deepcopy(row) if row is not None else None

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
        key = self._profile_key(principal_id, person_id)
        existing = self._profiles.get(key)
        now = now_utc_iso()
        if existing is not None:
            updated = {
                **existing,
                "display_name": str(display_name if display_name is not None else existing.get("display_name") or key[1]).strip() or key[1],
                "profile_scope": _normalize_profile_scope(
                    str(profile_scope if profile_scope is not None else existing.get("profile_scope") or "personal")
                ),
                "consent_mode": _normalize_consent_mode(
                    str(consent_mode if consent_mode is not None else existing.get("consent_mode") or "explicit_only")
                ),
                "learning_enabled": bool(existing.get("learning_enabled")) if learning_enabled is None else bool(learning_enabled),
                "high_stakes_domains_enabled": bool(existing.get("high_stakes_domains_enabled"))
                if high_stakes_domains_enabled is None
                else bool(high_stakes_domains_enabled),
                "updated_at": now,
            }
            self._profiles[key] = updated
            return copy.deepcopy(updated)
        row = {
            "person_id": key[1],
            "principal_id": key[0],
            "display_name": str(display_name if display_name is not None else key[1]).strip() or key[1],
            "profile_scope": _normalize_profile_scope(str(profile_scope or "personal")),
            "consent_mode": _normalize_consent_mode(str(consent_mode or "explicit_only")),
            "learning_enabled": bool(learning_enabled) if learning_enabled is not None else False,
            "high_stakes_domains_enabled": bool(high_stakes_domains_enabled) if high_stakes_domains_enabled is not None else False,
            "created_at": now,
            "updated_at": now,
        }
        self._profiles[key] = row
        return copy.deepcopy(row)

    def get_person_profile(self, *, principal_id: str, person_id: str) -> dict[str, object] | None:
        return self._row_copy(self._profiles.get(self._profile_key(principal_id, person_id)))

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
        identity = (
            str(principal_id or "").strip(),
            str(person_id or "").strip() or "self",
            str(domain or "").strip().lower(),
            str(category or "").strip().lower(),
            str(key or "").strip().lower(),
        )
        storage_key = self._node_identity.get(identity)
        if storage_key is None:
            resolved_node_id = str(node_id or "").strip() or f"pref_node:{identity[1]}:{identity[2]}:{identity[3]}:{identity[4]}"
            storage_key = (identity[0], identity[1], resolved_node_id)
        existing = self._nodes.get(storage_key)
        now = now_utc_iso()
        row = {
            "node_id": storage_key[2],
            "principal_id": identity[0],
            "person_id": identity[1],
            "domain": identity[2],
            "category": identity[3],
            "key": identity[4],
            "value_json": copy.deepcopy(value_json),
            "strength": _normalize_strength(strength or (existing or {}).get("strength") or "medium"),
            "confidence": _normalize_float(confidence, default=float((existing or {}).get("confidence") or 0.5)),
            "source_mode": str(source_mode or (existing or {}).get("source_mode") or "explicit").strip().lower() or "explicit",
            "status": _normalize_status(status or (existing or {}).get("status") or "active"),
            "decay_policy": str(decay_policy or (existing or {}).get("decay_policy") or "reinforce_only").strip().lower()
            or "reinforce_only",
            "last_confirmed_at": str(last_confirmed_at or (existing or {}).get("last_confirmed_at") or "").strip(),
            "last_observed_at": str(last_observed_at or (existing or {}).get("last_observed_at") or "").strip(),
            "created_at": str((existing or {}).get("created_at") or now),
            "updated_at": now,
        }
        self._nodes[storage_key] = row
        self._node_identity[identity] = storage_key
        if storage_key not in self._node_order:
            self._node_order.append(storage_key)
        return copy.deepcopy(row)

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
        principal = str(principal_id or "").strip()
        person = str(person_id or "").strip() or "self"
        domain_filter = str(domain or "").strip().lower()
        category_filter = str(category or "").strip().lower()
        status_filter = str(status or "").strip().lower()
        n = max(1, min(500, int(limit or 200)))
        rows = [self._nodes[key] for key in reversed(self._node_order) if key in self._nodes]
        rows = [row for row in rows if row["principal_id"] == principal and row["person_id"] == person]
        if domain_filter:
            rows = [row for row in rows if str(row.get("domain") or "") == domain_filter]
        if category_filter:
            rows = [row for row in rows if str(row.get("category") or "") == category_filter]
        if status_filter:
            rows = [row for row in rows if str(row.get("status") or "") == status_filter]
        return [copy.deepcopy(row) for row in rows[:n]]

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
        resolved_id = str(event_id or "").strip() or f"pref_event:{person}:{len(self._event_order) + 1}"
        key = (principal, person, resolved_id)
        now = now_utc_iso()
        row = {
            "event_id": resolved_id,
            "principal_id": principal,
            "person_id": person,
            "domain": str(domain or "").strip().lower() or "general",
            "event_type": str(event_type or "").strip().lower() or "observed",
            "object_type": str(object_type or "").strip().lower() or "candidate",
            "object_id": str(object_id or "").strip(),
            "source_ref": str(source_ref or "").strip(),
            "raw_signal_json": copy.deepcopy(raw_signal_json or {}),
            "interpreted_signal_json": copy.deepcopy(interpreted_signal_json or {}),
            "signal_strength": _normalize_float(signal_strength, default=0.5),
            "reversible": bool(reversible),
            "recorded_at": now,
        }
        self._events[key] = row
        self._event_order.append(key)
        return copy.deepcopy(row)

    def list_evidence_events(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        principal = str(principal_id or "").strip()
        person = str(person_id or "").strip() or "self"
        domain_filter = str(domain or "").strip().lower()
        n = max(1, min(500, int(limit or 50)))
        rows = [self._events[key] for key in reversed(self._event_order) if key in self._events]
        rows = [row for row in rows if row["principal_id"] == principal and row["person_id"] == person]
        if domain_filter:
            rows = [row for row in rows if str(row.get("domain") or "") == domain_filter]
        return [copy.deepcopy(row) for row in rows[:n]]

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
        resolved_id = str(assessment_id or "").strip() or f"decision_assessment:{person}:{len(self._assessment_order) + 1}"
        key = (principal, person, resolved_id)
        now = now_utc_iso()
        row = {
            "assessment_id": resolved_id,
            "principal_id": principal,
            "person_id": person,
            "domain": str(domain or "").strip().lower() or "general",
            "object_type": str(object_type or "").strip().lower() or "candidate",
            "object_id": str(object_id or "").strip(),
            "fit_score": float(fit_score),
            "confidence": _normalize_float(confidence, default=0.5),
            "predicted_reaction": str(predicted_reaction or "").strip().lower() or "consider",
            "recommendation": str(recommendation or "").strip().lower() or "mention",
            "match_reasons_json": [str(value or "").strip() for value in match_reasons_json if str(value or "").strip()],
            "mismatch_reasons_json": [str(value or "").strip() for value in mismatch_reasons_json if str(value or "").strip()],
            "unknowns_json": [str(value or "").strip() for value in unknowns_json if str(value or "").strip()],
            "blocking_constraints_json": [str(value or "").strip() for value in blocking_constraints_json if str(value or "").strip()],
            "assessment_json": copy.deepcopy(assessment_json or {}),
            "generated_at": now,
        }
        self._assessments[key] = row
        if key not in self._assessment_order:
            self._assessment_order.append(key)
        return copy.deepcopy(row)

    def list_decision_assessments(
        self,
        *,
        principal_id: str,
        person_id: str,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        principal = str(principal_id or "").strip()
        person = str(person_id or "").strip() or "self"
        domain_filter = str(domain or "").strip().lower()
        n = max(1, min(500, int(limit or 50)))
        rows = [self._assessments[key] for key in reversed(self._assessment_order) if key in self._assessments]
        rows = [row for row in rows if row["principal_id"] == principal and row["person_id"] == person]
        if domain_filter:
            rows = [row for row in rows if str(row.get("domain") or "") == domain_filter]
        return [copy.deepcopy(row) for row in rows[:n]]

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
        resolved_id = str(correction_id or "").strip() or f"profile_correction:{person}:{len(self._correction_order) + 1}"
        key = (principal, person, resolved_id)
        now = now_utc_iso()
        row = {
            "correction_id": resolved_id,
            "principal_id": principal,
            "person_id": person,
            "target_type": str(target_type or "").strip().lower() or "preference_node",
            "target_id": str(target_id or "").strip(),
            "old_value_json": copy.deepcopy(old_value_json),
            "new_value_json": copy.deepcopy(new_value_json),
            "reason": str(reason or "").strip(),
            "corrected_by": str(corrected_by or "").strip(),
            "corrected_at": now,
        }
        self._corrections[key] = row
        self._correction_order.append(key)
        return copy.deepcopy(row)

    def list_profile_corrections(
        self,
        *,
        principal_id: str,
        person_id: str,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        principal = str(principal_id or "").strip()
        person = str(person_id or "").strip() or "self"
        n = max(1, min(500, int(limit or 50)))
        rows = [self._corrections[key] for key in reversed(self._correction_order) if key in self._corrections]
        rows = [row for row in rows if row["principal_id"] == principal and row["person_id"] == person]
        return [copy.deepcopy(row) for row in rows[:n]]

    def erase_principal(self, principal_id: str) -> dict[str, int]:
        principal = str(principal_id or "").strip()
        if not principal:
            return {"profiles": 0, "nodes": 0, "evidence_events": 0, "assessments": 0, "corrections": 0}
        profile_keys = {key for key in self._profiles if key[0] == principal}
        node_keys = {key for key in self._nodes if key[0] == principal}
        event_keys = {key for key in self._events if key[0] == principal}
        assessment_keys = {key for key in self._assessments if key[0] == principal}
        correction_keys = {key for key in self._corrections if key[0] == principal}
        for key in profile_keys:
            self._profiles.pop(key, None)
        for key in node_keys:
            self._nodes.pop(key, None)
        for key in event_keys:
            self._events.pop(key, None)
        for key in assessment_keys:
            self._assessments.pop(key, None)
        for key in correction_keys:
            self._corrections.pop(key, None)
        self._node_order = [key for key in self._node_order if key not in node_keys]
        self._event_order = [key for key in self._event_order if key not in event_keys]
        self._assessment_order = [key for key in self._assessment_order if key not in assessment_keys]
        self._correction_order = [key for key in self._correction_order if key not in correction_keys]
        self._node_identity = {
            identity: key for identity, key in self._node_identity.items() if key not in node_keys
        }
        return {
            "profiles": len(profile_keys),
            "nodes": len(node_keys),
            "evidence_events": len(event_keys),
            "assessments": len(assessment_keys),
            "corrections": len(correction_keys),
        }

    def export_principal(self, principal_id: str) -> dict[str, list[dict[str, object]]]:
        principal = str(principal_id or "").strip()
        if not principal:
            return {"profiles": [], "nodes": [], "evidence_events": [], "assessments": [], "corrections": []}
        return {
            "profiles": [copy.deepcopy(row) for key, row in self._profiles.items() if key[0] == principal],
            "nodes": [copy.deepcopy(row) for key, row in self._nodes.items() if key[0] == principal],
            "evidence_events": [copy.deepcopy(row) for key, row in self._events.items() if key[0] == principal],
            "assessments": [copy.deepcopy(row) for key, row in self._assessments.items() if key[0] == principal],
            "corrections": [copy.deepcopy(row) for key, row in self._corrections.items() if key[0] == principal],
        }
