from __future__ import annotations

import html
import json
import math
import os
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pytest

from app.product import service as property_service_module
from app.product.service import ProductService
from app.services.property_market_catalog import CUSTOMER_SEARCH_COUNTRY_ORDER, PROVIDERS, PropertyProviderSpec
from tests.product_test_helpers import build_property_client, start_workspace


TERMINAL_RUN_STATUSES = {"processed", "completed_partial", "failed", "cancelled"}
MATCH_TIERS = ("external_id", "property_url", "source_ref", "title_scope")
PROBE_PRESENT = "PRESENT"
PROBE_ABSENT = "ABSENT"
PROBE_UNKNOWN = "UNKNOWN"
PROBE_OUT_OF_WINDOW = "OUT_OF_WINDOW"
INVALID_SOURCE_FETCH_REPAIR_RESOLUTIONS = {
    "suppressed_missing_location",
    "suppressed_location_scope",
    "suppressed_missing_price",
}
GENERATED_MEDIA_COUNTER_KEYS = (
    "tour_created_total",
    "pending_tour_total",
    "ready_tour_total",
    "flythrough_rendered_total",
    "flythrough_existing_total",
    "flythrough_failed_total",
)
RANKING_AUDIT_SCORE_KEY = "_target_recovery_ranking_score_key"


@dataclass(slots=True)
class PrincipalContext:
    principal_id: str
    workspace_name: str = "PropertyQuarry Canary"


@dataclass(slots=True)
class NegativeControl:
    canonical_url: str = ""
    external_id: str = ""
    source_ref: str = ""
    title: str = ""
    district_hint: str = ""
    must_not_rank: bool = True


@dataclass(slots=True)
class TargetListing:
    provider: str
    country_code: str
    canonical_url: str
    title: str
    listing_mode: str
    property_type: str
    location_query: str
    external_id: str = ""
    source_ref: str = ""
    district_hint: str = ""
    postal_hint: str = ""
    price_eur: float = 0.0
    area_m2: float = 0.0
    rooms: float = 0.0
    selected_platforms: tuple[str, ...] = ()
    selected_districts: tuple[str, ...] = ()
    soft_preferences: dict[str, object] = field(default_factory=dict)
    negatives: tuple[NegativeControl, ...] = ()
    pool_size: int = 1
    picked_index: int = 0


@dataclass(slots=True)
class IdentityMatch:
    matched: bool
    tier: str = ""
    candidate_ref: str = ""
    property_url: str = ""
    title: str = ""
    rank: int = 0


@dataclass(slots=True)
class RankingAudit:
    valid: bool
    reason: str = ""
    score_key: str = ""
    target_ordinal_rank: int = 0
    target_competition_rank: int = 0
    target_score: float = 0.0
    strictly_higher_scored_count: int = 0
    equal_scored_count: int = 0


@dataclass(slots=True)
class RepairTrace:
    repair_needed: bool = False
    repair_triggered: bool = False
    repair_executed: bool = False
    task_ids: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    resolutions: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExactSearchProbe:
    state: str
    reason: str = ""
    source_identities: list[str] = field(default_factory=list)
    successful_source_count: int = 0
    raw_url_count: int = 0
    scanned_url_count: int = 0
    scan_cap_per_source: int = 0
    target_source_index: int | None = None
    target_index: int | None = None
    target_url: str = ""
    errors: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class RecoveryReport:
    case_key: str
    principal_id: str
    run_id: str
    watch_url: str
    provider: str
    target_title: str
    target_url: str
    pool_size: int
    picked_index: int
    run_status: str
    target_found: bool
    target_match_tier: str
    target_rank: int
    target_ranking: RankingAudit
    repair: RepairTrace
    generated_media_counters: dict[str, int]
    negative_hits: list[dict[str, object]]
    ranked_count: int
    attempt_index: int
    source_count: int
    event_count: int
    synthesized_brief: dict[str, object]
    initial_exact_probe: ExactSearchProbe
    final_exact_probe: ExactSearchProbe | None = None
    provider_volatility: bool = False
    variant: str = "targeted"
    summary_diagnostics: dict[str, object] = field(default_factory=dict)
    source_summaries: list[dict[str, object]] = field(default_factory=list)
    recent_events: list[dict[str, object]] = field(default_factory=list)


def _manifest_path() -> Path:
    raw = str(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_MANIFEST") or "").strip()
    if not raw:
        pytest.skip("PROPERTYQUARRY_TARGET_RECOVERY_MANIFEST not set")
    path = Path(raw)
    if not path.exists():
        pytest.skip(f"target-recovery manifest not found: {path}")
    return path


def _normalize_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower() or "https"
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    filtered_query = [
        (key, item)
        for key, item in query
        if str(key or "").strip().lower() not in {"utm_source", "utm_medium", "utm_campaign", "fbclid", "gclid"}
    ]
    normalized_query = urllib.parse.urlencode(sorted(filtered_query))
    return urllib.parse.urlunsplit((scheme, host, path, normalized_query, ""))


def _willhaben_ad_id(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlsplit(raw)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=False))
    ad_id = str(query.get("adId") or query.get("adid") or "").strip()
    if ad_id:
        return ad_id
    match = re.search(r"-(\d{6,})/?$", parsed.path)
    if match:
        return str(match.group(1) or "").strip()
    return ""


def _normalize_text(value: object) -> str:
    return " ".join(html.unescape(str(value or "")).strip().lower().split())


