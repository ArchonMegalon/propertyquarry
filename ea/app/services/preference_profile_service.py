from __future__ import annotations

import copy
import logging
import math
from typing import Any

from app.repositories.preference_profiles import InMemoryPreferenceProfileRepository, PreferenceProfileRepository
from app.repositories.preference_profiles_postgres import PostgresPreferenceProfileRepository
from app.services.property_market_catalog import currency_code_for_country, supported_currency_codes
from app.settings import Settings, ensure_storage_fallback_allowed


def _backend_mode(settings: Settings) -> str:
    return str(getattr(getattr(settings, "storage", None), "backend", "") or "auto").strip().lower() or "auto"


def build_preference_profile_repo(settings: Settings) -> PreferenceProfileRepository:
    backend = _backend_mode(settings)
    log = logging.getLogger("ea.preference_profiles")
    if backend == "memory":
        ensure_storage_fallback_allowed(settings, "preference profiles configured for memory")
        return InMemoryPreferenceProfileRepository()
    if backend == "postgres":
        if not settings.database_url:
            raise RuntimeError("EA_STORAGE_BACKEND=postgres requires DATABASE_URL")
        return PostgresPreferenceProfileRepository(settings.database_url)
    if settings.database_url:
        try:
            return PostgresPreferenceProfileRepository(settings.database_url)
        except Exception as exc:
            ensure_storage_fallback_allowed(settings, "preference profiles auto fallback", exc)
            log.warning("postgres preference-profile backend unavailable in auto mode; falling back to memory: %s", exc)
    ensure_storage_fallback_allowed(settings, "preference profiles auto backend without DATABASE_URL")
    return InMemoryPreferenceProfileRepository()


def build_preference_profile_service(settings: Settings) -> "PreferenceProfileService":
    return PreferenceProfileService(repo=build_preference_profile_repo(settings))


def _compact_text(value: object, *, fallback: str = "", limit: int = 160) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return fallback
    if len(text) <= limit:
        return text
    return f"{text[: max(limit - 3, 0)]}..."


def _normalize_strength(value: str) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"low", "medium", "high"}:
        return raw
    return "medium"


def _strength_weight(value: str) -> float:
    return {"low": 0.7, "medium": 1.5, "high": 2.5}.get(_normalize_strength(value), 1.5)


def _normalize_confidence(value: object, *, default: float = 0.5) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    return max(0.0, min(1.0, out))


def _normalize_key(value: object) -> str:
    return str(value or "").strip().lower()


def _canonical_preference_key(value: object) -> str:
    return _normalize_key(value)


