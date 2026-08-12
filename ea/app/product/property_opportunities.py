from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any


OpportunityAssessor = Callable[..., dict[str, object] | None]


def property_opportunity_public_projection(value: object) -> dict[str, object]:
    """Return the customer-safe subset of a durable opportunity assessment."""

    if not isinstance(value, dict):
        return {}
    status = str(value.get("status") or "").strip().lower()
    if status not in {"ready", "preview", "unavailable"}:
        return {}

    def text(key: str, *, limit: int = 500) -> str:
        return str(value.get(key) or "").strip()[:limit]

    def text_list(key: str) -> list[str]:
        raw = value.get(key)
        if not isinstance(raw, list):
            return []
        return [str(item).strip()[:500] for item in raw if str(item).strip()][:12]

    projection: dict[str, object] = {
        "opportunity_id": text("opportunity_id", limit=300),
        "status": status,
        "domain": text("domain", limit=80),
        "object_type": text("object_type", limit=80),
        "object_id": text("object_id"),
        "person_id": text("person_id", limit=200),
        "run_id": text("run_id", limit=200),
        "predicted_reaction": text("predicted_reaction", limit=1200),
        "recommendation": text("recommendation", limit=200),
        "match_reasons": text_list("match_reasons"),
        "mismatch_reasons": text_list("mismatch_reasons"),
        "unknowns": text_list("unknowns"),
        "blocking_constraints": text_list("blocking_constraints"),
        "generated_at": text("generated_at", limit=100),
    }
    for key in ("fit_score", "confidence"):
        try:
            projection[key] = float(value.get(key) or 0.0)
        except (TypeError, ValueError):
            projection[key] = 0.0
    return projection


def _candidate_opportunity_ref(candidate: dict[str, object]) -> str:
    for key in ("candidate_ref", "source_ref", "listing_id"):
        value = str(candidate.get(key) or "").strip()
        if value:
            return value[:500]
    fingerprint_source = "|".join(
        (
            str(candidate.get("property_url") or "").strip(),
            str(candidate.get("title") or "").strip(),
            str(candidate.get("source_label") or "").strip(),
        )
    )
    digest = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:24]
    return f"property-opportunity-{digest}"


def _candidate_domain(candidate: dict[str, object]) -> str:
    provider = " ".join(
        str(candidate.get(key) or "").strip().lower()
        for key in ("source_platform", "platform", "provider_family", "source_label")
    )
    return "willhaben" if "willhaben" in provider else "property"


def _opportunity_assessment_id(*, domain: str, object_id: str, run_id: str) -> str:
    """Bind one durable assessment row to one search-run candidate."""

    framed = "\x00".join(
        (
            str(run_id or "").strip(),
            str(domain or "").strip().lower(),
            str(object_id or "").strip(),
        )
    )
    return f"property_opportunity:{hashlib.sha256(framed.encode('utf-8')).hexdigest()}"


def _assessment_input(candidate: dict[str, object]) -> dict[str, object]:
    facts = (
        dict(candidate.get("property_facts") or {})
        if isinstance(candidate.get("property_facts"), dict)
        else {}
    )
    payload = dict(facts)
    for key, value in candidate.items():
        if key in {"assessment", "preference_assessment", "opportunity"}:
            continue
        payload[key] = value
    return payload


def _assessment_projection(
    assessment: dict[str, object],
    *,
    domain: str,
    object_id: str,
    person_id: str,
    run_id: str,
) -> dict[str, object]:
    details = (
        dict(assessment.get("assessment_json") or {})
        if isinstance(assessment.get("assessment_json"), dict)
        else {}
    )

    def first(key: str, fallback: object = "") -> object:
        value = assessment.get(key)
        if value not in (None, "", [], {}):
            return value
        value = details.get(key)
        return fallback if value in (None, "", [], {}) else value

    opportunity_id = str(assessment.get("assessment_id") or "").strip()
    return {
        "opportunity_id": opportunity_id,
        "status": "ready" if opportunity_id else "preview",
        "domain": domain,
        "object_type": str(assessment.get("object_type") or "listing").strip() or "listing",
        "object_id": str(assessment.get("object_id") or object_id).strip() or object_id,
        "person_id": str(person_id or "self").strip() or "self",
        "run_id": str(run_id or "").strip(),
        "fit_score": float(first("fit_score", 0.0) or 0.0),
        "confidence": float(first("confidence", 0.0) or 0.0),
        "predicted_reaction": str(first("predicted_reaction") or "").strip(),
        "recommendation": str(first("recommendation") or "").strip(),
        "match_reasons": [
            str(value).strip()
            for value in list(first("match_reasons_json", []) or [])
            if str(value).strip()
        ][:12],
        "mismatch_reasons": [
            str(value).strip()
            for value in list(first("mismatch_reasons_json", []) or [])
            if str(value).strip()
        ][:12],
        "unknowns": [
            str(value).strip()
            for value in list(first("unknowns_json", []) or [])
            if str(value).strip()
        ][:12],
        "blocking_constraints": [
            str(value).strip()
            for value in list(first("blocking_constraints_json", []) or [])
            if str(value).strip()
        ][:12],
        "generated_at": str(assessment.get("generated_at") or "").strip(),
    }