def _coerce_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _load_cases() -> list[TargetListing]:
    if "PROPERTYQUARRY_TARGET_RECOVERY_MANIFEST" not in os.environ:
        return []
    payload = json.loads(_manifest_path().read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise AssertionError("PROPERTYQUARRY_TARGET_RECOVERY_MANIFEST must contain a list")
    cases: list[TargetListing] = []
    for index, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            raise AssertionError(f"manifest entry {index} must be an object")
        cases.append(_materialize_case_from_manifest_row(row, row_index=index))
    return cases


def _stable_pick_index(seed_basis: str, count: int) -> int:
    normalized = str(seed_basis or "").strip()
    if count <= 1:
        return 0
    total = 0
    for character in normalized:
        total = (total * 131 + ord(character)) % 2147483647
    return total % count


def _negative_controls(raw_items: object) -> tuple[NegativeControl, ...]:
    return tuple(
        NegativeControl(
            canonical_url=str(item.get("canonical_url") or "").strip(),
            external_id=str(item.get("external_id") or "").strip(),
            source_ref=str(item.get("source_ref") or "").strip(),
            title=str(item.get("title") or "").strip(),
            district_hint=str(item.get("district_hint") or "").strip(),
            must_not_rank=bool(item.get("must_not_rank", True)),
        )
        for item in list(raw_items or [])
        if isinstance(item, dict)
    )


def _target_listing_from_payload(
    payload: dict[str, object],
    *,
    provider: str,
    country_code: str,
    selected_platforms: tuple[str, ...],
    pool_size: int,
    picked_index: int,
) -> TargetListing:
    return TargetListing(
        provider=provider,
        country_code=country_code,
        canonical_url=str(payload.get("canonical_url") or "").strip(),
        title=str(payload.get("title") or "").strip(),
        listing_mode=str(payload.get("listing_mode") or "").strip().lower(),
        property_type=str(payload.get("property_type") or "").strip().lower(),
        location_query=str(payload.get("location_query") or "").strip(),
        external_id=str(payload.get("external_id") or "").strip(),
        source_ref=str(payload.get("source_ref") or "").strip(),
        district_hint=str(payload.get("district_hint") or "").strip(),
        postal_hint=str(payload.get("postal_hint") or "").strip(),
        price_eur=_coerce_float(payload.get("price_eur")),
        area_m2=_coerce_float(payload.get("area_m2")),
        rooms=_coerce_float(payload.get("rooms")),
        selected_platforms=selected_platforms,
        selected_districts=tuple(
            str(value or "").strip()
            for value in list(payload.get("selected_districts") or [])
            if str(value or "").strip()
        ),
        soft_preferences=dict(payload.get("soft_preferences") or {}) if isinstance(payload.get("soft_preferences"), dict) else {},
        negatives=_negative_controls(payload.get("negatives")),
        pool_size=pool_size,
        picked_index=picked_index,
    )


def _materialize_case_from_manifest_row(row: dict[str, object], *, row_index: int) -> TargetListing:
    provider = str(row.get("provider") or "").strip()
    country_code = str(row.get("country_code") or "").strip().upper()
    selected_platforms = tuple(
        str(value or "").strip()
        for value in list(row.get("selected_platforms") or [provider])
        if str(value or "").strip()
    )
    candidates = [dict(item) for item in list(row.get("candidates") or []) if isinstance(item, dict)]
    if not candidates:
        return _target_listing_from_payload(
            row,
            provider=provider,
            country_code=country_code,
            selected_platforms=selected_platforms,
            pool_size=1,
            picked_index=0,
        )
    seed = str(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_SEED") or "tibor-watch").strip()
    pick_basis = f"{seed}|{country_code}|{provider}|{row_index}|{len(candidates)}"
    picked_index = _stable_pick_index(pick_basis, len(candidates))
    picked = dict(candidates[picked_index])
    if "soft_preferences" not in picked and isinstance(row.get("soft_preferences"), dict):
        picked["soft_preferences"] = dict(row.get("soft_preferences") or {})
    if "selected_districts" not in picked and isinstance(row.get("selected_districts"), list):
        picked["selected_districts"] = list(row.get("selected_districts") or [])
    if "negatives" not in picked and isinstance(row.get("negatives"), list):
        picked["negatives"] = list(row.get("negatives") or [])
    if not str(picked.get("listing_mode") or "").strip() and str(row.get("listing_mode") or "").strip():
        picked["listing_mode"] = row.get("listing_mode")
    if not str(picked.get("property_type") or "").strip() and str(row.get("property_type") or "").strip():
        picked["property_type"] = row.get("property_type")
    return _target_listing_from_payload(
        picked,
        provider=provider,
        country_code=country_code,
        selected_platforms=selected_platforms,
        pool_size=len(candidates),
        picked_index=picked_index,
    )


def _rank_threshold() -> int:
    try:
        return int(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_TARGET_RANK_MAX") or 5)
    except Exception:
        return 0


def _target_provider_matrix_enabled() -> bool:
    return str(os.environ.get("PROPERTYQUARRY_TARGET_PROVIDER_MATRIX") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "enabled",
        "full",
    }


def _default_soft_filter_preferences(case: TargetListing) -> dict[str, object]:
    preferences: dict[str, object] = {
        "max_distance_to_library_m": 500,
        "max_distance_to_library_importance": "nice_to_have",
        "max_distance_to_playground_m": 500,
        "max_distance_to_playground_importance": "nice_to_have",
        "max_distance_to_shopping_center_m": 500,
        "max_distance_to_shopping_center_importance": "avoid",
        "max_distance_to_supermarket_m": 300,
        "max_distance_to_supermarket_importance": "nice_to_have",
        "prefer_good_air_quality": True,
        "prefer_low_crime_area": True,
        "require_parking_pressure_check": True,
    }
    if str(case.country_code or "").strip().upper() != "AT":
        preferences.pop("max_distance_to_playground_m", None)
        preferences.pop("max_distance_to_playground_importance", None)
    return preferences


def _synthesize_search_preferences(
    case: TargetListing,
    *,
    loosen_level: int = 0,
    soft_filters: bool = True,
    default_soft_filters: bool = False,
) -> dict[str, object]:
    if not case.country_code or not case.listing_mode or not case.property_type or not case.location_query:
        raise AssertionError(f"{case.provider or case.title}: manifest entry is missing required target facts")
    selected_platforms = list(case.selected_platforms or ((case.provider,) if case.provider else ()))
    if not selected_platforms:
        raise AssertionError(f"{case.title}: selected_platforms missing")
    try:
        max_results_per_source = max(
            1,
            min(10, int(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_MAX_RESULTS_PER_SOURCE") or 5)),
        )
    except Exception:
        max_results_per_source = 5
    preferences: dict[str, object] = {
        "country_code": case.country_code,
        "language_code": "de" if case.country_code == "AT" else "en",
        "listing_mode": case.listing_mode,
        "property_type": case.property_type if case.property_type in {"apartment", "house", "land", "office"} else "any",
        "location_query": case.location_query,
        "selected_platforms": selected_platforms,
        "property_search_enabled": True,
        "property_commercial": {
            "active_plan_key": str(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_PLAN_KEY") or "agent").strip().lower() or "agent",
            "status": "active",
            "active_until": "2999-01-01T00:00:00+00:00",
        },
        "max_results_per_source": max_results_per_source,
        "search_goal": "home",
        "investment_research_mode": "off",
        "search_mode": "discovery",
        "include_public_housing_signals": False,
        "include_developer_project_signals": False,
        "include_distressed_sale_signals": False,
        "use_stored_feedback_preferences": False,
        "preference_person_id": "self",
        "full_region_scope": False,
        "selected_districts": list(case.selected_districts),
    }
    if case.price_eur > 0:
        if case.listing_mode == "rent":
            multiplier = (1.03, 1.06, 1.10, 1.15, 1.20)[min(loosen_level, 4)]
        else:
            multiplier = (1.05, 1.08, 1.12, 1.18, 1.25)[min(loosen_level, 4)]
        preferences["max_price_eur"] = round(case.price_eur * multiplier, 2)
    if case.area_m2 > 0 and loosen_level <= 1:
        preferences["min_area_m2"] = max(1, int(case.area_m2 * 0.95))
    if case.rooms > 0 and loosen_level == 0:
        rounded_rooms = int(case.rooms) if float(case.rooms).is_integer() else 0
        if rounded_rooms > 0:
            preferences["min_rooms"] = rounded_rooms
    if loosen_level >= 3:
        preferences["property_type"] = "any"
    if soft_filters:
        if default_soft_filters:
            preferences.update(_default_soft_filter_preferences(case))
        for key, value in case.soft_preferences.items():
            preferences[str(key)] = value
    return preferences


def _assert_brief_satisfies_target(case: TargetListing, brief: dict[str, object]) -> None:
    max_price = _coerce_float(brief.get("max_price_eur"))
    if case.price_eur > 0 and max_price > 0 and case.price_eur > max_price:
        raise AssertionError(f"{case.title}: synthesized brief excludes target price")
    min_area = _coerce_float(brief.get("min_area_m2"))
    if case.area_m2 > 0 and min_area > 0 and case.area_m2 < min_area:
        raise AssertionError(f"{case.title}: synthesized brief excludes target area")
    min_rooms = _coerce_float(brief.get("min_rooms"))
    if case.rooms > 0 and min_rooms > 0 and case.rooms < min_rooms:
        raise AssertionError(f"{case.title}: synthesized brief excludes target room count")


def _watch_url(run_id: str) -> str:
    return f"/app/properties?run_id={urllib.parse.quote(run_id, safe='')}"


def _print_watch_banner(case: TargetListing, principal: PrincipalContext, run_id: str, *, variant: str = "targeted") -> None:
    sys.stderr.write(
        "\n".join(
            [
                "",
                f"[target-recovery] principal={principal.principal_id}",
                f"[target-recovery] provider={case.provider}",
                f"[target-recovery] variant={variant}",
                f"[target-recovery] target={case.title}",
                f"[target-recovery] run_id={run_id}",
                f"[target-recovery] watch={_watch_url(run_id)}",
                "",
            ]
        )
    )
    sys.stderr.flush()


def _candidate_rows(status_payload: dict[str, object]) -> list[dict[str, object]]:
    summary = dict(status_payload.get("summary") or {}) if isinstance(status_payload.get("summary"), dict) else {}
    ranked = [dict(row) for row in list(summary.get("ranked_candidates") or []) if isinstance(row, dict)]
    if ranked:
        for candidate in ranked:
            candidate[RANKING_AUDIT_SCORE_KEY] = "ranking_score"
        return ranked
    synthesized: list[dict[str, object]] = []
    for source in [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]:
        for candidate in [dict(row) for row in list(source.get("top_candidates") or []) if isinstance(row, dict)]:
            candidate.setdefault("source_label", str(source.get("source_label") or source.get("label") or "").strip())
            synthesized.append(candidate)
    synthesized.sort(key=lambda item: float(item.get("fit_score") or 0.0), reverse=True)
    for index, candidate in enumerate(synthesized, start=1):
        candidate.setdefault("rank", index)
        candidate[RANKING_AUDIT_SCORE_KEY] = "fit_score"
    return synthesized


def _source_rows(status_payload: dict[str, object]) -> list[dict[str, object]]:
    summary = dict(status_payload.get("summary") or {}) if isinstance(status_payload.get("summary"), dict) else {}
    return [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]


def _title_scope_match(candidate: dict[str, object], case: TargetListing) -> bool:
    candidate_title = _normalize_text(candidate.get("title"))
    if candidate_title and candidate_title == _normalize_text(case.title):
        candidate_facts = dict(candidate.get("property_facts") or {}) if isinstance(candidate.get("property_facts"), dict) else {}
        combined_scope = " ".join(
            _normalize_text(value)
            for value in (
                candidate.get("source_scope_location"),
                candidate_facts.get("source_scope_location"),
                candidate_facts.get("postal_name"),
                candidate_facts.get("district"),
                case.location_query,
            )
            if str(value or "").strip()
        )
        return _normalize_text(case.location_query) in combined_scope or _normalize_text(case.district_hint) in combined_scope
    return False


def _match_candidate(candidate: dict[str, object], case: TargetListing) -> IdentityMatch:
    candidate_external_id = str(candidate.get("external_id") or candidate.get("listing_id") or "").strip()
    if case.external_id and candidate_external_id and case.external_id == candidate_external_id:
        return IdentityMatch(
            matched=True,
            tier="external_id",
            candidate_ref=str(candidate.get("candidate_ref") or "").strip(),
            property_url=str(candidate.get("property_url") or "").strip(),
            title=str(candidate.get("title") or "").strip(),
            rank=int(candidate.get("rank") or 0),
        )
    candidate_url = _normalize_url(candidate.get("property_url") or candidate.get("review_url") or candidate.get("packet_url"))
    if case.canonical_url and candidate_url and _normalize_url(case.canonical_url) == candidate_url:
        return IdentityMatch(
            matched=True,
            tier="property_url",
            candidate_ref=str(candidate.get("candidate_ref") or "").strip(),
            property_url=str(candidate.get("property_url") or "").strip(),
            title=str(candidate.get("title") or "").strip(),
            rank=int(candidate.get("rank") or 0),
        )
    case_willhaben_ad_id = _willhaben_ad_id(case.canonical_url)
    candidate_willhaben_ad_id = _willhaben_ad_id(candidate_url)
    if case_willhaben_ad_id and candidate_willhaben_ad_id and case_willhaben_ad_id == candidate_willhaben_ad_id:
        return IdentityMatch(
            matched=True,
            tier="property_url",
            candidate_ref=str(candidate.get("candidate_ref") or "").strip(),
            property_url=str(candidate.get("property_url") or "").strip(),
            title=str(candidate.get("title") or "").strip(),
            rank=int(candidate.get("rank") or 0),
        )
    candidate_source_ref = str(candidate.get("source_ref") or "").strip()
    if case.source_ref and candidate_source_ref and case.source_ref == candidate_source_ref:
        return IdentityMatch(
            matched=True,
            tier="source_ref",
            candidate_ref=str(candidate.get("candidate_ref") or "").strip(),
            property_url=str(candidate.get("property_url") or "").strip(),
            title=str(candidate.get("title") or "").strip(),
            rank=int(candidate.get("rank") or 0),
        )
    if _title_scope_match(candidate, case):
        return IdentityMatch(
            matched=True,
            tier="title_scope",
            candidate_ref=str(candidate.get("candidate_ref") or "").strip(),
            property_url=str(candidate.get("property_url") or "").strip(),
            title=str(candidate.get("title") or "").strip(),
            rank=int(candidate.get("rank") or 0),
        )
    return IdentityMatch(matched=False)


def _match_ranked_target(candidates: list[dict[str, object]], case: TargetListing) -> IdentityMatch:
    for candidate in candidates:
        match = _match_candidate(candidate, case)
        if match.matched:
            return match
    return IdentityMatch(matched=False)


def _candidate_ranking_score(candidate: dict[str, object], *, score_key: str) -> float | None:
    if score_key not in candidate or candidate.get(score_key) is None:
        return None
    raw_score = candidate.get(score_key)
    if isinstance(raw_score, bool):
        return None
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _audit_target_ranking(
    candidates: list[dict[str, object]],
    case: TargetListing,
) -> RankingAudit:
    if not candidates:
        return RankingAudit(valid=False, reason="ranked_candidates_missing")

    score_keys = {
        str(candidate.get(RANKING_AUDIT_SCORE_KEY) or "").strip()
        for candidate in candidates
    }
    if "" in score_keys:
        return RankingAudit(valid=False, reason="candidate_score_provenance_missing")
    if len(score_keys) != 1:
        return RankingAudit(valid=False, reason="candidate_score_provenance_mixed")
    score_key = next(iter(score_keys))
    if score_key not in {"ranking_score", "fit_score"}:
        return RankingAudit(valid=False, reason="candidate_score_provenance_unsupported")

    scores: list[float] = []
    target_position = 0
    target_score = 0.0
    for position, candidate in enumerate(candidates, start=1):
        reported_rank = candidate.get("rank")
        if type(reported_rank) is not int:
            return RankingAudit(valid=False, reason="candidate_rank_invalid")
        if reported_rank != position:
            return RankingAudit(valid=False, reason="candidate_ranks_not_sequential")
        score = _candidate_ranking_score(candidate, score_key=score_key)
        if score is None:
            return RankingAudit(valid=False, reason="candidate_score_missing_or_invalid", score_key=score_key)
        if scores and score > scores[-1]:
            return RankingAudit(valid=False, reason="candidate_scores_not_descending")
        scores.append(score)
        if not target_position and _match_candidate(candidate, case).matched:
            target_position = position
            target_score = score

    if not target_position:
        return RankingAudit(valid=False, reason="target_missing_from_ranked_candidates")
    if target_score <= 0.0:
        return RankingAudit(
            valid=False,
            reason="target_score_not_positive",
            score_key=score_key,
            target_ordinal_rank=target_position,
            target_score=target_score,
        )

    # ProductService sorts by this score and then assigns an ordinal. Equal
    # scores retain arrival order, which is intentionally nondeterministic when
    # provider previews finish concurrently. Competition rank applies the gate
    # to the target's score tier without treating its arbitrary ordinal inside
    # that tie cohort as a relevance regression.
    strictly_higher_scored_count = sum(score > target_score for score in scores)
    equal_scored_count = sum(score == target_score for score in scores)
    return RankingAudit(
        valid=True,
        reason="score_order_and_rank_sequence_valid",
        score_key=score_key,
        target_ordinal_rank=target_position,
        target_competition_rank=strictly_higher_scored_count + 1,
        target_score=target_score,
        strictly_higher_scored_count=strictly_higher_scored_count,
        equal_scored_count=equal_scored_count,
    )


def _target_ranking_meets_threshold(audit: RankingAudit, *, threshold: int) -> bool:
    return bool(
        audit.valid
        and type(threshold) is int
        and threshold > 0
        and audit.target_competition_rank > 0
        and audit.target_competition_rank <= threshold
    )


def _match_negative(candidate: dict[str, object], control: NegativeControl) -> bool:
    if control.external_id:
        candidate_external_id = str(candidate.get("external_id") or candidate.get("listing_id") or "").strip()
        if candidate_external_id == control.external_id:
            return True
    if control.canonical_url:
        candidate_url = _normalize_url(candidate.get("property_url") or candidate.get("review_url") or candidate.get("packet_url"))
        if candidate_url and candidate_url == _normalize_url(control.canonical_url):
            return True
    if control.source_ref and str(candidate.get("source_ref") or "").strip() == control.source_ref:
        return True
    if control.title and _normalize_text(candidate.get("title")) == _normalize_text(control.title):
        return True
    return False


def _generated_media_counters(summary: dict[str, object]) -> dict[str, int]:
    counters: dict[str, int] = {}
    for key in GENERATED_MEDIA_COUNTER_KEYS:
        try:
            counters[key] = int(summary.get(key) or 0)
        except Exception:
            counters[key] = 0
    return counters


def _assert_no_generated_media(summary: dict[str, object]) -> dict[str, int]:
    counters = _generated_media_counters(summary)
    leaking = {key: value for key, value in counters.items() if value > 0}
    if leaking:
        raise AssertionError(
            "target-recovery canary triggered generated media side effects: "
            + ", ".join(f"{key}={value}" for key, value in sorted(leaking.items()))
        )
    return counters


def _list_repair_tasks(client, *, limit: int = 200) -> list[dict[str, object]]:
    response = client.get("/v1/human/tasks", params={"limit": limit})
    assert response.status_code == 200, response.text
    payload = response.json()
    rows = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _normalize_human_task_id(value: object) -> str:
    normalized = str(value or "").strip()
    return normalized.removeprefix("human_task:")


def _current_run_repair_task_ids(status_payload: dict[str, object], *, run_id: str) -> set[str]:
    normalized_run_id = str(run_id or "").strip()
    status_run_id = str(status_payload.get("run_id") or "").strip()
    if not normalized_run_id or status_run_id != normalized_run_id:
        return set()
    summary = dict(status_payload.get("summary") or {}) if isinstance(status_payload.get("summary"), dict) else {}
    task_rows = [
        dict(row)
        for row in list(summary.get("provider_repair_tasks") or [])
        if isinstance(row, dict)
    ]
    for source in [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]:
        task_rows.extend(
            dict(row)
            for row in list(source.get("provider_repair_tasks") or [])
            if isinstance(row, dict)
        )
    task_ids = {
        normalized
        for row in task_rows
        for normalized in (
            _normalize_human_task_id(
                row.get("human_task_id") or row.get("task_id") or row.get("queue_item_ref")
            ),
        )
        if normalized
    }
    for row in [dict(item) for item in list(summary.get("repair_receipts") or []) if isinstance(item, dict)]:
        if str(row.get("run_id") or "").strip() != normalized_run_id:
            continue
        normalized = _normalize_human_task_id(
            row.get("human_task_id") or row.get("task_id") or row.get("queue_item_ref")
        )
        if normalized:
            task_ids.add(normalized)
    return task_ids


def _matching_repair_tasks(
    tasks: list[dict[str, object]],
    case: TargetListing,
    *,
    baseline_task_ids: set[str],
    run_id: str = "",
    status_payload: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    matches: list[dict[str, object]] = []
    normalized_target_url = _normalize_url(case.canonical_url)
    normalized_run_id = str(run_id or "").strip()
    normalized_baseline_task_ids = {
        _normalize_human_task_id(task_id)
        for task_id in baseline_task_ids
        if _normalize_human_task_id(task_id)
    }
    current_run_task_ids = _current_run_repair_task_ids(
        dict(status_payload or {}),
        run_id=normalized_run_id,
    )
    for task in tasks:
        task_id = str(task.get("human_task_id") or "").strip()
        if str(task.get("task_type") or "").strip() != "property_provider_repair_ooda":
            continue
        normalized_task_id = _normalize_human_task_id(task_id)
        if normalized_task_id and normalized_task_id in current_run_task_ids:
            matches.append(task)
            continue
        if normalized_task_id in normalized_baseline_task_ids:
            continue
        input_json = dict(task.get("input_json") or {}) if isinstance(task.get("input_json"), dict) else {}
        task_run_id = str(input_json.get("run_id") or "").strip()
        if normalized_run_id and task_run_id and task_run_id == normalized_run_id:
            matches.append(task)
            continue
        property_url = _normalize_url(input_json.get("property_url") or input_json.get("source_url"))
        if normalized_target_url and property_url and normalized_target_url == property_url:
            matches.append(task)
            continue
        if case.source_ref and str(input_json.get("source_ref") or "").strip() == case.source_ref:
            matches.append(task)
            continue
        if case.title and _normalize_text(input_json.get("title")) == _normalize_text(case.title):
            matches.append(task)
    return matches


def _repair_needed(status_payload: dict[str, object]) -> bool:
    summary = dict(status_payload.get("summary") or {}) if isinstance(status_payload.get("summary"), dict) else {}
    if int(summary.get("provider_repair_task_opened_total") or 0) > 0:
        return True
    for source in _source_rows(status_payload):
        if str(source.get("error") or "").strip():
            return True
        if str(source.get("repair_status") or "").strip():
            return True
        if list(source.get("provider_repair_tasks") or []):
            return True
    return False


def _apply_repair_receipts_to_trace(repair_trace: RepairTrace, summary: dict[str, object], *, run_id: str) -> None:
    receipts = [
        dict(row)
        for row in list(summary.get("repair_receipts") or [])
        if isinstance(row, dict)
        and (not str(run_id or "").strip() or str(row.get("run_id") or "").strip() == str(run_id or "").strip())
    ]
    if not receipts:
        return
    repair_trace.repair_needed = True
    repair_trace.repair_triggered = True
    repair_trace.repair_executed = True
    repair_trace.task_ids = [
        str(row.get("human_task_id") or "").strip()
        for row in receipts
        if str(row.get("human_task_id") or "").strip()
    ] or repair_trace.task_ids
    repair_trace.statuses = ["returned" for _ in receipts]
    repair_trace.resolutions = [
        str(row.get("resolution") or "").strip()
        for row in receipts
        if str(row.get("resolution") or "").strip()
    ] or repair_trace.resolutions


def _target_recovery_summary_diagnostics(status_payload: dict[str, object]) -> dict[str, object]:
    summary = dict(status_payload.get("summary") or {}) if isinstance(status_payload.get("summary"), dict) else {}
    retained = {
        str(key): value
        for key, value in summary.items()
        if str(key).endswith("_total")
        or str(key)
        in {
            "current_source_candidate_total",
            "current_source_reviewed_total",
            "current_step",
            "progress",
            "repair_receipts",
            "status",
        }
    }
    retained.setdefault("status", str(status_payload.get("status") or "").strip())
    return retained


def _target_recovery_report_run_key(value: object) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in str(value or "").strip()
    ).strip("-_")
    return normalized[:64] or "run"


def _write_report(tmp_path: Path, report: RecoveryReport) -> Path:
    artifact_dir = tmp_path / "target_recovery_reports"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    attempt_number = max(0, int(report.attempt_index)) + 1
    run_key = _target_recovery_report_run_key(report.run_id)
    target = artifact_dir / f"{report.case_key}--attempt-{attempt_number:02d}--{run_key}.json"
    target.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _case_key(case: TargetListing, *, variant: str = "targeted") -> str:
    basis = f"{case.country_code}-{case.provider}-{variant}-{case.title}".lower()
    cleaned = "".join(char if char.isalnum() else "-" for char in basis)
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")[:120] or "target-recovery"


def _provider_seed_text() -> str:
    return str(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_SEED") or "tibor-watch").strip()


def _provider_probe_limit() -> int:
    try:
        return max(1, int(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_PROBE_LIMIT") or 8))
    except Exception:
        return 8


def _provider_pick_window_size(total: int) -> int:
    if total <= 0:
        return 0
    rank_budget = _rank_threshold()
    try:
        configured = int(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_PICK_WINDOW") or rank_budget)
    except Exception:
        configured = rank_budget
    # The canary may only select a target from the provider positions that the
    # product is subsequently required to recover. Sampling a deeper listing
    # and then requiring a top-N result turns provider ordering into a random
    # failure instead of exercising PropertyQuarry's ranking contract.
    return max(1, min(total, configured, rank_budget))


def _ordered_probe_candidates(urls: list[str], *, seed_basis: str) -> list[tuple[int, str]]:
    if not urls:
        return []
    pick_window = _provider_pick_window_size(len(urls))
    start_index = _stable_pick_index(seed_basis, pick_window)
    indexed_window = list(enumerate(urls[:pick_window]))
    rotated = indexed_window[start_index:] + indexed_window[:start_index]
    return rotated[: min(len(rotated), _provider_probe_limit())]


def _country_codes() -> tuple[str, ...]:
    raw = str(
        os.environ.get("PROPERTYQUARRY_TARGET_PROVIDER_MATRIX_COUNTRIES")
        or os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_COUNTRIES")
        or ""
    ).strip()
    if not raw and _target_provider_matrix_enabled():
        return tuple(
            code
            for code in CUSTOMER_SEARCH_COUNTRY_ORDER
            if any(
                str(spec.country_code or "").strip().upper() == code
                and bool(spec.search_ready)
                for spec in PROVIDERS
            )
        )
    if not raw:
        raw = "AT"
    values = tuple(str(item or "").strip().upper() for item in raw.split(",") if str(item or "").strip())
    return values or ("AT",)


def _provider_include_filter() -> set[str]:
    raw = str(
        os.environ.get("PROPERTYQUARRY_TARGET_PROVIDER_MATRIX_PROVIDERS")
        or os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_PROVIDERS")
        or ""
    ).strip()
    return {
        str(item or "").strip().lower()
        for item in raw.split(",")
        if str(item or "").strip()
    }


def _target_recovery_variants() -> tuple[str, ...]:
    raw = str(
        os.environ.get("PROPERTYQUARRY_TARGET_PROVIDER_MATRIX_VARIANTS")
        or os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_VARIANTS")
        or ""
    ).strip()
    if raw:
        values = tuple(
            value
            for value in (
                str(item or "").strip().lower()
                for item in raw.split(",")
                if str(item or "").strip()
            )
            if value in {"targeted", "strict", "soft"}
        )
        if values:
            return tuple(dict.fromkeys(values))
    if _target_provider_matrix_enabled():
        return ("strict", "soft")
    return ("targeted",)


def _variant_soft_filter_mode(variant: str) -> tuple[bool, bool]:
    normalized = str(variant or "").strip().lower()
    if normalized == "strict":
        return False, False
    if normalized == "soft":
        return True, True
    return True, False


def _provider_specs() -> list[PropertyProviderSpec]:
    countries = set(_country_codes())
    include = _provider_include_filter()
    rows = [spec for spec in PROVIDERS if spec.country_code in countries and bool(spec.search_ready)]
    if include:
        rows = [spec for spec in rows if spec.key in include]
    return rows


def _provider_listing_mode(spec: PropertyProviderSpec) -> str:
    modes = tuple(str(item or "").strip().lower() for item in spec.supported_listing_modes if str(item or "").strip())
    if "rent" in modes:
        return "rent"
    if modes:
        return modes[0]
    return "buy"


def _provider_source_spec(spec: PropertyProviderSpec, *, listing_mode: str) -> dict[str, object]:
    source_url = str(spec.search_urls.get(listing_mode) or next(iter(spec.search_urls.values()), "")).strip()
    return {
        "url": source_url,
        "label": f"{spec.label} | {spec.country_code} | {listing_mode.title()}",
        "platform": spec.key,
        "provider_family": spec.family,
        "provider_trust_tier": spec.trust_tier,
        "country_code": spec.country_code,
        "search_url": source_url,
    }


def _preview_price_eur(facts: dict[str, object]) -> float:
    for key in ("price_eur", "rent_eur", "purchase_price_eur", "kaufpreis_eur"):
        value = _coerce_float(facts.get(key))
        if value > 0:
            return value
    return 0.0


def _preview_area_m2(facts: dict[str, object]) -> float:
    for key in ("area_m2", "area_sqm", "living_area_sqm", "wohnflaeche_m2", "living_area_m2"):
        value = _coerce_float(facts.get(key))
        if value > 0:
            return value
    return 0.0


def _preview_rooms(facts: dict[str, object]) -> float:
    for key in ("rooms", "room_count", "zimmer"):
        value = _coerce_float(facts.get(key))
        if value > 0:
            return value
    return 0.0


def _preview_location_query(facts: dict[str, object], *, source_label: str) -> str:
    for key in ("postal_name", "district", "location", "street_address", "source_scope_location"):
        value = str(facts.get(key) or "").strip()
        if value:
            return value
    return ""


def _title_location_hint(*, title: str, summary: str, property_url: str) -> tuple[str, str, str]:
    for blob in (str(title or "").strip(), str(summary or "").strip(), urllib.parse.unquote(str(property_url or "").strip())):
        if not blob:
            continue
        postal_match = re.search(r"\((\d{4}\s+[^\)]+)\)", blob)
        if postal_match:
            location = " ".join(str(postal_match.group(1) or "").split()).strip()
            postal_code = location.split(" ", 1)[0] if " " in location else ""
            district_hint = location.split(" ", 1)[1].strip() if " " in location else location
            return location, district_hint, postal_code
    return "", "", ""


def _preview_property_type(facts: dict[str, object], *, title: str, summary: str) -> str:
    normalized = str(facts.get("property_type") or "").strip().lower()
    if normalized in {"apartment", "house", "land", "office"}:
        return normalized
    blob = _normalize_text(" ".join([title, summary, normalized]))
    if re.search(r"\b(reihenhaus|einfamilienhaus|haus|house)\b", blob):
        return "house"
    if re.search(r"\b(baugrund|grundstück|grundstueck|plot of land|land for sale)\b", blob):
        return "land"
    if re.search(
        r"\b(büro|buero|office|gewerbe|lager|logistik(?:fläche|flaeche)?|halle)\b",
        blob,
    ):
        return "office"
    return "apartment"


def _preview_listing_mode(
    *,
    property_url: str,
    title: str,
    summary: str,
    facts: dict[str, object],
    fallback: str,
) -> str:
    evidence = property_service_module._property_candidate_listing_mode_evidence(
        property_url=property_url,
        title=title,
        summary=summary,
        property_facts=facts,
    )
    rent_signal = bool(
        evidence.get("rent_text_signal") or evidence.get("rent_fact_signal")
    )
    buy_signal = bool(
        evidence.get("buy_text_signal") or evidence.get("buy_fact_signal")
    )
    if buy_signal and not rent_signal:
        return "buy"
    if rent_signal and not buy_signal:
        return "rent"
    return fallback


def _preview_title_is_generic_portal(title: str, *, source_label: str) -> bool:
    normalized = _normalize_text(title)
    if not normalized:
        return True
    source = _normalize_text(source_label)
    generic_fragments = (
        "portal obwieszczeń",
        "portal obwieszczen",
        "portal obwieszczen i licytacji",
        "portal obwieszczeń i licytacji",
        "real estate search",
        "property search",
        "suchergebnisse",
        "immobiliensuche",
    )
    if any(fragment in normalized for fragment in generic_fragments):
        return True
    if "portal" in normalized and ("licytac" in normalized or "obwieszc" in normalized):
        return True
    source_tokens = {token for token in source.split() if len(token) >= 5}
    title_tokens = {token for token in normalized.split() if len(token) >= 5}
    if source_tokens and title_tokens and title_tokens.issubset(source_tokens):
        return True
    return False


def _preview_is_probe_usable(*, property_url: str, preview: dict[str, object], source_label: str) -> bool:
    title = str(preview.get("title") or "").strip()
    summary = str(preview.get("summary") or "").strip()
    facts = dict(preview.get("property_facts_json") or {}) if isinstance(preview.get("property_facts_json"), dict) else {}
    location_query = _preview_location_query(facts, source_label=source_label)
    if not location_query:
        location_query, _district_hint, _postal_hint = _title_location_hint(
            title=title,
            summary=summary,
            property_url=property_url,
        )
    if not title or title == property_url:
        return False
    compact_title = re.sub(r"[^a-z0-9äöüß]+", "", title.lower())
    if len(compact_title) < 8:
        return False
    normalized_title = _normalize_text(title)
    normalized_source_label = _normalize_text(source_label)
    if normalized_title == normalized_source_label:
        return False
    if _preview_title_is_generic_portal(title, source_label=source_label):
        return False
    title_tokens = {token for token in normalized_title.split() if len(token) >= 4}
    source_tokens = {token for token in normalized_source_label.split() if len(token) >= 4}
    if title_tokens and source_tokens and title_tokens.issubset(source_tokens):
        return False
    if not location_query:
        return False
    if _normalize_text(location_query) == _normalize_text(source_label):
        return False
    if _preview_price_eur(facts) <= 0 and _preview_area_m2(facts) <= 0 and not summary:
        return False
    return True


def _discover_target_case_for_provider(
    service: ProductService,
    spec: PropertyProviderSpec,
    *,
    manifest_override: dict[str, object] | None,
) -> TargetListing | None:
    listing_mode = _provider_listing_mode(spec)
    source_spec = _provider_source_spec(spec, listing_mode=listing_mode)
    source_url = str(source_spec.get("url") or "").strip()
    if not source_url:
        return None
    try:
        listing_urls, _cache_state = property_service_module._property_scout_listing_urls_for_source(
            source_url=source_url,
            source_spec=source_spec,
            force_refresh=False,
        )
    except Exception:
        return None
    urls = [str(item or "").strip() for item in listing_urls if str(item or "").strip()]
    if not urls:
        return None
    seed = _provider_seed_text()
    ordered_candidates = _ordered_probe_candidates(
        urls,
        seed_basis=f"{seed}|{spec.country_code}|{spec.key}|{len(urls)}",
    )
    if not ordered_candidates:
        return None
    chosen_url = ""
    chosen_preview: dict[str, object] = {}
    chosen_pool_index = 0
    for candidate_index, candidate_url in ordered_candidates:
        try:
            preview = property_service_module._property_scout_page_preview_with_timeout(candidate_url, prefer_fast=True)
        except Exception:
            continue
        if _preview_is_probe_usable(property_url=candidate_url, preview=preview, source_label=str(source_spec.get("label") or "").strip()):
            chosen_url = candidate_url
            chosen_preview = dict(preview)
            chosen_pool_index = candidate_index
            break
    if not chosen_url or not chosen_preview:
        return None
    facts = dict(chosen_preview.get("property_facts_json") or {}) if isinstance(chosen_preview.get("property_facts_json"), dict) else {}
    source_label = str(source_spec.get("label") or "").strip()
    location_query = _preview_location_query(facts, source_label=source_label)
    title_location_query, title_district_hint, title_postal_hint = _title_location_hint(
        title=str(chosen_preview.get("title") or "").strip(),
        summary=str(chosen_preview.get("summary") or "").strip(),
        property_url=chosen_url,
    )
    if not location_query:
        location_query = title_location_query
    payload: dict[str, object] = {
        "canonical_url": chosen_url,
        "title": str(chosen_preview.get("title") or "").strip(),
        "listing_mode": _preview_listing_mode(
            property_url=chosen_url,
            title=str(chosen_preview.get("title") or "").strip(),
            summary=str(chosen_preview.get("summary") or "").strip(),
            facts=facts,
            fallback=listing_mode,
        ),
        "property_type": _preview_property_type(
            facts,
            title=str(chosen_preview.get("title") or "").strip(),
            summary=str(chosen_preview.get("summary") or "").strip(),
        ),
        "location_query": location_query,
        "external_id": str(chosen_preview.get("listing_id") or facts.get("listing_id") or "").strip(),
        "source_ref": str(chosen_preview.get("listing_id") or chosen_url).strip(),
        "district_hint": str(facts.get("district") or title_district_hint or "").strip(),
        "postal_hint": str(facts.get("postal_name") or title_postal_hint or "").strip(),
        "price_eur": _preview_price_eur(facts),
        "area_m2": _preview_area_m2(facts),
        "rooms": _preview_rooms(facts),
        "soft_preferences": {},
        "selected_districts": [],
        "negatives": [],
    }
    if isinstance(manifest_override, dict):
        for key in ("soft_preferences", "selected_districts", "negatives"):
            if key in manifest_override and key not in payload:
                payload[key] = manifest_override[key]
        if isinstance(manifest_override.get("soft_preferences"), dict):
            payload["soft_preferences"] = dict(manifest_override.get("soft_preferences") or {})
        if isinstance(manifest_override.get("selected_districts"), list):
            payload["selected_districts"] = list(manifest_override.get("selected_districts") or [])
        if isinstance(manifest_override.get("negatives"), list):
            payload["negatives"] = list(manifest_override.get("negatives") or [])
    return _target_listing_from_payload(
        payload,
        provider=spec.key,
        country_code=spec.country_code,
        selected_platforms=(spec.key,),
        pool_size=len(urls),
        picked_index=chosen_pool_index,
    )


def _discover_cases_from_catalog(client) -> tuple[list[TargetListing], list[str]]:
    service = ProductService(client.app.state.container)
    overrides = {
        case.provider: case
        for case in _load_cases()
        if case.provider
    }
    discovered: list[TargetListing] = []
    skipped: list[str] = []
    for spec in _provider_specs():
        override_case = overrides.get(spec.key)
        override_payload = asdict(override_case) if override_case is not None else {}
        case = _discover_target_case_for_provider(service, spec, manifest_override=override_payload)
        if case is None:
            skipped.append(spec.key)
            continue
        discovered.append(case)
    return discovered, skipped


def _probe_error(
    stage: str,
    detail: object,
    *,
    source_index: int | None = None,
    source_url: str = "",
) -> dict[str, object]:
    return {
        "stage": stage,
        "source_index": source_index,
        "source_url": source_url,
        "error": f"{type(detail).__name__}: {detail}" if isinstance(detail, BaseException) else str(detail),
    }


def _probe_no_store_spec(spec: dict[str, object]) -> dict[str, object]:
    clone = dict(spec)
    clone.pop("provider_cache_key", None)
    pushdown = clone.get("provider_filter_pushdown")
    if isinstance(pushdown, dict):
        clone["provider_filter_pushdown"] = {key: value for key, value in pushdown.items() if key != "cache_key"}
    return clone


def _exact_search_probe(
    case: TargetListing,
    brief: dict[str, object],
    *,
    principal_id: str = "propertyquarry-target-recovery-probe",
) -> ExactSearchProbe:
    probe = ExactSearchProbe(state=PROBE_UNKNOWN)
    try:
        generated_specs = property_service_module.generated_property_source_specs(
            preferences=brief,
            selected_platforms=tuple(case.selected_platforms or ((case.provider,) if case.provider else ())),
            principal_id=principal_id,
            default_person_id="self",
            max_results=int(brief.get("max_results_per_source") or 5),
        )
        specs = [dict(spec) if isinstance(spec, dict) else {} for spec in list(generated_specs or [])]
        probe.scan_cap_per_source = max(0, int(property_service_module._property_search_scan_cap_per_source()))
    except Exception as exc:
        probe.reason = "probe_setup_failed"
        probe.errors.append(_probe_error("probe_setup", exc))
        return probe

    probe.source_identities = [str(spec.get("url") or "").strip() for spec in specs]
    if not specs:
        probe.reason = "no_generated_sources"
        probe.errors.append(_probe_error("generate_source_specs", "no generated source specs"))
        return probe

    for source_index, spec in enumerate(specs):
        source_url = str(spec.get("url") or "").strip()
        if not source_url:
            probe.errors.append(_probe_error("source_spec", "missing URL", source_index=source_index))
            continue
        try:
            listing_urls, _cache_state = property_service_module._property_scout_listing_urls_for_source(
                source_url=source_url,
                source_spec=_probe_no_store_spec(spec),
                force_refresh=True,
            )
            raw_urls = list(listing_urls or [])
        except Exception as exc:
            probe.errors.append(_probe_error("source_list", exc, source_index=source_index, source_url=source_url))
            continue
        probe.successful_source_count += 1
        probe.raw_url_count += len(raw_urls)
        if not raw_urls:
            probe.errors.append(
                _probe_error("empty_source_list", "no listing URLs", source_index=source_index, source_url=source_url)
            )
            continue
        scanned_urls = raw_urls if probe.scan_cap_per_source == 0 else raw_urls[: probe.scan_cap_per_source]
        probe.scanned_url_count += len(scanned_urls)
        for target_index, item in enumerate(raw_urls):
            try:
                matches_target = _match_candidate({"property_url": item}, case).matched
            except Exception as exc:
                if target_index < len(scanned_urls):
                    probe.errors.append(
                        _probe_error("match_listing_url", exc, source_index=source_index, source_url=source_url)
                    )
                continue
            if matches_target:
                within_window = target_index < len(scanned_urls)
                if within_window or probe.target_index is None:
                    probe.target_source_index = source_index
                    probe.target_index = target_index
                    probe.target_url = str(item or "").strip()
                if within_window:
                    probe.state = PROBE_PRESENT
                    probe.reason = "target_in_scan_window"
                    return probe

    if probe.target_index is not None:
        probe.state = PROBE_OUT_OF_WINDOW
        probe.reason = "target_beyond_scan_cap"
    elif probe.errors or probe.successful_source_count != len(specs):
        probe.reason = "incomplete_source_evidence"
    else:
        probe.state = PROBE_ABSENT
        probe.reason = "target_absent_from_source_windows"
    return probe


def _provider_volatility_allowed(
    initial_probe: ExactSearchProbe,
    final_probe: ExactSearchProbe,
) -> bool:
    return bool(
        initial_probe.state == PROBE_PRESENT
        and bool(initial_probe.source_identities)
        and initial_probe.source_identities == final_probe.source_identities
        and final_probe.state == PROBE_ABSENT
    )


def _probe_diagnostics(
    initial_probe: ExactSearchProbe,
    final_probe: ExactSearchProbe | None = None,
) -> str:
    return json.dumps(
        {"initial_exact_probe": asdict(initial_probe), "final_exact_probe": asdict(final_probe) if final_probe else None},
        ensure_ascii=False, sort_keys=True,
    )


def _target_recovery_run_timeout_diagnostics(
    status_payload: dict[str, object],
    *,
    elapsed_seconds: float,
    scan_cap_per_source: int,
) -> str:
    summary = (
        dict(status_payload.get("summary") or {})
        if isinstance(status_payload.get("summary"), dict)
        else {}
    )
    events = [
        dict(row)
        for row in list(status_payload.get("events") or [])
        if isinstance(row, dict)
    ]
    return json.dumps(
        {
            "timeout_kind": "bounded_timeout",
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 2),
            "scan_cap_per_source": int(scan_cap_per_source),
            "status": str(status_payload.get("status") or "").strip(),
            "progress": int(status_payload.get("progress") or summary.get("progress") or 0),
            "current_step": str(
                status_payload.get("current_step") or summary.get("current_step") or ""
            ).strip(),
            "updated_at": str(
                status_payload.get("updated_at") or summary.get("updated_at") or ""
            ).strip(),
            "sources_total": int(summary.get("sources_total") or 0),
            "raw_listing_total": int(
                summary.get("raw_listing_total") or summary.get("listing_total") or 0
            ),
            "current_source_reviewed_total": int(
                summary.get("current_source_reviewed_total") or 0
            ),
            "current_source_candidate_total": int(
                summary.get("current_source_candidate_total") or 0
            ),
            "recent_events": events[-5:],
        },
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _target_recovery_scan_cap(case: TargetListing, *, rank_threshold: int) -> int:
    # Discovery only chooses targets from the rank window this canary promises
    # to recover. The exact no-store probe refreshes the provider list after
    # discovery, so reserve one adjacent slot for an insertion at the window
    # boundary while keeping the scan tightly bounded instead of falling back
    # to the product-wide 80-listing background-search default.
    return max(1, int(rank_threshold) + 1, int(case.picked_index) + 1)


def _target_recovery_adaptive_scan_cap(
    probe: ExactSearchProbe,
    *,
    current_cap: int,
    adaptive_limit: int,
) -> int:
    if probe.state != PROBE_OUT_OF_WINDOW or probe.target_index is None:
        return current_cap
    required_cap = int(probe.target_index) + 1
    if required_cap > max(current_cap, adaptive_limit):
        return current_cap
    return max(current_cap, required_cap)


def test_target_recovery_scan_cap_covers_rank_window_and_target_position() -> None:
    case = _target_listing_from_payload(
        {
            "canonical_url": "https://www.willhaben.at/iad/object?adId=123456",
            "title": "Target listing",
            "listing_mode": "rent",
            "property_type": "apartment",
            "location_query": "Wien",
        },
        provider="willhaben",
        country_code="AT",
        selected_platforms=("willhaben",),
        pool_size=80,
        picked_index=3,
    )

    assert _target_recovery_scan_cap(case, rank_threshold=5) == 6

    case.picked_index = 7
    assert _target_recovery_scan_cap(case, rank_threshold=5) == 8


def test_neutral_review_bonus_decays_without_penalizing_personal_fit() -> None:
    early_neutral = property_service_module._property_scout_rank_score(
        property_url="https://example.test/early",
        assessment={"fit_score": 50.0},
        preview={
            "title": "Early provider result",
            "property_facts_json": {"postal_name": "1100 Wien"},
        },
        ordinal=1,
    )
    deep_review_ready = property_service_module._property_scout_rank_score(
        property_url="https://example.test/deep",
        assessment={"fit_score": 50.0},
        preview={
            "title": "Deep result with floor plan",
            "property_facts_json": {
                "has_floorplan": True,
                "floorplan_count": 1,
                "area_m2": 85.0,
                "postal_name": "1100 Wien",
            },
        },
        ordinal=20,
    )
    deep_personal_match = property_service_module._property_scout_rank_score(
        property_url="https://example.test/deep-personal",
        assessment={"fit_score": 82.0},
        preview={"title": "Strong personal match", "property_facts_json": {}},
        ordinal=20,
    )

    assert early_neutral == 52.0
    assert deep_review_ready == 50.0
    assert deep_personal_match == 82.0
    assert deep_personal_match > early_neutral > deep_review_ready


def test_encoded_listing_metadata_recovers_commercial_property_type() -> None:
    encoded_title = (
        "Gro&#xDF;z&#xFC;gige Lager- und Logistikfl&#xE4;che "
        "mit 594 m&#xB2; in Wien"
    )
    extracted_title = property_service_module._property_scout_extract_meta_content(
        f'<meta property="og:title" content="{encoded_title}">',
        "og:title",
    )

    assert extracted_title == "Großzügige Lager- und Logistikfläche mit 594 m² in Wien"
    assert _preview_property_type({}, title=encoded_title, summary="") == "office"


@pytest.mark.parametrize(
    ("listing_text", "postal_name"),
    (
        ("Büro in 2340 Mödling HIER | 2340 Mödling HIER", "2340 Mödling"),
        (
            "Eigentumswohnung in 2700 Wiener Neustadt zu kaufen",
            "2700 Wiener Neustadt",
        ),
    ),
)
def test_listing_postal_evidence_removes_portal_call_to_action(
    listing_text: str,
    postal_name: str,
) -> None:
    evidence = property_service_module._property_postal_location_evidence(listing_text)

    assert evidence
    assert evidence[0]["postal_name"] == postal_name


def test_wohnnet_detail_is_not_generic_and_uses_observed_buy_mode() -> None:
    property_url = (
        "https://www.wohnnet.at/immobilien/"
        "eigentumswohnung-6382-kirchdorf-in-tirol-kauf-4-zimmer-297257291"
    )
    title = "K5 Top 4 - Obergeschosswohnung in exklusivem Neubau"
    summary = "4 Zimmer Eigentumswohnung in 6382 Kirchdorf in Tirol zu kaufen"
    facts = {"postal_name": "6382 Kirchdorf in Tirol", "rooms": 4.0}

    assert not property_service_module._property_candidate_is_generic_listing_page(
        property_url=property_url,
        title=title,
        summary=summary,
        property_facts=facts,
    )
    assert property_service_module._property_candidate_is_generic_listing_page(
        property_url="https://www.wohnnet.at/immobilien/",
        title="Top-Immobilien auf wohnnet.at finden",
        summary="Immobiliensuche in Österreich",
        property_facts={},
    )
    assert _preview_listing_mode(
        property_url=property_url,
        title=title,
        summary=summary,
        facts=facts,
        fallback="rent",
    ) == "buy"


def test_wiener_neustadt_exact_postal_scope_is_not_treated_as_vienna() -> None:
    property_url = (
        "https://www.wohnnet.at/immobilien/"
        "halle-2700-wiener-neustadt-miete-297504551"
    )

    assert property_service_module._property_candidate_matches_requested_location(
        location_hints=("2700 Wiener Neustadt",),
        property_url=property_url,
        title="Großzügige Lager- und Logistikfläche mit 594 m²",
        summary="Halle in 2700 Wiener Neustadt HIER",
        property_facts={
            "postal_name": "2700 Wiener Neustadt",
            "listing_postal_evidence": [
                {"postal_code": "2700", "postal_name": "2700 Wiener Neustadt"}
            ],
        },
        country_code="AT",
    )


def test_target_recovery_adaptive_scan_cap_tracks_bounded_provider_reordering() -> None:
    out_of_window = ExactSearchProbe(
        state=PROBE_OUT_OF_WINDOW,
        target_index=12,
    )
    assert _target_recovery_adaptive_scan_cap(
        out_of_window,
        current_cap=6,
        adaptive_limit=80,
    ) == 13

    out_of_window.target_index = 80
    assert _target_recovery_adaptive_scan_cap(
        out_of_window,
        current_cap=6,
        adaptive_limit=80,
    ) == 6

    present = ExactSearchProbe(state=PROBE_PRESENT, target_index=12)
    assert _target_recovery_adaptive_scan_cap(
        present,
        current_cap=6,
        adaptive_limit=80,
    ) == 6


def test_target_recovery_reports_keep_attempts_and_stage_diagnostics(tmp_path: Path) -> None:
    status_payload: dict[str, object] = {
        "status": "processed",
        "summary": {
            "status": "processed",
            "raw_listing_total": 30,
            "filtered_generic_page_total": 1,
            "ranked_candidates": [{"title": "Unrelated candidate"}],
            "sources": [
                {
                    "source_label": "Willhaben",
                    "listing_total": 6,
                    "filtered_generic_page_total": 1,
                    "provider_cache": {"status": "miss"},
                }
            ],
        },
        "events": [
            {
                "step": "source_generic_page_filter",
                "message": "Suppressed a generic listing page.",
            }
        ],
    }
    summary_diagnostics = _target_recovery_summary_diagnostics(status_payload)
    source_summaries = _source_rows(status_payload)
    recent_events = [dict(row) for row in list(status_payload.get("events") or []) if isinstance(row, dict)][-20:]

    def _report(*, attempt_index: int, run_id: str) -> RecoveryReport:
        return RecoveryReport(
            case_key="at-willhaben-targeted-recovery",
            principal_id="cf-email:test@example.com",
            run_id=run_id,
            watch_url=f"/app/properties?run_id={run_id}",
            provider="willhaben",
            target_title="Target listing",
            target_url="https://www.willhaben.at/iad/immobilien/d/demo-1545890000/",
            pool_size=30,
            picked_index=1,
            run_status="processed",
            target_found=False,
            target_match_tier="",
            target_rank=0,
            target_ranking=RankingAudit(valid=False, reason="target_missing_from_ranked_candidates"),
            repair=RepairTrace(),
            generated_media_counters={},
            negative_hits=[],
            ranked_count=1,
            attempt_index=attempt_index,
            source_count=1,
            event_count=1,
            synthesized_brief={"country_code": "AT"},
            initial_exact_probe=ExactSearchProbe(state=PROBE_PRESENT),
            summary_diagnostics=summary_diagnostics,
            source_summaries=source_summaries,
            recent_events=recent_events,
        )

    first_path = _write_report(tmp_path, _report(attempt_index=0, run_id="run/one"))
    second_path = _write_report(tmp_path, _report(attempt_index=1, run_id="run/two"))

    assert first_path.name == "at-willhaben-targeted-recovery--attempt-01--run-one.json"
    assert second_path.name == "at-willhaben-targeted-recovery--attempt-02--run-two.json"
    assert first_path.exists()
    assert second_path.exists()
    first_payload = json.loads(first_path.read_text(encoding="utf-8"))
    assert first_payload["summary_diagnostics"]["filtered_generic_page_total"] == 1
    assert "ranked_candidates" not in first_payload["summary_diagnostics"]
    assert first_payload["source_summaries"][0]["provider_cache"]["status"] == "miss"
    assert first_payload["recent_events"][0]["step"] == "source_generic_page_filter"


def test_property_target_recovery_canary_under_tibor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    principal = PrincipalContext(
        principal_id=str(
            os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_PRINCIPAL_ID")
            or "cf-email:tibor.girschele@gmail.com"
        ).strip()
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_SEARCH_PROVIDER_WORKER_CONCURRENCY",
        str(os.environ.get("PROPERTYQUARRY_SEARCH_PROVIDER_WORKER_CONCURRENCY") or "8"),
    )
    monkeypatch.setattr(
        ProductService,
        "_maybe_auto_create_property_scout_tour",
        lambda self, **kwargs: {"status": "skipped", "reason": "target_recovery_canary_media_disabled", "tour_url": "", "blocked_reason": "target_recovery_canary_media_disabled"},
    )
    monkeypatch.setattr(
        ProductService,
        "_maybe_render_property_scout_flythrough",
        lambda self, **kwargs: {"status": "skipped", "reason": "target_recovery_canary_media_disabled", "video_url": ""},
    )
    client = build_property_client(principal_id=principal.principal_id)
    start_workspace(client, mode="personal", workspace_name=principal.workspace_name)
    cases, skipped_providers = _discover_cases_from_catalog(client)
    if skipped_providers:
        sys.stderr.write(
            "[target-recovery] skipped provider probes: "
            + ", ".join(sorted(skipped_providers))
            + "\n"
        )
        sys.stderr.flush()
    if not cases:
        pytest.skip("no unattended provider probes produced a usable target listing")
    timeout_seconds = max(float(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_TIMEOUT_SECONDS") or 180.0), 10.0)
    poll_interval = max(float(os.environ.get("PROPERTYQUARRY_TARGET_RECOVERY_POLL_SECONDS") or 1.0), 0.1)

    ProductService(client.app.state.container).update_property_alert_policy(
        principal_id=principal.principal_id,
        actor="target_recovery_canary",
        auto_generate_tour_for_good_fit=False,
    )
    successful_case_total = 0
    volatility_skipped_total = 0
    variants = _target_recovery_variants()

    for case, variant in [(case, variant) for case in cases for variant in variants]:
        final_report: RecoveryReport | None = None
        soft_filters, default_soft_filters = _variant_soft_filter_mode(variant)
        rank_threshold = _rank_threshold()
        assert rank_threshold > 0, "PROPERTYQUARRY_TARGET_RECOVERY_TARGET_RANK_MAX must be a positive integer"
        scan_cap_per_source = _target_recovery_scan_cap(case, rank_threshold=rank_threshold)
        monkeypatch.setenv("EA_PROPERTY_SEARCH_SCAN_CAP_PER_SOURCE", str(scan_cap_per_source))
        max_attempts = 5
        eligibility_brief = _synthesize_search_preferences(
            case,
            loosen_level=max_attempts - 1,
            soft_filters=soft_filters,
            default_soft_filters=default_soft_filters,
        )
        _assert_brief_satisfies_target(case, eligibility_brief)
        initial_exact_probe = _exact_search_probe(
            case,
            eligibility_brief,
            principal_id=principal.principal_id,
        )
        adaptive_limit = max(
            scan_cap_per_source,
            int(
                os.environ.get(
                    "PROPERTYQUARRY_TARGET_RECOVERY_ADAPTIVE_SCAN_CAP_MAX"
                )
                or 80
            ),
        )
        adaptive_scan_cap = _target_recovery_adaptive_scan_cap(
            initial_exact_probe,
            current_cap=scan_cap_per_source,
            adaptive_limit=adaptive_limit,
        )
        if adaptive_scan_cap > scan_cap_per_source:
            sys.stderr.write(
                "[target-recovery] provider order moved the exact target; "
                f"expanding bounded scan cap for {case.provider} from "
                f"{scan_cap_per_source} to {adaptive_scan_cap}\n"
            )
            sys.stderr.flush()
            scan_cap_per_source = adaptive_scan_cap
            monkeypatch.setenv(
                "EA_PROPERTY_SEARCH_SCAN_CAP_PER_SOURCE",
                str(scan_cap_per_source),
            )
            initial_exact_probe = _exact_search_probe(
                case,
                eligibility_brief,
                principal_id=principal.principal_id,
            )
        assert initial_exact_probe.state == PROBE_PRESENT, (
            f"{case.title}: exact loosest adaptive provider probe gate requires PRESENT before "
            f"starting force_refresh=False recovery runs; {_probe_diagnostics(initial_exact_probe)}"
        )
        for attempt_index in range(max_attempts):
            baseline_task_ids = {
                str(task.get("human_task_id") or "").strip()
                for task in _list_repair_tasks(client)
                if str(task.get("human_task_id") or "").strip()
            }
            brief = _synthesize_search_preferences(
                case,
                loosen_level=attempt_index,
                soft_filters=soft_filters,
                default_soft_filters=default_soft_filters,
            )
            _assert_brief_satisfies_target(case, brief)
            stored = client.post("/v1/onboarding/property-search/preferences", json=brief)
            assert stored.status_code == 200, stored.text

            started = client.post(
                "/app/api/property/search-runs",
                json={
                    "selected_platforms": list(case.selected_platforms),
                    "property_preferences": brief,
                    "force_refresh": False,
                    "max_results_per_source": int(brief.get("max_results_per_source") or 2),
                },
            )
            assert started.status_code == 202, started.text
            started_body = started.json()
            run_id = str(started_body.get("run_id") or "").strip()
            assert run_id, f"{case.title}: run_id missing from start response"
            _print_watch_banner(case, principal, run_id, variant=variant)

            last_status: dict[str, object] = {}
            target_match = IdentityMatch(matched=False)
            repair_trace = RepairTrace()
            run_started_at = time.monotonic()
            deadline = run_started_at + timeout_seconds
            while time.monotonic() < deadline:
                status_response = client.get(f"/app/api/property/search-runs/{run_id}")
                assert status_response.status_code == 200, status_response.text
                last_status = status_response.json()
                summary = dict(last_status.get("summary") or {}) if isinstance(last_status.get("summary"), dict) else {}
                media_counters = _assert_no_generated_media(summary)
                repair_trace.repair_needed = repair_trace.repair_needed or _repair_needed(last_status)
                _apply_repair_receipts_to_trace(repair_trace, summary, run_id=run_id)

                candidates = _candidate_rows(last_status)
                target_match = _match_ranked_target(candidates, case)

                tasks = _matching_repair_tasks(
                    _list_repair_tasks(client),
                    case,
                    baseline_task_ids=baseline_task_ids,
                    run_id=run_id,
                    status_payload=last_status,
                )
                if tasks:
                    repair_trace.repair_triggered = True
                    repair_trace.task_ids = [str(task.get("human_task_id") or "") for task in tasks]
                    repair_trace.statuses = [str(task.get("status") or "").strip() for task in tasks]
                    repair_trace.resolutions = [str(task.get("resolution") or "").strip() for task in tasks]
                    repair_trace.repair_executed = any(
                        str(task.get("status") or "").strip().lower() in {"returned", "completed"}
                        for task in tasks
                    )

                if str(last_status.get("status") or "").strip().lower() in TERMINAL_RUN_STATUSES:
                    break
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0.0:
                    break
                time.sleep(min(poll_interval, remaining_seconds))

            status_value = str(last_status.get("status") or "").strip().lower()
            observed_at = time.monotonic()
            timeout_diagnostics = _target_recovery_run_timeout_diagnostics(
                last_status,
                elapsed_seconds=observed_at - run_started_at,
                scan_cap_per_source=scan_cap_per_source,
            )
            assert status_value in TERMINAL_RUN_STATUSES, (
                f"{case.title}: run did not reach terminal status before bounded timeout; "
                f"{timeout_diagnostics}"
            )
            assert status_value != "failed", f"{case.title}: run failed unrepaired"

            summary = dict(last_status.get("summary") or {}) if isinstance(last_status.get("summary"), dict) else {}
            media_counters = _assert_no_generated_media(summary)
            _apply_repair_receipts_to_trace(repair_trace, summary, run_id=run_id)
            candidates = _candidate_rows(last_status)
            target_match = _match_ranked_target(candidates, case)
            target_ranking = _audit_target_ranking(candidates, case)
            negative_hits: list[dict[str, object]] = []
            for control in case.negatives:
                for candidate in candidates:
                    if _match_negative(candidate, control):
                        negative_hits.append(
                            {
                                "title": str(candidate.get("title") or "").strip(),
                                "property_url": str(candidate.get("property_url") or "").strip(),
                                "rank": int(candidate.get("rank") or 0),
                                "control_title": control.title,
                                "must_not_rank": control.must_not_rank,
                            }
                        )

            final_report = RecoveryReport(
                case_key=_case_key(case, variant=variant),
                principal_id=principal.principal_id,
                run_id=run_id,
                watch_url=_watch_url(run_id),
                provider=case.provider,
                target_title=case.title,
                target_url=case.canonical_url,
                pool_size=case.pool_size,
                picked_index=case.picked_index,
                run_status=status_value,
                target_found=target_match.matched,
                target_match_tier=target_match.tier,
                target_rank=target_match.rank,
                target_ranking=target_ranking,
                repair=repair_trace,
                generated_media_counters=media_counters,
                negative_hits=negative_hits,
                ranked_count=len(candidates),
                attempt_index=attempt_index,
                source_count=len(_source_rows(last_status)),
                event_count=len(list(last_status.get("events") or [])),
                synthesized_brief=brief,
                initial_exact_probe=initial_exact_probe,
                variant=variant,
                summary_diagnostics=_target_recovery_summary_diagnostics(last_status),
                source_summaries=_source_rows(last_status)[:20],
                recent_events=[
                    dict(row)
                    for row in list(last_status.get("events") or [])
                    if isinstance(row, dict)
                ][-20:],
            )
            _write_report(tmp_path, final_report)

            forbidden_hits = [row for row in negative_hits if bool(row.get("must_not_rank", True))]
            if repair_trace.repair_needed:
                assert repair_trace.repair_triggered, f"{case.title}: repair was needed but no Fleet repair task opened"
                assert repair_trace.repair_executed, f"{case.title}: Fleet repair task never executed"
                bad_source_fetch_resolutions = [
                    resolution
                    for resolution in repair_trace.resolutions
                    if str(resolution or "").strip().lower() in INVALID_SOURCE_FETCH_REPAIR_RESOLUTIONS
                ]
                assert not bad_source_fetch_resolutions, (
                    f"{case.title}: Fleet returned a semantically wrong source-fetch repair resolution: "
                    f"{bad_source_fetch_resolutions}"
                )

            if (
                target_match.matched
                and target_match.tier in MATCH_TIERS
                and not forbidden_hits
                and _target_ranking_meets_threshold(target_ranking, threshold=rank_threshold)
            ):
                successful_case_total += 1
                break
        assert final_report is not None
        if not final_report.target_found:
            final_report.final_exact_probe = _exact_search_probe(
                case,
                eligibility_brief,
                principal_id=principal.principal_id,
            )
            _write_report(tmp_path, final_report)
            if _provider_volatility_allowed(initial_exact_probe, final_report.final_exact_probe):
                final_report.provider_volatility = True
                _write_report(tmp_path, final_report)
                volatility_skipped_total += 1
                sys.stderr.write(
                    f"[target-recovery] skipped volatile target for {case.provider}: {case.title}\n"
                )
                sys.stderr.flush()
                continue
        probe_diagnostics = _probe_diagnostics(initial_exact_probe, final_report.final_exact_probe)
        assert final_report.target_found, (
            f"{case.title}: target listing not recovered after adaptive retries; {probe_diagnostics}"
        )
        assert final_report.target_match_tier in MATCH_TIERS, (
            f"{case.title}: unsupported target match tier; {probe_diagnostics}"
        )
        assert final_report.target_ranking.valid, (
            f"{case.title}: ranked result integrity failed "
            f"(reason={final_report.target_ranking.reason}, "
            f"score_key={final_report.target_ranking.score_key or 'unknown'}); {probe_diagnostics}"
        )
        assert _target_ranking_meets_threshold(final_report.target_ranking, threshold=rank_threshold), (
            f"{case.title}: target recovered but not ranked highly enough "
            f"(ordinal_rank={final_report.target_rank}, "
            f"competition_rank={final_report.target_ranking.target_competition_rank}, "
            f"score_key={final_report.target_ranking.score_key}, "
            f"target_score={final_report.target_ranking.target_score}, "
            f"strictly_higher={final_report.target_ranking.strictly_higher_scored_count}, "
            f"equal_score={final_report.target_ranking.equal_scored_count}, "
            f"threshold={rank_threshold}); {probe_diagnostics}"
        )
        forbidden_hits = [row for row in final_report.negative_hits if bool(row.get('must_not_rank', True))]
        assert not forbidden_hits, (
            f"{case.title}: near-miss impostors survived ranking: {forbidden_hits}; {probe_diagnostics}"
        )
    if successful_case_total <= 0 and volatility_skipped_total > 0:
        pytest.skip("all target-recovery cases became provider-volatile before recovery validation")
    assert successful_case_total > 0, "no target-recovery case completed successfully"


def _ranking_audit_candidates(*, scores: list[float], target_position: int) -> list[dict[str, object]]:
    target_url = _exact_probe_test_case().canonical_url
    return [
        {
            "rank": position,
            "ranking_score": score,
            RANKING_AUDIT_SCORE_KEY: "ranking_score",
            "property_url": target_url if position == target_position else f"https://provider.test/listing-{position}",
        }
        for position, score in enumerate(scores, start=1)
    ]


def test_target_ranking_gate_uses_score_competition_rank_for_ties() -> None:
    candidates = _ranking_audit_candidates(
        scores=[92.0] * 20 + [80.0] * 10,
        target_position=14,
    )

    audit = _audit_target_ranking(candidates, _exact_probe_test_case())

    assert audit.valid
    assert audit.target_ordinal_rank == 14
    assert audit.target_competition_rank == 1
    assert audit.strictly_higher_scored_count == 0
    assert audit.equal_scored_count == 20
    assert _target_ranking_meets_threshold(audit, threshold=5)


def test_target_ranking_gate_still_rejects_five_strictly_better_scores() -> None:
    candidates = _ranking_audit_candidates(
        scores=[100.0, 99.0, 98.0, 97.0, 96.0] + [90.0] * 15 + [80.0] * 10,
        target_position=14,
    )

    audit = _audit_target_ranking(candidates, _exact_probe_test_case())

    assert audit.valid
    assert audit.target_ordinal_rank == 14
    assert audit.target_competition_rank == 6
    assert audit.strictly_higher_scored_count == 5
    assert audit.equal_scored_count == 15
    assert not _target_ranking_meets_threshold(audit, threshold=5)


def test_target_ranking_score_uses_declared_key_without_truthy_fallback() -> None:
    candidate = {
        "ranking_score": 0.0,
        "fit_score": 100.0,
    }

    assert _candidate_ranking_score(candidate, score_key="ranking_score") == 0.0
    assert _candidate_ranking_score(candidate, score_key="fit_score") == 100.0


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        pytest.param(
            lambda rows: rows[1].update(
                {
                    RANKING_AUDIT_SCORE_KEY: "fit_score",
                    "fit_score": rows[1]["ranking_score"],
                }
            ),
            "candidate_score_provenance_mixed",
            id="mixed-score-keys",
        ),
        pytest.param(
            lambda rows: rows[1].pop("ranking_score"),
            "candidate_score_missing_or_invalid",
            id="missing-declared-score",
        ),
        pytest.param(
            lambda rows: rows[1].pop(RANKING_AUDIT_SCORE_KEY),
            "candidate_score_provenance_missing",
            id="missing-score-provenance",
        ),
    ],
)
def test_target_ranking_gate_fails_closed_on_score_provenance(
    mutation,
    expected_reason: str,
) -> None:
    candidates = _ranking_audit_candidates(scores=[90.0, 80.0], target_position=2)
    mutation(candidates)

    audit = _audit_target_ranking(candidates, _exact_probe_test_case())

    assert not audit.valid
    assert audit.reason == expected_reason
    assert not _target_ranking_meets_threshold(audit, threshold=5)


@pytest.mark.parametrize("invalid_rank", [True, 1.0, 1.5, "1"])
def test_target_ranking_gate_rejects_non_integer_rank_types(invalid_rank: object) -> None:
    candidates = _ranking_audit_candidates(scores=[90.0], target_position=1)
    candidates[0]["rank"] = invalid_rank

    audit = _audit_target_ranking(candidates, _exact_probe_test_case())

    assert not audit.valid
    assert audit.reason == "candidate_rank_invalid"


@pytest.mark.parametrize("invalid_threshold", [0, -1, True])
def test_target_ranking_gate_rejects_nonpositive_or_boolean_threshold(invalid_threshold: object) -> None:
    audit = _audit_target_ranking(
        _ranking_audit_candidates(scores=[90.0], target_position=1),
        _exact_probe_test_case(),
    )

    assert audit.valid
    assert not _target_ranking_meets_threshold(audit, threshold=invalid_threshold)


@pytest.mark.parametrize(
    ("candidates", "expected_reason"),
    [
        pytest.param(
            [
                {
                    "rank": 1,
                    "ranking_score": 80.0,
                    RANKING_AUDIT_SCORE_KEY: "ranking_score",
                    "property_url": "https://provider.test/other",
                },
                {
                    "rank": 2,
                    "ranking_score": 90.0,
                    RANKING_AUDIT_SCORE_KEY: "ranking_score",
                    "property_url": "https://www.willhaben.at/iad/object?adId=123456",
                },
            ],
            "candidate_scores_not_descending",
            id="score-order",
        ),
        pytest.param(
            [
                {
                    "rank": 1,
                    "ranking_score": 90.0,
                    RANKING_AUDIT_SCORE_KEY: "ranking_score",
                    "property_url": "https://provider.test/other",
                },
                {
                    "rank": 3,
                    "ranking_score": 80.0,
                    RANKING_AUDIT_SCORE_KEY: "ranking_score",
                    "property_url": "https://www.willhaben.at/iad/object?adId=123456",
                },
            ],
            "candidate_ranks_not_sequential",
            id="rank-sequence",
        ),
    ],
)
def test_target_ranking_gate_rejects_broken_rank_integrity(
    candidates: list[dict[str, object]],
    expected_reason: str,
) -> None:
    audit = _audit_target_ranking(candidates, _exact_probe_test_case())

    assert not audit.valid
    assert audit.reason == expected_reason
    assert not _target_ranking_meets_threshold(audit, threshold=5)


def _exact_probe_test_case() -> TargetListing:
    return TargetListing(
        provider="willhaben",
        country_code="AT",
        canonical_url="https://www.willhaben.at/iad/object?adId=123456",
        title="Target flat",
        listing_mode="rent",
        property_type="apartment",
        location_query="1010 Vienna",
        selected_platforms=("willhaben",),
    )


def _stub_exact_search_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    specs: list[dict[str, object]] | None = None,
    listing_urls: dict[str, list[str]] | None = None,
    scan_cap: int = 80,
    source_errors: dict[str, Exception] | None = None,
) -> list[tuple[str, bool]]:
    generated_specs = [{"url": "https://provider.test/search"}] if specs is None else specs
    urls_by_source = listing_urls or {}
    errors_by_source = source_errors or {}
    calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        property_service_module,
        "generated_property_source_specs",
        lambda **kwargs: generated_specs,
    )
    monkeypatch.setattr(property_service_module, "_property_search_scan_cap_per_source", lambda: scan_cap)

    def _source_list(*, source_url: str, source_spec: dict[str, object], force_refresh: bool):
        calls.append((source_url, force_refresh))
        if source_url in errors_by_source:
            raise errors_by_source[source_url]
        return list(urls_by_source.get(source_url, [])), "fresh"

    monkeypatch.setattr(
        property_service_module,
        "_property_scout_listing_urls_for_source",
        _source_list,
    )
    return calls


def test_exact_search_probe_real_wrapper_is_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    case = _exact_probe_test_case()
    source_url = "https://provider.test/search"
    pushdown = {"cache_key": "nested-cache", "listing_mode": "rent", "location_query": "Vienna"}
    original_spec: dict[str, object] = {
        "url": source_url,
        "platform": "willhaben",
        "provider_cache_key": "top-level-cache",
        "provider_filter_pushdown": pushdown,
        "fetch_timeout_seconds": 12,
    }
    captured: dict[str, object] = {}
    monkeypatch.setattr(property_service_module, "generated_property_source_specs", lambda **kwargs: [original_spec])
    monkeypatch.setattr(property_service_module, "_property_search_scan_cap_per_source", lambda: 80)
    monkeypatch.setattr(property_service_module, "_property_scout_fetch_html_compat", lambda *args, **kwargs: "<html />")

    def _extract(*, source_url: str, html: str, source_spec: dict[str, object]) -> tuple[str, ...]:
        captured["source_spec"] = source_spec
        return (case.canonical_url,)

    monkeypatch.setattr(property_service_module, "_property_scout_extract_listing_urls", _extract)
    monkeypatch.setattr(
        property_service_module,
        "_property_source_listing_cache_put",
        lambda *args, **kwargs: pytest.fail("no-store probe wrote listing cache"),
    )

    probe = _exact_search_probe(case, {"max_results_per_source": 5})
    passed_spec = dict(captured["source_spec"])

    assert probe.state == PROBE_PRESENT
    assert probe.source_identities == [source_url]
    assert "provider_cache_key" not in passed_spec
    assert passed_spec["provider_filter_pushdown"] == {"listing_mode": "rent", "location_query": "Vienna"}
    assert passed_spec["fetch_timeout_seconds"] == 12
    assert original_spec["provider_cache_key"] == "top-level-cache"
    assert pushdown["cache_key"] == "nested-cache"


@pytest.mark.parametrize(
    ("specs", "urls", "cap", "errors", "expected"),
    [
        pytest.param(None, {"https://provider.test/search": [*[f"https://provider.test/{i}" for i in range(6)], _exact_probe_test_case().canonical_url]}, 80, None, {"state": PROBE_PRESENT, "target_index": 6, "scanned_url_count": 7}, id="in-window-beyond-max-results"),
        pytest.param(None, {"https://provider.test/search": ["other", "other-2", _exact_probe_test_case().canonical_url]}, 0, None, {"state": PROBE_PRESENT, "target_index": 2, "scanned_url_count": 3}, id="zero-cap-unlimited"),
        pytest.param(None, {}, 80, {"https://provider.test/search": RuntimeError("blocked")}, {"state": PROBE_UNKNOWN, "successful_source_count": 0, "error_stage": "source_list"}, id="source-exception"),
        pytest.param(None, {}, 80, None, {"state": PROBE_UNKNOWN, "successful_source_count": 1, "error_stage": "empty_source_list"}, id="empty-extraction"),
        pytest.param([], {}, 80, None, {"state": PROBE_UNKNOWN, "error_stage": "generate_source_specs"}, id="empty-generated-specs"),
        pytest.param([{}], {}, 80, None, {"state": PROBE_UNKNOWN, "error_stage": "source_spec"}, id="url-less-spec"),
        pytest.param(None, {"https://provider.test/search": ["other", "other-2", _exact_probe_test_case().canonical_url]}, 2, None, {"state": PROBE_OUT_OF_WINDOW, "reason": "target_beyond_scan_cap", "target_index": 2, "scanned_url_count": 2}, id="out-of-window"),
        pytest.param([{"url": "good"}, {"url": "broken"}], {"good": ["other"]}, 80, {"broken": TimeoutError("blocked")}, {"state": PROBE_UNKNOWN, "successful_source_count": 1, "raw_url_count": 1, "scanned_url_count": 1}, id="mixed-negative"),
    ],
)
def test_exact_search_probe_evidence_states(
    monkeypatch: pytest.MonkeyPatch,
    specs: list[dict[str, object]] | None,
    urls: dict[str, list[str]],
    cap: int,
    errors: dict[str, Exception] | None,
    expected: dict[str, object],
) -> None:
    calls = _stub_exact_search_probe(
        monkeypatch,
        specs=specs,
        listing_urls=urls,
        scan_cap=cap,
        source_errors=errors,
    )
    probe = _exact_search_probe(_exact_probe_test_case(), {"max_results_per_source": 5})

    for field_name, value in expected.items():
        if field_name != "error_stage":
            assert getattr(probe, field_name) == value
    if "error_stage" in expected:
        assert probe.errors[0]["stage"] == expected["error_stage"]
    assert all(force_refresh for _source_url, force_refresh in calls)
    if probe.state == PROBE_OUT_OF_WINDOW:
        initial = ExactSearchProbe(state=PROBE_PRESENT, source_identities=probe.source_identities)
        assert not _provider_volatility_allowed(initial, probe)


def test_exact_search_probe_resets_cap_for_each_source(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _exact_probe_test_case().canonical_url
    _stub_exact_search_probe(
        monkeypatch,
        specs=[{"url": "one"}, {"url": "two"}],
        listing_urls={"one": ["1", "2"], "two": ["3", target, "4"]},
        scan_cap=2,
    )
    probe = _exact_search_probe(_exact_probe_test_case(), {"max_results_per_source": 5})

    assert probe.state == PROBE_PRESENT
    assert (probe.raw_url_count, probe.scanned_url_count) == (5, 4)
    assert (probe.target_source_index, probe.target_index) == (1, 1)


def test_exact_search_probe_partial_failure_plus_present(monkeypatch: pytest.MonkeyPatch) -> None:
    target = _exact_probe_test_case().canonical_url
    _stub_exact_search_probe(
        monkeypatch,
        specs=[{"url": "broken"}, {"url": "good"}],
        listing_urls={"good": [target]},
        source_errors={"broken": TimeoutError("blocked")},
    )
    probe = _exact_search_probe(_exact_probe_test_case(), {"max_results_per_source": 5})

    assert probe.state == PROBE_PRESENT
    assert probe.successful_source_count == 1
    assert probe.errors[0]["stage"] == "source_list"


def test_adaptive_eligibility_accepts_attempt_four_when_attempt_zero_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _exact_probe_test_case()
    _stub_exact_search_probe(
        monkeypatch,
        listing_urls={"apartment": ["other"], "any": [case.canonical_url]},
    )
    monkeypatch.setattr(
        property_service_module,
        "generated_property_source_specs",
        lambda *, preferences, **kwargs: [{"url": str(preferences["property_type"])}],
    )
    attempt_zero = _synthesize_search_preferences(case, loosen_level=0)
    attempt_four = _synthesize_search_preferences(case, loosen_level=4)

    assert _exact_search_probe(case, attempt_zero).state == PROBE_ABSENT
    assert _exact_search_probe(case, attempt_four).state == PROBE_PRESENT


@pytest.mark.parametrize(
    ("state", "source_identities", "expected"),
    [
        (PROBE_ABSENT, ["source"], True),
        (PROBE_PRESENT, ["source"], False),
        (PROBE_UNKNOWN, ["source"], False),
        (PROBE_ABSENT, ["changed"], False),
    ],
    ids=("absent", "present", "unknown", "source-mismatch"),
)
def test_provider_volatility_requires_definitive_absence(
    state: str,
    source_identities: list[str],
    expected: bool,
) -> None:
    initial = ExactSearchProbe(state=PROBE_PRESENT, source_identities=["source"])
    final = ExactSearchProbe(
        state=state,
        source_identities=source_identities,
        successful_source_count=1,
    )
    assert _provider_volatility_allowed(initial, final) is expected


def test_ordered_probe_candidates_never_samples_beyond_rank_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_TARGET_RECOVERY_PROBE_LIMIT", "4")
    monkeypatch.setenv("PROPERTYQUARRY_TARGET_RECOVERY_PICK_WINDOW", "6")
    monkeypatch.setenv("PROPERTYQUARRY_TARGET_RECOVERY_TARGET_RANK_MAX", "3")
    urls = [f"https://example.test/{index}" for index in range(12)]
    ordered = _ordered_probe_candidates(urls, seed_basis="AT|willhaben|demo")
    assert len(ordered) == 3
    assert all(index < 3 for index, _url in ordered)
    assert len({index for index, _url in ordered}) == 3


def test_provider_pick_window_size_is_bounded_by_rank_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_TARGET_RECOVERY_PICK_WINDOW", raising=False)
    monkeypatch.setenv("PROPERTYQUARRY_TARGET_RECOVERY_PROBE_LIMIT", "5")
    monkeypatch.setenv("PROPERTYQUARRY_TARGET_RECOVERY_TARGET_RANK_MAX", "5")
    assert _provider_pick_window_size(40) == 5
    assert _provider_pick_window_size(3) == 3

    monkeypatch.setenv("PROPERTYQUARRY_TARGET_RECOVERY_PICK_WINDOW", "2")
    assert _provider_pick_window_size(40) == 2


def test_target_provider_matrix_expands_to_all_search_ready_countries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_TARGET_PROVIDER_MATRIX", "1")
    monkeypatch.delenv("PROPERTYQUARRY_TARGET_PROVIDER_MATRIX_COUNTRIES", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_TARGET_RECOVERY_COUNTRIES", raising=False)
    countries = _country_codes()
    expected = tuple(
        code
        for code in CUSTOMER_SEARCH_COUNTRY_ORDER
        if any(
            str(spec.country_code or "").strip().upper() == code
            and bool(spec.search_ready)
            for spec in PROVIDERS
        )
    )
    assert countries == expected
    assert _target_recovery_variants() == ("strict", "soft")


def test_target_provider_matrix_synthesizes_strict_and_soft_briefs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_TARGET_PROVIDER_MATRIX", "1")
    case = TargetListing(
        provider="willhaben",
        country_code="AT",
        canonical_url="https://www.willhaben.at/iad/object?adId=123",
        title="Target flat",
        listing_mode="rent",
        property_type="apartment",
        location_query="1020 Vienna",
        price_eur=1500.0,
        area_m2=80.0,
        rooms=3.0,
        selected_platforms=("willhaben",),
        soft_preferences={"max_distance_to_theatre_m": 700, "max_distance_to_theatre_importance": "nice_to_have"},
    )
    strict_soft_filters, strict_default_soft_filters = _variant_soft_filter_mode("strict")
    soft_soft_filters, soft_default_soft_filters = _variant_soft_filter_mode("soft")

    strict = _synthesize_search_preferences(
        case,
        soft_filters=strict_soft_filters,
        default_soft_filters=strict_default_soft_filters,
    )
    soft = _synthesize_search_preferences(
        case,
        soft_filters=soft_soft_filters,
        default_soft_filters=soft_default_soft_filters,
    )

    assert strict["selected_platforms"] == ["willhaben"]
    assert soft["selected_platforms"] == ["willhaben"]
    assert "max_distance_to_theatre_m" not in strict
    assert "max_distance_to_library_m" not in strict
    assert soft["max_distance_to_theatre_m"] == 700
    assert soft["max_distance_to_library_importance"] == "nice_to_have"
    assert soft["max_distance_to_shopping_center_importance"] == "avoid"


def test_preview_probe_usable_rejects_provider_label_junk_target() -> None:
    preview = {
        "title": "| Gesiba",
        "summary": "Wohnung in Wien.",
        "property_facts_json": {
            "postal_name": "1100 Wien",
            "area_m2": 63,
        },
    }
    assert _preview_is_probe_usable(
        property_url="https://example.test/gesiba/123",
        preview=preview,
        source_label="GESIBA | Austria | Rent | Vienna",
    ) is False


def test_preview_probe_usable_rejects_generic_auction_portal_target() -> None:
    preview = {
        "title": "Licytacja komornicza - Portal Obwieszczeń i Licytacji Komorniczych",
        "summary": "Portal page instead of a listing-detail extract.",
        "property_facts_json": {
            "postal_name": "00-031 Warszawa",
        },
    }

    assert _preview_is_probe_usable(
        property_url="https://elicytacje.komornik.pl/licytacje/76353/1-8-niewydzielona-czesc-nieruchomosci",
        preview=preview,
        source_label="Komornik e-Licytacje | PL | Buy",
    ) is False


def test_matching_repair_tasks_prefers_run_id_over_provider_broad_match() -> None:
    case = TargetListing(
        provider="willhaben",
        country_code="AT",
        canonical_url="https://www.willhaben.at/iad/object?adId=123",
        title="Target flat",
        listing_mode="rent",
        property_type="apartment",
        location_query="1010 Vienna",
    )
    tasks = [
        {
            "human_task_id": "human_task:other",
            "task_type": "property_provider_repair_ooda",
            "input_json": {
                "run_id": "run-other",
                "source_platform": "willhaben",
                "title": "Different flat",
                "property_url": "https://www.willhaben.at/iad/object?adId=999",
            },
        },
        {
            "human_task_id": "human_task:current",
            "task_type": "property_provider_repair_ooda",
            "input_json": {
                "run_id": "run-123",
                "source_platform": "willhaben",
                "source_url": "https://www.willhaben.at/iad/immobilien/mietwohnungen/wien",
            },
        },
    ]
    matches = _matching_repair_tasks(tasks, case, baseline_task_ids=set(), run_id="run-123")
    assert [str(task.get("human_task_id") or "") for task in matches] == ["human_task:current"]


def test_match_ranked_target_uses_the_current_snapshot_rank() -> None:
    case = TargetListing(
        provider="willhaben",
        country_code="AT",
        canonical_url="https://www.willhaben.at/iad/object?adId=123",
        title="Target flat",
        listing_mode="rent",
        property_type="apartment",
        location_query="1010 Vienna",
        external_id="123",
    )

    early_match = _match_ranked_target([{"external_id": "123", "rank": 6}], case)
    terminal_match = _match_ranked_target([{"external_id": "123", "rank": 4}], case)

    assert early_match.rank == 6
    assert terminal_match.rank == 4


def test_matching_repair_tasks_accepts_reused_baseline_task_only_with_current_run_evidence() -> None:
    case = TargetListing(
        provider="willhaben",
        country_code="AT",
        canonical_url="https://www.willhaben.at/iad/object?adId=123",
        title="Target flat",
        listing_mode="rent",
        property_type="apartment",
        location_query="1010 Vienna",
    )
    tasks = [
        {
            "human_task_id": "reused",
            "task_type": "property_provider_repair_ooda",
            "status": "returned",
            "resolution": "patched_provider_extractor",
            "input_json": {"run_id": "run-original"},
        },
        {
            "human_task_id": "unrelated",
            "task_type": "property_provider_repair_ooda",
            "status": "returned",
            "resolution": "patched_other_provider",
            "input_json": {"run_id": "run-other"},
        },
    ]
    status_payload = {
        "run_id": "run-current",
        "summary": {
            "sources": [
                {
                    "provider_repair_tasks": [
                        {"human_task_id": "human_task:reused", "status": "returned"},
                    ],
                }
            ]
        },
    }

    matches = _matching_repair_tasks(
        tasks,
        case,
        baseline_task_ids={"reused", "unrelated"},
        run_id="run-current",
        status_payload=status_payload,
    )
    without_current_run_evidence = _matching_repair_tasks(
        tasks,
        case,
        baseline_task_ids={"reused", "unrelated"},
        run_id="run-current",
        status_payload={"run_id": "run-other", "summary": status_payload["summary"]},
    )

    assert [str(task.get("human_task_id") or "") for task in matches] == ["reused"]
    assert without_current_run_evidence == []