def _list_value(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _candidate_currency_code(facts: dict[str, object]) -> str:
    supported = {code.upper() for code in supported_currency_codes()}
    for key in ("price_currency", "currency_code", "currency"):
        currency_code = str(facts.get(key) or "").strip().upper()
        if currency_code in supported:
            return currency_code
    for key in ("country_code", "market_country_code"):
        country_code = str(facts.get(key) or "").strip().upper()
        if country_code:
            return currency_code_for_country(country_code)
    return "EUR"


def _money_label(value: float, *, currency_code: str) -> str:
    if float(value).is_integer():
        amount = f"{value:.0f}"
    else:
        amount = f"{value:g}"
    return f"{currency_code} {amount}"


def _positive_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0.0:
        return None
    return parsed


def _first_fact_text(facts: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = _compact_text(facts.get(key), fallback="", limit=120)
        if value:
            return value
    return ""


def _distance_access_copy(
    *,
    facts: dict[str, object],
    access_label: str,
    distance_m: float,
    name_keys: tuple[str, ...] = (),
    suffix: str = "",
) -> str:
    subject = f"{access_label} access"
    place_name = _first_fact_text(facts, *name_keys)
    if place_name:
        subject = f"{subject} via {place_name}"
    sentence = f"{subject} is about {int(distance_m)} m away"
    if suffix:
        return f"{sentence}, {suffix}"
    return f"{sentence}."


class PreferenceProfileService:
    def __init__(self, *, repo: PreferenceProfileRepository) -> None:
        self._repo = repo

    def ensure_profile(
        self,
        *,
        principal_id: str,
        person_id: str = "self",
        display_name: str | None = None,
        profile_scope: str | None = None,
        consent_mode: str | None = None,
        learning_enabled: bool | None = None,
        high_stakes_domains_enabled: bool | None = None,
    ) -> dict[str, object]:
        return self._repo.ensure_person_profile(
            principal_id=principal_id,
            person_id=person_id,
            display_name=display_name,
            profile_scope=profile_scope,
            consent_mode=consent_mode,
            learning_enabled=learning_enabled,
            high_stakes_domains_enabled=high_stakes_domains_enabled,
        )

    def get_profile_bundle(self, *, principal_id: str, person_id: str = "self") -> dict[str, object]:
        profile = self._repo.get_person_profile(principal_id=principal_id, person_id=person_id)
        if profile is None:
            profile = self.ensure_profile(principal_id=principal_id, person_id=person_id)
        return {
            "profile": copy.deepcopy(profile),
            "preference_nodes": self._repo.list_preference_nodes(principal_id=principal_id, person_id=person_id, limit=300),
            "recent_evidence_events": self._repo.list_evidence_events(principal_id=principal_id, person_id=person_id, limit=50),
            "recent_decision_assessments": self._repo.list_decision_assessments(principal_id=principal_id, person_id=person_id, limit=25),
            "recent_corrections": self._repo.list_profile_corrections(principal_id=principal_id, person_id=person_id, limit=25),
        }

    def erase_principal(self, principal_id: str) -> dict[str, int]:
        method = getattr(self._repo, "erase_principal", None)
        if not callable(method):
            return {"profiles": 0, "nodes": 0, "evidence_events": 0, "assessments": 0, "corrections": 0}
        return {
            str(key): int(value or 0)
            for key, value in dict(method(str(principal_id or "").strip()) or {}).items()
        }

    def export_principal(self, principal_id: str) -> dict[str, list[dict[str, object]]]:
        method = getattr(self._repo, "export_principal", None)
        if callable(method):
            payload = dict(method(str(principal_id or "").strip()) or {})
            return {
                key: [copy.deepcopy(row) for row in list(payload.get(key) or []) if isinstance(row, dict)]
                for key in ("profiles", "nodes", "evidence_events", "assessments", "corrections")
            }
        bundle = self.get_profile_bundle(principal_id=principal_id, person_id="self")
        return {
            "profiles": [dict(bundle.get("profile") or {})],
            "nodes": [dict(row) for row in list(bundle.get("preference_nodes") or []) if isinstance(row, dict)],
            "evidence_events": [dict(row) for row in list(bundle.get("recent_evidence_events") or []) if isinstance(row, dict)],
            "assessments": [dict(row) for row in list(bundle.get("recent_decision_assessments") or []) if isinstance(row, dict)],
            "corrections": [dict(row) for row in list(bundle.get("recent_corrections") or []) if isinstance(row, dict)],
        }

    def upsert_preference_node(
        self,
        *,
        principal_id: str,
        person_id: str = "self",
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
        self.ensure_profile(principal_id=principal_id, person_id=person_id)
        key = _canonical_preference_key(key)
        return self._repo.upsert_preference_node(
            principal_id=principal_id,
            person_id=person_id,
            domain=domain,
            category=category,
            key=key,
            value_json=value_json,
            strength=strength,
            confidence=confidence,
            source_mode=source_mode,
            status=status,
            decay_policy=decay_policy,
            last_confirmed_at=last_confirmed_at,
            last_observed_at=last_observed_at,
            node_id=node_id,
        )

    def apply_correction(
        self,
        *,
        principal_id: str,
        person_id: str = "self",
        domain: str,
        category: str,
        key: str,
        value_json: object,
        strength: str = "high",
        reason: str = "",
        corrected_by: str = "",
    ) -> dict[str, object]:
        existing = next(
            (
                row
                for row in self._repo.list_preference_nodes(
                    principal_id=principal_id,
                    person_id=person_id,
                    domain=domain,
                    category=category,
                    limit=200,
                )
                if _normalize_key(row.get("key")) == _normalize_key(key)
            ),
            None,
        )
        updated = self.upsert_preference_node(
            principal_id=principal_id,
            person_id=person_id,
            domain=domain,
            category=category,
            key=key,
            value_json=value_json,
            strength=strength,
            confidence=1.0,
            source_mode="explicit_correction",
            status="active",
            decay_policy="manual_only",
            last_confirmed_at=existing.get("updated_at", "") if isinstance(existing, dict) else "",
        )
        correction = self._repo.record_profile_correction(
            principal_id=principal_id,
            person_id=person_id,
            target_type="preference_node",
            target_id=str(updated.get("node_id") or ""),
            old_value_json=dict(existing or {}) if isinstance(existing, dict) else {},
            new_value_json=dict(updated or {}),
            reason=reason,
            corrected_by=corrected_by,
        )
        return {
            "node": updated,
            "correction": correction,
        }

    def archive_preference_node(
        self,
        *,
        principal_id: str,
        person_id: str = "self",
        node_id: str,
        reason: str = "",
        corrected_by: str = "",
    ) -> dict[str, object]:
        normalized_node_id = str(node_id or "").strip()
        if not normalized_node_id:
            raise ValueError("preference_node_id_required")
        existing = next(
            (
                row
                for row in self._repo.list_preference_nodes(
                    principal_id=principal_id,
                    person_id=person_id,
                    limit=500,
                )
                if str(row.get("node_id") or "").strip() == normalized_node_id
            ),
            None,
        )
        if not isinstance(existing, dict):
            raise KeyError("preference_node_not_found")
        updated = self.upsert_preference_node(
            principal_id=principal_id,
            person_id=person_id,
            domain=str(existing.get("domain") or "willhaben"),
            category=str(existing.get("category") or "soft_preference"),
            key=str(existing.get("key") or ""),
            value_json=copy.deepcopy(existing.get("value_json")),
            strength=str(existing.get("strength") or "medium"),
            confidence=_normalize_confidence(existing.get("confidence"), default=0.5),
            source_mode="explicit_correction",
            status="inactive",
            decay_policy="manual_only",
            last_confirmed_at=str(existing.get("updated_at") or ""),
        )
        correction = self._repo.record_profile_correction(
            principal_id=principal_id,
            person_id=person_id,
            target_type="preference_node",
            target_id=str(updated.get("node_id") or normalized_node_id),
            old_value_json=dict(existing),
            new_value_json=dict(updated),
            reason=str(reason or "Removed from the active search profile.").strip(),
            corrected_by=corrected_by,
        )
        return {
            "node": updated,
            "correction": correction,
        }

    def record_evidence_event(
        self,
        *,
        principal_id: str,
        person_id: str = "self",
        domain: str,
        event_type: str,
        object_type: str,
        object_id: str,
        source_ref: str = "",
        raw_signal_json: dict[str, object] | None = None,
        interpreted_signal_json: dict[str, object] | None = None,
        signal_strength: float = 0.5,
        reversible: bool = True,
    ) -> dict[str, object]:
        self.ensure_profile(principal_id=principal_id, person_id=person_id)
        event = self._repo.record_evidence_event(
            principal_id=principal_id,
            person_id=person_id,
            domain=domain,
            event_type=event_type,
            object_type=object_type,
            object_id=object_id,
            source_ref=source_ref,
            raw_signal_json=raw_signal_json or {},
            interpreted_signal_json=interpreted_signal_json or {},
            signal_strength=signal_strength,
            reversible=reversible,
        )
        applied_nodes = self._apply_inference_from_event(
            principal_id=principal_id,
            person_id=person_id,
            event=event,
        )
        return {
            "event": event,
            "applied_nodes": applied_nodes,
        }

    def assess_candidate(
        self,
        *,
        principal_id: str,
        person_id: str = "self",
        domain: str,
        object_type: str,
        object_id: str,
        object_payload: dict[str, object],
        assessment_id: str | None = None,
        persist: bool = True,
        require_existing_profile: bool = False,
    ) -> dict[str, object] | None:
        profile = self._repo.get_person_profile(principal_id=principal_id, person_id=person_id)
        if profile is None:
            if require_existing_profile:
                return None
            profile = self.ensure_profile(principal_id=principal_id, person_id=person_id)
        nodes = self._repo.list_preference_nodes(principal_id=principal_id, person_id=person_id, domain=domain, limit=300)
        if not nodes and require_existing_profile:
            return None
        if str(domain or "").strip().lower() == "willhaben":
            assessment = self._assess_willhaben(
                object_id=object_id,
                object_payload=object_payload,
                nodes=nodes,
            )
        else:
            assessment = self._assess_generic(
                object_id=object_id,
                object_payload=object_payload,
                nodes=nodes,
            )
        if persist:
            stored = self._repo.record_decision_assessment(
                principal_id=principal_id,
                person_id=person_id,
                domain=domain,
                object_type=object_type,
                object_id=object_id,
                fit_score=float(assessment["fit_score"]),
                confidence=float(assessment["confidence"]),
                predicted_reaction=str(assessment["predicted_reaction"]),
                recommendation=str(assessment["recommendation"]),
                match_reasons_json=list(assessment.get("match_reasons_json") or []),
                mismatch_reasons_json=list(assessment.get("mismatch_reasons_json") or []),
                unknowns_json=list(assessment.get("unknowns_json") or []),
                blocking_constraints_json=list(assessment.get("blocking_constraints_json") or []),
                assessment_json=assessment,
                assessment_id=assessment_id,
            )
            return stored
        return assessment

    def build_teable_projection_records(self, *, principal_id: str, person_id: str = "self") -> dict[str, list[dict[str, object]]]:
        bundle = self.get_profile_bundle(principal_id=principal_id, person_id=person_id)
        profile = dict(bundle.get("profile") or {})
        return {
            "preference_review_queue": [
                {
                    "projection_id": str(row.get("node_id") or ""),
                    "person_id": str(row.get("person_id") or ""),
                    "display_name": str(profile.get("display_name") or person_id),
                    "domain": str(row.get("domain") or ""),
                    "category": str(row.get("category") or ""),
                    "key": str(row.get("key") or ""),
                    "confidence": float(row.get("confidence") or 0.0),
                    "source_mode": str(row.get("source_mode") or ""),
                    "status": str(row.get("status") or ""),
                    "target_ref": f"preference_node:{row.get('node_id')}",
                    "projection_version": str(row.get("updated_at") or ""),
                    "editable_fields_allowlist": ["value_json", "strength", "status"],
                    "evidence_ref_count": self._node_evidence_count(bundle=bundle, node=row),
                    "last_updated_at": str(row.get("updated_at") or ""),
                    "expiry_at": "",
                    "correlation_id": f"{principal_id}:{person_id}:{row.get('node_id')}",
                }
                for row in list(bundle.get("preference_nodes") or [])[:50]
            ],
            "preference_recent_assessments": [
                {
                    "projection_id": str(row.get("assessment_id") or ""),
                    "person_id": str(row.get("person_id") or ""),
                    "display_name": str(profile.get("display_name") or person_id),
                    "domain": str(row.get("domain") or ""),
                    "object_type": str(row.get("object_type") or ""),
                    "object_id": str(row.get("object_id") or ""),
                    "fit_score": float(row.get("fit_score") or 0.0),
                    "confidence": float(row.get("confidence") or 0.0),
                    "recommendation": str(row.get("recommendation") or ""),
                    "predicted_reaction": str(row.get("predicted_reaction") or ""),
                    "updated_at": str(row.get("generated_at") or ""),
                    "correlation_id": f"{principal_id}:{person_id}:{row.get('assessment_id')}",
                }
                for row in list(bundle.get("recent_decision_assessments") or [])[:50]
            ],
        }

    def _node_evidence_count(self, *, bundle: dict[str, object], node: dict[str, object]) -> int:
        key = _normalize_key(node.get("key"))
        domain = _normalize_key(node.get("domain"))
        count = 0
        for row in list(bundle.get("recent_evidence_events") or []):
            if _normalize_key(row.get("domain")) != domain:
                continue
            interpreted = dict(row.get("interpreted_signal_json") or {})
            for hint in list(interpreted.get("preference_hints") or []):
                if _normalize_key(dict(hint).get("key")) == key:
                    count += 1
        return count

    def _apply_inference_from_event(
        self,
        *,
        principal_id: str,
        person_id: str,
        event: dict[str, object],
    ) -> list[dict[str, object]]:
        profile = self._repo.get_person_profile(principal_id=principal_id, person_id=person_id)
        if profile is None:
            return []
        consent_mode = str(profile.get("consent_mode") or "").strip().lower()
        learning_enabled = bool(profile.get("learning_enabled"))
        interpreted = dict(event.get("interpreted_signal_json") or {})
        if consent_mode == "paused":
            return []
        if consent_mode == "explicit_only" and not interpreted.get("preference_hints"):
            return []
        if consent_mode != "explicit_only" and not learning_enabled and not interpreted.get("preference_hints"):
            return []
        applied: list[dict[str, object]] = []
        for hint in list(interpreted.get("preference_hints") or []):
            if not isinstance(hint, dict):
                continue
            applied_row = self._apply_preference_hint(
                principal_id=principal_id,
                person_id=person_id,
                hint=hint,
                event=event,
            )
            if applied_row:
                applied.append(applied_row)
        if interpreted.get("preference_hints"):
            return applied
        inferred = self._infer_hints_from_event(event)
        for hint in inferred:
            applied_row = self._apply_preference_hint(
                principal_id=principal_id,
                person_id=person_id,
                hint=hint,
                event=event,
            )
            if applied_row:
                applied.append(applied_row)
        return applied

    def _apply_preference_hint(
        self,
        *,
        principal_id: str,
        person_id: str,
        hint: dict[str, object],
        event: dict[str, object],
    ) -> dict[str, object] | None:
        domain = str(hint.get("domain") or event.get("domain") or "general").strip().lower()
        category = str(hint.get("category") or "").strip().lower()
        key = str(hint.get("key") or "").strip().lower()
        if not domain or not category or not key:
            return None
        value_json = copy.deepcopy(hint.get("value_json"))
        merge_mode = str(hint.get("merge_mode") or "replace").strip().lower()
        existing = next(
            (
                row
                for row in self._repo.list_preference_nodes(
                    principal_id=principal_id,
                    person_id=person_id,
                    domain=domain,
                    category=category,
                    limit=50,
                )
                if _normalize_key(row.get("key")) == key
            ),
            None,
        )
        if merge_mode == "append_unique":
            values = []
            if isinstance(existing, dict):
                values.extend(_list_value(existing.get("value_json")))
            values.extend(_list_value(value_json))
            deduped: list[str] = []
            seen: set[str] = set()
            for item in values:
                normalized = _normalize_key(item)
                if normalized and normalized not in seen:
                    deduped.append(item)
                    seen.add(normalized)
            value_json = deduped
        confidence = max(
            _normalize_confidence(hint.get("confidence"), default=0.55),
            _normalize_confidence(event.get("signal_strength"), default=0.5),
        )
        return self.upsert_preference_node(
            principal_id=principal_id,
            person_id=person_id,
            domain=domain,
            category=category,
            key=key,
            value_json=value_json,
            strength=str(hint.get("strength") or "medium"),
            confidence=confidence,
            source_mode=str(hint.get("source_mode") or "behavioral_inference"),
            status=str(hint.get("status") or "active"),
            decay_policy=str(hint.get("decay_policy") or "reinforce_only"),
            last_observed_at=str(event.get("recorded_at") or ""),
            node_id=str((existing or {}).get("node_id") or "") or None,
        )

    def _infer_hints_from_event(self, event: dict[str, object]) -> list[dict[str, object]]:
        domain = _normalize_key(event.get("domain"))
        event_type = _normalize_key(event.get("event_type"))
        raw = dict(event.get("raw_signal_json") or {})
        hints: list[dict[str, object]] = []
        district = str(raw.get("district") or raw.get("postal_name") or raw.get("location") or "").strip()
        heating = str(raw.get("heating") or raw.get("heating_type") or "").strip()
        if domain == "willhaben" and event_type in {"listing_shortlisted", "listing_saved", "listing_accepted"}:
            if district:
                hints.append(
                    {
                        "domain": "willhaben",
                        "category": "soft_preference",
                        "key": "preferred_areas",
                        "value_json": [district],
                        "strength": "medium",
                        "merge_mode": "append_unique",
                    }
                )
            if bool(raw.get("has_floorplan")):
                hints.append(
                    {
                        "domain": "willhaben",
                        "category": "soft_preference",
                        "key": "requires_floorplan_for_remote_review",
                        "value_json": True,
                        "strength": "medium",
                    }
                )
        if domain == "willhaben" and event_type in {"listing_rejected", "listing_dismissed"}:
            if heating:
                hints.append(
                    {
                        "domain": "willhaben",
                        "category": "aversion",
                        "key": "avoid_heating_types",
                        "value_json": [heating],
                        "strength": "medium",
                        "merge_mode": "append_unique",
                    }
                )
        return hints

    def _assess_generic(self, *, object_id: str, object_payload: dict[str, object], nodes: list[dict[str, object]]) -> dict[str, object]:
        del object_id, object_payload
        confidence = 0.2 if nodes else 0.0
        recommendation = "ask_for_clarification" if nodes else "mention"
        return {
            "domain": "general",
            "object_type": "candidate",
            "object_id": "",
            "fit_score": 50.0,
            "confidence": confidence,
            "predicted_reaction": "consider",
            "recommendation": recommendation,
            "match_reasons_json": [],
            "mismatch_reasons_json": [],
            "unknowns_json": ["No domain-specific scoring model is configured yet for this candidate type."],
            "blocking_constraints_json": [],
        }

    def _assess_willhaben(
        self,
        *,
        object_id: str,
        object_payload: dict[str, object],
        nodes: list[dict[str, object]],
    ) -> dict[str, object]:
        facts = dict(object_payload or {})
        search_preferences = (
            dict(facts.get("search_preferences") or {})
            if isinstance(facts.get("search_preferences"), dict)
            else {}
        )
        attributes = dict(facts.get("attribute_map") or {})
        currency_code = _candidate_currency_code(facts)
        district = str(
            facts.get("postal_name")
            or facts.get("district")
            or facts.get("location")
            or facts.get("source_scope_location")
            or facts.get("address")
            or ""
        ).strip()
        total_rent = _positive_number(
            facts.get("total_rent_eur")
            or facts.get("monthly_rent_eur")
            or facts.get("price_eur")
        )
        rooms = _positive_number(facts.get("rooms"))
        area_sqm = _positive_number(facts.get("area_sqm") or facts.get("area_m2"))
        listing_mode = str(
            search_preferences.get("listing_mode")
            or facts.get("listing_mode")
            or "rent"
        ).strip().lower()
        active_price = (
            _positive_number(
                facts.get("purchase_price_eur")
                or facts.get("price_eur")
                or facts.get("purchase_price")
            )
            if listing_mode in {"buy", "sale", "purchase", "kauf"}
            else total_rent
        )
        active_price_label = (
            "Purchase price"
            if listing_mode in {"buy", "sale", "purchase", "kauf"}
            else "Total monthly rent"
        )
        heating = str(
            facts.get("heating")
            or facts.get("heating_type")
            or attributes.get("HEIZUNGSART")
            or attributes.get("HEATING_TYPE")
            or ""
        ).strip()
        has_floorplan = bool(facts.get("floorplan_count") or facts.get("has_floorplan") or facts.get("floorplan_urls_json"))
        has_360 = (
            bool(facts.get("has_360"))
            or str(facts.get("tour_media_mode") or "").strip().lower() == "panorama_360"
            or bool(facts.get("source_virtual_tour_url"))
        )
        has_balcony = bool(
            facts.get("balcony")
            or facts.get("has_balcony")
            or facts.get("terrace")
            or facts.get("has_terrace")
        )
        has_lift = (
            bool(facts.get("lift"))
            or "lift" in _normalize_key(facts.get("headline_hook"))
            or "lift" in _normalize_key(attributes.get("GENERAL_TEXT_ADVERT/Ausstattung"))
        )
        feature_text = " ".join(
            str(part or "").strip()
            for part in (
                facts.get("title"),
                facts.get("headline_hook"),
                facts.get("summary"),
                facts.get("description_text"),
                attributes.get("GENERAL_TEXT_ADVERT/Ausstattung"),
                attributes.get("GENERAL_TEXT_ADVERT"),
                attributes.get("AUSSTATTUNG"),
                attributes.get("FLOOR"),
            )
            if str(part or "").strip()
        ).lower()
        has_balcony = has_balcony or any(
            marker in feature_text
            for marker in ("balkon", "terrasse", "balcony", "terrace", "loggia")
        )
        has_lift = has_lift or any(
            marker in feature_text
            for marker in ("aufzug", "fahrstuhl", "elevator", "lift")
        )
        cooling_negated = any(
            marker in feature_text
            for marker in ("ohne klimaanlage", "keine klimaanlage", "no air conditioning", "without air conditioning")
        )
        has_air_conditioning = bool(facts.get("air_conditioning") or facts.get("has_air_conditioning")) or (not cooling_negated and any(
            marker in feature_text
            for marker in ("klimaanlage", "air conditioning", "aircondition", "splitgerät", "splitgeraet")
        ))
        has_attic_apartment = bool(
            facts.get("top_floor")
            or facts.get("dachgeschoss")
            or facts.get("attic_apartment")
            or facts.get("is_attic_apartment")
        ) or any(
            marker in feature_text
            for marker in ("dachgeschoss", "dachgeschoß", "dg-wohnung", "top floor", "attic apartment", "mansarde")
        )
        quiet_score = float(facts.get("quiet_score") or facts.get("micro_location_quiet_score") or 0.0)
        noise_risk = _normalize_key(
            facts.get("street_noise_risk")
            or facts.get("noise_risk")
            or facts.get("traffic_noise_risk")
            or ""
        )
        nearest_subway_m = float(facts.get("nearest_subway_m") or 0.0)
        nearest_supermarket_m = float(facts.get("nearest_supermarket_m") or 0.0)
        nearest_pharmacy_m = float(facts.get("nearest_pharmacy_m") or 0.0)
        lease_term_years_max = float(facts.get("lease_term_years_max") or 0.0)
        bike_score = float(facts.get("bike_infrastructure_score") or 0.0)
        green_score = float(facts.get("green_space_score") or 0.0)
        playground_score = float(facts.get("playground_score") or 0.0)
        score = 50.0
        match_reasons: list[str] = []
        mismatch_reasons: list[str] = []
        unknowns: list[str] = list(dict(facts.get("decision_summary") or {}).get("unknowns") or [])
        blocking_constraints: list[str] = []
        search_evidence_total = 0

        max_price = _positive_number(
            search_preferences.get("max_price_eur")
            or search_preferences.get("max_monthly_rent_eur")
        )
        if max_price is not None:
            max_price_label = _money_label(max_price, currency_code=currency_code)
            if active_price is None:
                unknowns.append(
                    f"Confirm the {active_price_label.lower()} against the {max_price_label} search ceiling."
                )
            elif active_price <= max_price:
                search_evidence_total += 1
                score += 8.0
                match_reasons.append(
                    f"{active_price_label} {_money_label(active_price, currency_code=currency_code)} "
                    f"is within the {max_price_label} search ceiling."
                )
            else:
                search_evidence_total += 1
                score -= 20.0
                blocking_constraints.append(
                    f"{active_price_label} exceeds the active search ceiling of {max_price_label}."
                )

        minimum_area = _positive_number(
            search_preferences.get("min_area_m2")
            or search_preferences.get("min_area_sqm")
        )
        if minimum_area is not None:
            if area_sqm is None:
                unknowns.append(
                    f"Confirm the living area against the {minimum_area:g} m² search minimum."
                )
            elif area_sqm >= minimum_area:
                search_evidence_total += 1
                score += 6.0
                match_reasons.append(
                    f"The verified {area_sqm:g} m² living area clears the {minimum_area:g} m² search minimum."
                )
            else:
                search_evidence_total += 1
                score -= 20.0
                blocking_constraints.append(
                    f"The living area is below the active {minimum_area:g} m² search minimum."
                )

        minimum_rooms = _positive_number(search_preferences.get("min_rooms"))
        if minimum_rooms is not None:
            if rooms is None:
                unknowns.append(
                    f"Confirm the room count against the {minimum_rooms:g}-room search minimum."
                )
            elif rooms >= minimum_rooms:
                search_evidence_total += 1
                score += 5.0
                match_reasons.append(
                    f"The verified {rooms:g}-room layout clears the {minimum_rooms:g}-room search minimum."
                )
            else:
                search_evidence_total += 1
                score -= 20.0
                blocking_constraints.append(
                    f"The room count is below the active {minimum_rooms:g}-room search minimum."
                )

        location_query = str(search_preferences.get("location_query") or "").strip()
        postal_code = str(
            facts.get("source_postal_code")
            or facts.get("postal_code")
            or ""
        ).strip()
        if location_query and district and (
            (postal_code and postal_code in location_query)
            or _normalize_key(district) in _normalize_key(location_query)
        ):
            search_evidence_total += 1
            score += 5.0
            match_reasons.append(
                f"{district} is inside the selected search area."
            )

        keyword_preferences = (
            dict(search_preferences.get("keyword_preferences") or {})
            if isinstance(search_preferences.get("keyword_preferences"), dict)
            else {}
        )
        def _active_keyword_preference(value: object) -> bool:
            return value is True or str(value or "").strip().lower() in {
                "nice_to_have",
                "must_have",
                "hard",
                "required",
                "strict",
                "important",
                "strong_wish",
                "preferred",
                "yes",
                "on",
            }

        balcony_preference = (
            keyword_preferences.get("balcony")
            or keyword_preferences.get("balkon")
            or keyword_preferences.get("terrace")
            or keyword_preferences.get("terrasse")
        )
        if _active_keyword_preference(balcony_preference):
            if has_balcony:
                search_evidence_total += 1
                score += 3.0
                match_reasons.append(
                    "Balcony, terrace, or loggia space is indicated, matching the active search brief."
                )
            else:
                unknowns.append("Balcony or terrace space is not confirmed.")
        lift_preference = (
            keyword_preferences.get("lift")
            or keyword_preferences.get("aufzug")
            or keyword_preferences.get("elevator")
        )
        if _active_keyword_preference(lift_preference):
            if has_lift:
                search_evidence_total += 1
                score += 3.0
                match_reasons.append("Lift access is indicated, matching the active search brief.")
            else:
                unknowns.append("Lift access is not confirmed.")

        if bool(search_preferences.get("require_floorplan")):
            if has_floorplan:
                search_evidence_total += 1
                score += 4.0
                match_reasons.append("A floor plan satisfies the active remote-review requirement.")
            else:
                score -= 12.0
                blocking_constraints.append(
                    "The active search requires a floor plan, but none is available."
                )
        elif has_floorplan:
            score += 2.0
            match_reasons.append("A floor plan is available for first-pass review.")

        if bool(search_preferences.get("require_360")):
            if has_360:
                search_evidence_total += 1
                score += 4.0
                match_reasons.append("A live 360 source satisfies the active remote-review requirement.")
            else:
                score -= 18.0
                blocking_constraints.append(
                    "The active search requires a live 360 source, but none is available."
                )

        if bool(search_preferences.get("avoid_attic_apartment")) and has_attic_apartment:
            search_evidence_total += 1
            score -= 7.0
            mismatch_reasons.append(
                "The home appears to be top-floor or attic, which conflicts with the active search brief."
            )

        for node in nodes:
            category = _normalize_key(node.get("category"))
            key = _normalize_key(node.get("key"))
            value = node.get("value_json")
            weight = _strength_weight(str(node.get("strength") or "medium")) * max(float(node.get("confidence") or 0.5), 0.3)

            if category == "constraint" and key == "max_total_rent_eur" and isinstance(total_rent, (int, float)):
                try:
                    ceiling = float(value)
                except Exception:
                    ceiling = None
                if ceiling is not None and total_rent > ceiling:
                    ceiling_label = _money_label(ceiling, currency_code=currency_code)
                    blocking_constraints.append(f"Total monthly burden exceeds the preferred ceiling of {ceiling_label}.")
                    score -= 20.0
            elif category == "constraint" and key == "min_rooms" and isinstance(rooms, (int, float)):
                try:
                    minimum_rooms = float(value)
                except Exception:
                    minimum_rooms = None
                if minimum_rooms is not None and rooms < minimum_rooms:
                    blocking_constraints.append(f"Room count is below the required minimum of {minimum_rooms:g}.")
                    score -= 20.0
            elif category == "constraint" and key == "min_area_sqm" and isinstance(area_sqm, (int, float)):
                try:
                    minimum_area = float(value)
                except Exception:
                    minimum_area = None
                if minimum_area is not None and area_sqm < minimum_area:
                    blocking_constraints.append(f"Living area is below the required minimum of {minimum_area:g} m².")
                    score -= 20.0
            elif category == "constraint" and key == "require_floorplan":
                if bool(value) and not has_floorplan:
                    blocking_constraints.append("A floor plan is required for remote screening, but the listing does not provide one.")
                    score -= 12.0
            elif category == "constraint" and key == "require_360":
                if bool(value) and not has_360:
                    blocking_constraints.append("A live 360 or panorama source is required, but the listing does not provide one.")
                    score -= 18.0
            elif category in {"soft_preference", "aversion"} and _canonical_preference_key(key) == "preferred_areas":
                preferred = {_normalize_key(item) for item in _list_value(value)}
                if district and _normalize_key(district) in preferred:
                    score += 9.0 * weight
                    match_reasons.append(f"The listing is in {district}, which matches established area preferences.")
                elif district:
                    score -= 2.5 * weight
                    mismatch_reasons.append(f"The listing is outside the established preferred areas ({district}).")
            elif category in {"soft_preference", "aversion"} and _canonical_preference_key(key) == "avoided_areas":
                avoided = {_normalize_key(item) for item in _list_value(value)}
                if district and _normalize_key(district) in avoided:
                    score -= 6.0 * weight
                    mismatch_reasons.append(f"The listing is in {district}, which matches a recorded location aversion.")
            elif category == "aversion" and key == "avoid_heating_types":
                avoided_heating = {_normalize_key(item) for item in _list_value(value)}
                if heating and _normalize_key(heating) in avoided_heating:
                    score -= 12.0 * weight
                    mismatch_reasons.append(f"{heating} matches a recorded heating aversion.")
            elif category == "soft_preference" and key == "requires_floorplan_for_remote_review":
                if bool(value):
                    if has_floorplan:
                        score += 6.0 * weight
                        match_reasons.append("A floor plan is available, which supports the preferred remote review workflow.")
                    else:
                        score -= 6.0 * weight
                        mismatch_reasons.append("No floor plan is available, which conflicts with the preferred remote review workflow.")
            elif category == "soft_preference" and key == "prefer_balcony":
                if bool(value):
                    if has_balcony:
                        score += 3.0 * weight
                        match_reasons.append("The listing appears to include balcony or outdoor value, which matches the stated preference.")
                    else:
                        score -= 2.0 * weight
                        mismatch_reasons.append("The listing does not clearly show balcony or outdoor value.")
            elif category == "soft_preference" and key == "prefer_lift":
                if bool(value):
                    if has_lift:
                        score += 6.0 * weight
                        match_reasons.append("Lift access appears available, which matches the preferred accessibility workflow.")
                    else:
                        score -= 5.0 * weight
                        mismatch_reasons.append("Lift access is not clearly available, which conflicts with the preferred accessibility workflow.")
            elif category == "soft_preference" and key in {"prefer_air_conditioning", "klimaanlage"}:
                if bool(value):
                    if has_air_conditioning:
                        score += 4.0 * weight
                        match_reasons.append("Air conditioning or active cooling is mentioned.")
                    else:
                        score -= 3.0 * weight
                        mismatch_reasons.append("Air conditioning is not confirmed.")
            elif category == "aversion" and key in {"avoid_attic_apartment", "dachgeschosswohnung"}:
                if bool(value):
                    if has_attic_apartment:
                        score -= 7.0 * weight
                        mismatch_reasons.append("The listing appears to be top-floor or attic, which matches an avoid preference.")
                    else:
                        score += 1.0 * weight
                        match_reasons.append("No top-floor or attic signal is visible.")
            elif category == "soft_preference" and key in {"prefer_attic_apartment", "dachgeschosswohnung"}:
                if bool(value):
                    if has_attic_apartment:
                        score += 3.0 * weight
                        match_reasons.append("The listing appears to be top-floor or attic, which matches the stated preference.")
                    else:
                        score -= 2.0 * weight
                        mismatch_reasons.append("Top-floor or attic status is not confirmed.")
            elif category == "soft_preference" and key == "prefer_360_for_remote_review":
                if bool(value):
                    if has_360:
                        score += 7.0 * weight
                    else:
                        score -= 6.0 * weight
                        mismatch_reasons.append("No live 360 source is available, which conflicts with the preferred remote review workflow.")
            elif category == "soft_preference" and key == "prefer_subway_nearby":
                if bool(value):
                    if nearest_subway_m > 0.0 and nearest_subway_m <= 650.0:
                        score += 6.0 * weight
                        match_reasons.append(
                            _distance_access_copy(
                                facts=facts,
                                access_label="Underground",
                                distance_m=nearest_subway_m,
                                name_keys=("nearest_subway_name", "subway_station_name", "nearest_transit_name", "transit_stop_name"),
                                suffix="which matches the transit preference.",
                            )
                        )
                    elif nearest_subway_m > 1200.0:
                        score -= 6.0 * weight
                        mismatch_reasons.append(
                            _distance_access_copy(
                                facts=facts,
                                access_label="Underground",
                                distance_m=nearest_subway_m,
                                name_keys=("nearest_subway_name", "subway_station_name", "nearest_transit_name", "transit_stop_name"),
                                suffix="which is weaker than preferred.",
                            )
                        )
                    else:
                        if nearest_subway_m > 0.0:
                            unknowns.append(
                                _distance_access_copy(
                                    facts=facts,
                                    access_label="Underground",
                                    distance_m=nearest_subway_m,
                                    name_keys=("nearest_subway_name", "subway_station_name", "nearest_transit_name", "transit_stop_name"),
                                )
                            )
                        else:
                            unknowns.append("The listing does not include a confirmed underground distance.")
            elif category == "soft_preference" and key == "prefer_supermarket_nearby":
                if bool(value):
                    if nearest_supermarket_m > 0.0 and nearest_supermarket_m <= 700.0:
                        score += 4.0 * weight
                        match_reasons.append(
                            _distance_access_copy(
                                facts=facts,
                                access_label="Supermarket",
                                distance_m=nearest_supermarket_m,
                                name_keys=("nearest_supermarket_name", "supermarket_name"),
                                suffix="which matches the daily-life preference.",
                            )
                        )
                    elif nearest_supermarket_m > 1000.0:
                        score -= 4.5 * weight
                        mismatch_reasons.append(
                            _distance_access_copy(
                                facts=facts,
                                access_label="Supermarket",
                                distance_m=nearest_supermarket_m,
                                name_keys=("nearest_supermarket_name", "supermarket_name"),
                                suffix="which is weaker than preferred.",
                            )
                        )
                    else:
                        if nearest_supermarket_m > 0.0:
                            unknowns.append(
                                _distance_access_copy(
                                    facts=facts,
                                    access_label="Supermarket",
                                    distance_m=nearest_supermarket_m,
                                    name_keys=("nearest_supermarket_name", "supermarket_name"),
                                )
                            )
                        else:
                            unknowns.append("The listing does not include a confirmed supermarket distance.")
            elif category == "soft_preference" and key == "prefer_pharmacy_nearby":
                if bool(value):
                    if nearest_pharmacy_m > 0.0 and nearest_pharmacy_m <= 800.0:
                        score += 3.5 * weight
                        match_reasons.append(
                            _distance_access_copy(
                                facts=facts,
                                access_label="Pharmacy",
                                distance_m=nearest_pharmacy_m,
                                name_keys=("nearest_pharmacy_name", "pharmacy_name"),
                                suffix="which matches the daily-life preference.",
                            )
                        )
                    elif nearest_pharmacy_m > 1200.0:
                        score -= 3.5 * weight
                        mismatch_reasons.append(
                            _distance_access_copy(
                                facts=facts,
                                access_label="Pharmacy",
                                distance_m=nearest_pharmacy_m,
                                name_keys=("nearest_pharmacy_name", "pharmacy_name"),
                                suffix="which is weaker than preferred.",
                            )
                        )
                    else:
                        if nearest_pharmacy_m > 0.0:
                            unknowns.append(
                                _distance_access_copy(
                                    facts=facts,
                                    access_label="Pharmacy",
                                    distance_m=nearest_pharmacy_m,
                                    name_keys=("nearest_pharmacy_name", "pharmacy_name"),
                                )
                            )
                        else:
                            unknowns.append("The listing does not include a confirmed pharmacy distance.")
            elif category == "soft_preference" and key == "prefer_unlimited_lease":
                if bool(value):
                    if lease_term_years_max > 0.0 and lease_term_years_max <= 5.0:
                        score -= 5.0 * weight
                        mismatch_reasons.append(f"The lease runs only about {int(lease_term_years_max)} years, which is shorter than preferred.")
                    elif lease_term_years_max > 8.0:
                        score += 3.0 * weight
                        match_reasons.append("The lease duration looks compatible with a longer-term stability preference.")
                    else:
                        if lease_term_years_max > 0.0:
                            unknowns.append(f"The lease runs about {int(lease_term_years_max)} years.")
                        else:
                            unknowns.append("The listing does not include a confirmed lease duration.")
            elif category == "soft_preference" and key == "prefer_lower_total_rent_eur" and isinstance(total_rent, (int, float)):
                try:
                    preferred_rent = float(value)
                except Exception:
                    preferred_rent = 0.0
                if preferred_rent > 0.0:
                    preferred_rent_label = _money_label(preferred_rent, currency_code=currency_code)
                    if total_rent <= preferred_rent:
                        score += 4.0 * weight
                        match_reasons.append(f"The total rent sits within the preferred range (about {preferred_rent_label}).")
                    elif total_rent > preferred_rent * 1.12:
                        score -= 5.0 * weight
                        mismatch_reasons.append(f"The total rent exceeds the preferred range (about {preferred_rent_label}).")
            elif category == "soft_preference" and key == "min_area_sqm_preference" and isinstance(area_sqm, (int, float)):
                try:
                    preferred_area = float(value)
                except Exception:
                    preferred_area = 0.0
                if preferred_area > 0.0:
                    if area_sqm >= preferred_area:
                        score += 3.5 * weight
                        match_reasons.append(f"The living area clears the preferred threshold of about {preferred_area:g} m².")
                    elif area_sqm < preferred_area - 5.0:
                        score -= 5.0 * weight
                        mismatch_reasons.append(f"The living area is below the preferred threshold of about {preferred_area:g} m².")
            elif category == "soft_preference" and key == "prefer_bike_infrastructure":
                if bool(value):
                    if bike_score >= 0.7:
                        score += 5.5 * weight
                        match_reasons.append("Bike infrastructure looks strong enough to match the cycling preference.")
                    elif bike_score > 0.0:
                        score -= 3.5 * weight
                        mismatch_reasons.append("Bike infrastructure looks weaker than the stated preference.")
                    else:
                        unknowns.append("Bike infrastructure still needs local verification.")
            elif category == "soft_preference" and key == "prefer_running_green_space":
                if bool(value):
                    if green_score >= 0.68:
                        score += 4.5 * weight
                        match_reasons.append("Green-space access looks strong enough to support regular running or walking.")
                    elif green_score > 0.0:
                        score -= 2.5 * weight
                        mismatch_reasons.append("Green-space access looks weaker than the stated outdoor preference.")
                    else:
                        unknowns.append("Running and green-space access still need local verification.")
            elif category == "soft_preference" and key == "prefer_playgrounds_nearby":
                if bool(value):
                    if playground_score >= 0.6:
                        score += 3.5 * weight
                        match_reasons.append("Nearby playground access looks compatible with the household preference.")
                    elif playground_score > 0.0:
                        score -= 2.0 * weight
                        mismatch_reasons.append("Nearby playground access looks weaker than preferred.")
                    else:
                        unknowns.append("Playground proximity still needs local verification.")
            elif category == "soft_preference" and key == "prefer_quiet_micro_location":
                if bool(value):
                    if quiet_score >= 0.7 or noise_risk in {"low", "quiet", "calm"}:
                        score += 5.0 * weight
                        match_reasons.append("The micro-location looks quiet enough to match the stated preference.")
                    elif quiet_score > 0.0 or noise_risk in {"medium", "moderate", "high", "busy", "noisy"}:
                        score -= 5.0 * weight
                        mismatch_reasons.append("Noise or street exposure looks weaker than the stated quiet-location preference.")
                    else:
                        unknowns.append("Street noise and quietness still need local verification.")
            elif category == "constraint" and key == "require_lift":
                if bool(value) and not has_lift:
                    blocking_constraints.append("Lift access is required, but the listing does not clearly provide it.")
                    score -= 10.0
            elif category == "constraint" and key == "require_quiet_micro_location":
                if bool(value):
                    if quiet_score > 0.0 and quiet_score < 0.45:
                        blocking_constraints.append("A quiet micro-location is required, but the available signals indicate elevated noise risk.")
                        score -= 12.0
                    elif noise_risk in {"high", "busy", "noisy"}:
                        blocking_constraints.append("A quiet micro-location is required, but the listing appears exposed to street or traffic noise.")
                        score -= 12.0
                    elif quiet_score <= 0.0 and not noise_risk:
                        unknowns.append("Quiet micro-location is required, but noise signals still need verification.")

        if has_360:
            score += 4.0

        if heating and not any(heating.lower() in entry.lower() for entry in mismatch_reasons):
            unknowns.append(f"Check the operating cost and efficiency implications of {heating}.")
        if not district:
            unknowns.append("The micro-location still needs manual review.")

        for row in list(facts.get("fact_requirement_plan") or []):
            if not isinstance(row, dict):
                continue
            state = str(row.get("state") or "").strip().lower()
            label = str(row.get("label") or row.get("key") or "").strip()
            if label and state in {"unknown", "retryable_error", "unavailable", "planned"}:
                unknowns.append(f"{label} is not verified yet.")

        match_reasons = list(dict.fromkeys(match_reasons))
        mismatch_reasons = list(dict.fromkeys(mismatch_reasons))
        unknowns = list(dict.fromkeys(unknowns))
        blocking_constraints = list(dict.fromkeys(blocking_constraints))

        confidence = (
            0.25
            if not nodes and search_evidence_total <= 0
            else min(
                0.92,
                0.4
                + min(len(nodes), 8) * 0.05
                + min(search_evidence_total, 6) * 0.07,
            )
        )
        if blocking_constraints:
            recommendation = "reject"
            predicted_reaction = "reject"
        elif score >= 68:
            recommendation = "shortlist"
            predicted_reaction = "shortlist"
        elif score >= 55:
            recommendation = "mention"
            predicted_reaction = "consider"
        elif score >= 45:
            recommendation = "ask_for_clarification"
            predicted_reaction = "mixed"
        else:
            recommendation = "reject"
            predicted_reaction = "reject"

        return {
            "domain": "willhaben",
            "object_type": "listing",
            "object_id": object_id,
            "fit_score": round(max(0.0, min(100.0, score)), 2),
            "confidence": round(confidence, 2),
            "predicted_reaction": predicted_reaction,
            "recommendation": recommendation,
            "match_reasons_json": match_reasons[:6],
            "mismatch_reasons_json": mismatch_reasons[:6],
            "unknowns_json": unknowns[:6],
            "blocking_constraints_json": blocking_constraints[:4],
        }