def materialize_property_search_opportunities(
    sources: list[dict[str, object]],
    *,
    principal_id: str,
    person_id: str,
    run_id: str,
    assess: OpportunityAssessor,
) -> dict[str, object]:
    """Persist one preference assessment per discovered candidate and project it onto every card copy.

    Search can contain the same candidate in both ``top_candidates`` and
    ``research_candidates``. The assessment is therefore cached per candidate
    reference so one search run creates one durable opportunity record, while
    each customer-visible projection receives the same explanation.
    """

    normalized_principal = str(principal_id or "").strip()
    normalized_person = str(person_id or "").strip() or "self"
    cached: dict[str, dict[str, object] | None] = {}
    failed_refs: set[str] = set()

    for source in sources:
        if not isinstance(source, dict):
            continue
        source_ready_refs: set[str] = set()
        source_failed_refs: set[str] = set()
        for collection_key in ("research_candidates", "top_candidates"):
            candidates = source.get(collection_key)
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                object_id = _candidate_opportunity_ref(candidate)
                if not str(candidate.get("candidate_ref") or "").strip():
                    candidate["candidate_ref"] = object_id
                domain = _candidate_domain(candidate)
                cache_key = f"{domain}:{object_id}"
                if cache_key not in cached:
                    try:
                        raw = assess(
                            principal_id=normalized_principal,
                            person_id=normalized_person,
                            domain=domain,
                            object_type="listing",
                            object_id=object_id,
                            object_payload=_assessment_input(candidate),
                            assessment_id=_opportunity_assessment_id(
                                domain=domain,
                                object_id=object_id,
                                run_id=run_id,
                            ),
                        )
                        cached[cache_key] = dict(raw) if isinstance(raw, dict) else None
                    except Exception:
                        cached[cache_key] = None
                assessment = cached[cache_key]
                if assessment is None:
                    candidate["opportunity"] = {
                        "status": "unavailable",
                        "domain": domain,
                        "object_type": "listing",
                        "object_id": object_id,
                        "run_id": str(run_id or "").strip(),
                    }
                    candidate["opportunity_status"] = "unavailable"
                    failed_refs.add(cache_key)
                    source_failed_refs.add(cache_key)
                    continue
                opportunity = _assessment_projection(
                    assessment,
                    domain=domain,
                    object_id=object_id,
                    person_id=normalized_person,
                    run_id=run_id,
                )
                candidate["opportunity"] = opportunity
                candidate["opportunity_id"] = str(opportunity.get("opportunity_id") or "")
                candidate["opportunity_status"] = str(opportunity.get("status") or "ready")
                candidate["preference_fit_score"] = float(opportunity.get("fit_score") or 0.0)
                candidate["preference_confidence"] = float(opportunity.get("confidence") or 0.0)
                candidate["opportunity_predicted_reaction"] = str(
                    opportunity.get("predicted_reaction") or ""
                )
                candidate["opportunity_recommendation"] = str(
                    opportunity.get("recommendation") or ""
                )
                source_ready_refs.add(cache_key)
        source["opportunity_total"] = len(source_ready_refs)
        source["opportunity_persistence_failed_total"] = len(source_failed_refs)

    ready_total = len([value for value in cached.values() if value is not None])
    return {
        "opportunity_total": ready_total,
        "opportunity_persistence_failed_total": len(failed_refs),
        "opportunity_person_id": normalized_person,
        "opportunity_generation_status": (
            "ready"
            if ready_total > 0 and not failed_refs
            else ("partial" if ready_total > 0 else ("unavailable" if failed_refs else "empty"))
        ),
    }


def find_property_search_candidate(
    run: dict[str, object],
    *,
    candidate_ref: str,
) -> dict[str, object] | None:
    """Return one exact candidate from a principal-scoped search snapshot."""

    target = str(candidate_ref or "").strip()
    if not target or len(target) > 500:
        return None
    summary = dict(run.get("summary") or {}) if isinstance(run.get("summary"), dict) else {}
    collections: list[object] = [
        run.get("ranked_candidates"),
        summary.get("ranked_candidates"),
    ]
    for source in list(summary.get("sources") or run.get("sources") or []):
        if not isinstance(source, dict):
            continue
        collections.extend(
            (
                source.get("research_candidates"),
                source.get("top_candidates"),
            )
        )
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for candidate in collection:
            if not isinstance(candidate, dict):
                continue
            resolved = str(
                candidate.get("candidate_ref")
                or candidate.get("source_ref")
                or candidate.get("listing_id")
                or ""
            ).strip()
            if resolved == target:
                return dict(candidate)
    return None
