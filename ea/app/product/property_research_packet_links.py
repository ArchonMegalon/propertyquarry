from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from itertools import islice
from typing import Any, Iterable, Mapping


PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION = 1
PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION = 3
PROPERTY_RESEARCH_PACKET_MAX_BYTES = 256 * 1024
PROPERTY_RESEARCH_PACKET_MAX_AGGREGATE_BYTES = 16 * 1024 * 1024
PROPERTY_RESEARCH_PACKET_MAX_DEPTH = 12
PROPERTY_RESEARCH_PACKET_MAX_ITEMS = 16_384
PROPERTY_RESEARCH_PACKET_MAX_CANDIDATES_PER_RUN = 4_096
PROPERTY_RESEARCH_CANDIDATE_REF_MAX_LENGTH = 256

_RUN_CANDIDATE_KEYS = (
    "ranked_candidates",
    "results",
    "top_candidates",
    "_delivery_candidates",
)
_SOURCE_CANDIDATE_KEYS = (
    "research_candidates",
    "top_candidates",
    "ranked_candidates",
    "results",
)

_PROPERTY_SEARCH_ASSERT_PRINCIPAL_WRITE_ALLOWED_SQL = (
    "SELECT property_search_assert_principal_write_allowed(%s, %s)"
)


class PropertyResearchPacketProjectionError(ValueError):
    """A candidate cannot be represented by the versioned packet contract."""


class PropertyResearchPacketOversizeError(PropertyResearchPacketProjectionError):
    """A packet exceeds a declared structural or serialized-size bound."""


class PropertyResearchPacketConflictError(PropertyResearchPacketProjectionError):
    """One tenant/ref identity points at conflicting listing URLs."""


class PropertyResearchPacketVersionError(PropertyResearchPacketProjectionError):
    """A stored packet was produced by an incompatible writer."""


def _bounded_packet_write_values(
    values: Iterable[Any],
    *,
    error_code: str,
) -> tuple[Any, ...]:
    materialized = tuple(
        islice(iter(values), PROPERTY_RESEARCH_PACKET_MAX_CANDIDATES_PER_RUN + 1)
    )
    if len(materialized) > PROPERTY_RESEARCH_PACKET_MAX_CANDIDATES_PER_RUN:
        raise PropertyResearchPacketOversizeError(error_code)
    return materialized


def _assert_property_research_packet_write_authorities(
    cursor: Any,
    identities: Iterable[tuple[object, object]],
) -> None:
    normalized_identities: set[tuple[str, str]] = set()
    for principal_id, run_id in identities:
        normalized_principal = str(principal_id or "").strip()
        if not normalized_principal:
            raise PropertyResearchPacketProjectionError(
                "packet_write_principal_identity_missing"
            )
        normalized_identities.add(
            (normalized_principal, str(run_id or "").strip())
        )
    for identity in sorted(normalized_identities):
        cursor.execute(
            _PROPERTY_SEARCH_ASSERT_PRINCIPAL_WRITE_ALLOWED_SQL,
            identity,
        )


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PropertyResearchPacketProjectionError("packet_json_not_canonicalizable") from exc


def property_research_packet_canonical_json(value: object) -> str:
    """Return the exact UTF-8 representation governed by the DB byte limit."""

    return _canonical_json_bytes(value).decode("utf-8")


def _bounded_json_value(
    value: object,
    *,
    depth: int = 0,
    item_budget: list[int] | None = None,
) -> object:
    if depth > PROPERTY_RESEARCH_PACKET_MAX_DEPTH:
        raise PropertyResearchPacketOversizeError("packet_max_depth_exceeded")
    budget = item_budget if item_budget is not None else [PROPERTY_RESEARCH_PACKET_MAX_ITEMS]
    budget[0] -= 1
    if budget[0] < 0:
        raise PropertyResearchPacketOversizeError("packet_max_items_exceeded")
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PropertyResearchPacketProjectionError("packet_non_finite_number")
        return 0.0 if value == 0.0 else value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str):
                raise PropertyResearchPacketProjectionError("packet_non_string_object_key")
            normalized[key] = _bounded_json_value(value[key], depth=depth + 1, item_budget=budget)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_bounded_json_value(item, depth=depth + 1, item_budget=budget) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized_items = [
            _bounded_json_value(item, depth=depth + 1, item_budget=budget)
            for item in value
        ]
        return sorted(normalized_items, key=_canonical_json_bytes)
    raise PropertyResearchPacketProjectionError(f"packet_unsupported_value:{type(value).__name__}")


def property_research_candidate_ref(candidate: Mapping[str, object]) -> str:
    explicit_ref = str(
        candidate.get("candidate_ref") or candidate.get("research_candidate_ref") or ""
    ).strip()
    if explicit_ref:
        if len(explicit_ref) > PROPERTY_RESEARCH_CANDIDATE_REF_MAX_LENGTH:
            raise PropertyResearchPacketProjectionError("candidate_ref_too_long")
        return explicit_ref
    raw = "|".join(
        str(candidate.get(key) or "").strip()
        for key in ("title", "property_url", "review_url", "source_ref", "source_label")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _candidate_ref_algorithm(candidate: Mapping[str, object]) -> str:
    return (
        "explicit"
        if str(candidate.get("candidate_ref") or candidate.get("research_candidate_ref") or "").strip()
        else "derived_v1"
    )


def _property_url(candidate: Mapping[str, object]) -> str:
    return str(candidate.get("property_url") or candidate.get("review_url") or "").strip()


def _property_url_sha256(candidate: Mapping[str, object]) -> str | None:
    value = _property_url(candidate)
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


def _merge_packet_candidate_values(current: object, incoming: object) -> object:
    if current in (None, "", [], {}):
        return incoming
    if incoming in (None, "", [], {}):
        return current
    if isinstance(current, dict) and isinstance(incoming, dict):
        merged = dict(current)
        for key in sorted(incoming):
            merged[key] = (
                _merge_packet_candidate_values(merged[key], incoming[key])
                if key in merged
                else incoming[key]
            )
        return merged
    if isinstance(current, list) and isinstance(incoming, list):
        merged = list(current)
        seen = {_canonical_json_bytes(item) for item in merged}
        for item in incoming:
            identity = _canonical_json_bytes(item)
            if identity not in seen:
                merged.append(item)
                seen.add(identity)
        return merged
    # Rank order is meaningful. Preserve the first non-empty scalar observation.
    return current


def _candidate_occurrences(record: Mapping[str, object]) -> Iterable[tuple[dict[str, object], int]]:
    summary = dict(record.get("summary") or {}) if isinstance(record.get("summary"), dict) else {}
    rank = 0
    for key in _RUN_CANDIDATE_KEYS:
        rows = summary.get(key)
        if not isinstance(rows, (list, tuple)):
            continue
        for row in rows:
            if isinstance(row, dict):
                rank += 1
                yield dict(row), rank
    sources = summary.get("sources")
    if not isinstance(sources, (list, tuple)):
        return
    for source in sources:
        if not isinstance(source, dict):
            continue
        source_defaults = {
            "source_label": source.get("source_label") or source.get("label"),
            "source_url": source.get("source_url"),
            "platform": source.get("platform"),
            "provider_family": source.get("provider_family"),
        }
        for key in _SOURCE_CANDIDATE_KEYS:
            rows = source.get(key)
            if not isinstance(rows, (list, tuple)):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                candidate = dict(row)
                for default_key, default_value in source_defaults.items():
                    if default_value not in (None, "", [], {}) and candidate.get(default_key) in (
                        None,
                        "",
                        [],
                        {},
                    ):
                        candidate[default_key] = default_value
                rank += 1
                yield candidate, rank


def _packet_content_sha256(packet: Mapping[str, object]) -> str:
    hashable = dict(packet)
    hashable.pop("packet_sha256", None)
    return hashlib.sha256(_canonical_json_bytes(hashable)).hexdigest()


def project_property_research_packet(
    candidate: Mapping[str, object],
    *,
    run_id: str,
    observed_at: str,
    source_rank: int,
) -> dict[str, object]:
    normalized_run_id = str(run_id or "").strip()
    normalized_observed_at = str(observed_at or "").strip()
    if not normalized_run_id:
        raise PropertyResearchPacketProjectionError("packet_run_id_missing")
    if len(normalized_run_id) > 256:
        raise PropertyResearchPacketProjectionError("packet_run_id_too_long")
    if not normalized_observed_at:
        raise PropertyResearchPacketProjectionError("packet_observed_at_missing")
    normalized = _bounded_json_value(dict(candidate))
    if not isinstance(normalized, dict):
        raise PropertyResearchPacketProjectionError("packet_candidate_not_object")
    candidate_ref = property_research_candidate_ref(normalized)
    normalized["candidate_ref"] = candidate_ref
    normalized["candidate_ref_algorithm"] = _candidate_ref_algorithm(candidate)
    normalized["packet_schema_version"] = PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION
    normalized["packet_source_run_id"] = normalized_run_id
    normalized["packet_source_observed_at"] = normalized_observed_at
    normalized["packet_source_rank"] = max(0, int(source_rank or 0))
    normalized["packet_sha256"] = _packet_content_sha256(normalized)
    serialized = _canonical_json_bytes(normalized)
    if len(serialized) > PROPERTY_RESEARCH_PACKET_MAX_BYTES:
        raise PropertyResearchPacketOversizeError(
            f"packet_max_bytes_exceeded:{len(serialized)}>{PROPERTY_RESEARCH_PACKET_MAX_BYTES}"
        )
    return normalized


def project_property_research_packet_links(record: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    run_id = str(record.get("run_id") or "").strip()
    principal_id = str(record.get("principal_id") or "").strip()
    if not run_id or not principal_id:
        return ()
    observed_at = str(record.get("updated_at") or record.get("created_at") or "").strip()
    merged_by_ref: dict[str, tuple[dict[str, object], int]] = {}
    for candidate, source_rank in _candidate_occurrences(record):
        if source_rank > PROPERTY_RESEARCH_PACKET_MAX_CANDIDATES_PER_RUN:
            raise PropertyResearchPacketOversizeError("packet_max_candidates_per_run_exceeded")
        candidate_ref = property_research_candidate_ref(candidate)
        existing = merged_by_ref.get(candidate_ref)
        if existing is None:
            merged_by_ref[candidate_ref] = (candidate, source_rank)
            continue
        existing_candidate, first_rank = existing
        existing_url = _property_url(existing_candidate)
        incoming_url = _property_url(candidate)
        if existing_url and incoming_url and existing_url != incoming_url:
            raise PropertyResearchPacketConflictError(f"candidate_ref_url_conflict:{candidate_ref}")
        merged_candidate = _merge_packet_candidate_values(existing_candidate, candidate)
        if not isinstance(merged_candidate, dict):
            raise PropertyResearchPacketProjectionError("packet_merge_not_object")
        merged_by_ref[candidate_ref] = (merged_candidate, first_rank)

    links: list[dict[str, object]] = []
    aggregate_bytes = 0
    for candidate_ref, (candidate, source_rank) in merged_by_ref.items():
        packet = project_property_research_packet(
            candidate,
            run_id=run_id,
            observed_at=observed_at,
            source_rank=source_rank,
        )
        packet_canonical_json = property_research_packet_canonical_json(packet)
        packet_size_bytes = len(packet_canonical_json.encode("utf-8"))
        aggregate_bytes += packet_size_bytes
        if aggregate_bytes > PROPERTY_RESEARCH_PACKET_MAX_AGGREGATE_BYTES:
            raise PropertyResearchPacketOversizeError(
                "packet_max_aggregate_bytes_exceeded:"
                f"{aggregate_bytes}>{PROPERTY_RESEARCH_PACKET_MAX_AGGREGATE_BYTES}"
            )
        links.append(
            {
                "principal_id": principal_id,
                "candidate_ref": candidate_ref,
                "candidate_ref_algorithm": str(packet["candidate_ref_algorithm"]),
                "packet_json": packet,
                "packet_canonical_json": packet_canonical_json,
                "packet_size_bytes": packet_size_bytes,
                "packet_schema_version": PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
                "packet_sha256": str(packet["packet_sha256"]),
                "property_url_sha256": _property_url_sha256(packet),
                "first_run_id": run_id,
                "last_run_id": run_id,
                "first_seen_at": observed_at,
                "last_seen_at": observed_at,
                "retention_state": "active",
            }
        )
    return tuple(links)


_PROPERTY_RESEARCH_PACKET_LINK_LOOKUP_SQL = """
    SELECT candidate_ref,
           packet_json,
           packet_canonical_json,
           packet_size_bytes,
           packet_schema_version,
           packet_sha256,
           candidate_ref_algorithm,
           property_url_sha256,
           first_run_id,
           last_run_id,
           first_seen_at,
           last_seen_at,
           retention_state
    FROM property_research_packet_links
    WHERE principal_id = %s
      AND candidate_ref = %s
      AND retention_state = 'active'
"""


_PROPERTY_RESEARCH_PACKET_LINK_UPSERT_SQL = """
    INSERT INTO property_research_packet_links (
        principal_id,
        candidate_ref,
        candidate_ref_algorithm,
        packet_json,
        packet_canonical_json,
        packet_size_bytes,
        packet_schema_version,
        packet_sha256,
        property_url_sha256,
        first_run_id,
        last_run_id,
        first_seen_at,
        last_seen_at,
        retention_state
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (principal_id, candidate_ref) DO UPDATE
    SET candidate_ref_algorithm = CASE
            WHEN (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) > (
                property_research_packet_links.last_seen_at,
                property_research_packet_links.last_run_id
            )
            THEN EXCLUDED.candidate_ref_algorithm
            ELSE property_research_packet_links.candidate_ref_algorithm
        END,
        packet_json = CASE
            WHEN (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) > (
                property_research_packet_links.last_seen_at,
                property_research_packet_links.last_run_id
            )
            THEN EXCLUDED.packet_json
            ELSE property_research_packet_links.packet_json
        END,
        packet_canonical_json = CASE
            WHEN (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) > (
                property_research_packet_links.last_seen_at,
                property_research_packet_links.last_run_id
            )
            THEN EXCLUDED.packet_canonical_json
            ELSE property_research_packet_links.packet_canonical_json
        END,
        packet_size_bytes = CASE
            WHEN (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) > (
                property_research_packet_links.last_seen_at,
                property_research_packet_links.last_run_id
            )
            THEN EXCLUDED.packet_size_bytes
            ELSE property_research_packet_links.packet_size_bytes
        END,
        packet_schema_version = CASE
            WHEN (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) > (
                property_research_packet_links.last_seen_at,
                property_research_packet_links.last_run_id
            )
            THEN EXCLUDED.packet_schema_version
            ELSE property_research_packet_links.packet_schema_version
        END,
        packet_sha256 = CASE
            WHEN (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) > (
                property_research_packet_links.last_seen_at,
                property_research_packet_links.last_run_id
            )
            THEN EXCLUDED.packet_sha256
            ELSE property_research_packet_links.packet_sha256
        END,
        property_url_sha256 = CASE
            WHEN (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) > (
                property_research_packet_links.last_seen_at,
                property_research_packet_links.last_run_id
            )
            THEN EXCLUDED.property_url_sha256
            ELSE property_research_packet_links.property_url_sha256
        END,
        first_run_id = CASE
            WHEN (EXCLUDED.first_seen_at, EXCLUDED.first_run_id) < (
                property_research_packet_links.first_seen_at,
                property_research_packet_links.first_run_id
            )
            THEN EXCLUDED.first_run_id
            ELSE property_research_packet_links.first_run_id
        END,
        first_seen_at = LEAST(property_research_packet_links.first_seen_at, EXCLUDED.first_seen_at),
        last_run_id = CASE
            WHEN (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) > (
                property_research_packet_links.last_seen_at,
                property_research_packet_links.last_run_id
            )
            THEN EXCLUDED.last_run_id
            ELSE property_research_packet_links.last_run_id
        END,
        last_seen_at = GREATEST(property_research_packet_links.last_seen_at, EXCLUDED.last_seen_at),
        retention_state = property_research_packet_links.retention_state
    WHERE (
            property_research_packet_links.property_url_sha256 = EXCLUDED.property_url_sha256
         OR (
                property_research_packet_links.property_url_sha256 IS NULL
            AND (
                    EXCLUDED.property_url_sha256 IS NULL
                 OR (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) > (
                        property_research_packet_links.last_seen_at,
                        property_research_packet_links.last_run_id
                    )
            )
         )
    )
      AND (
            (EXCLUDED.last_seen_at, EXCLUDED.last_run_id) <> (
                property_research_packet_links.last_seen_at,
                property_research_packet_links.last_run_id
            )
         OR (
                EXCLUDED.packet_sha256 = property_research_packet_links.packet_sha256
            AND EXCLUDED.packet_canonical_json = property_research_packet_links.packet_canonical_json
            AND EXCLUDED.candidate_ref_algorithm = property_research_packet_links.candidate_ref_algorithm
            AND EXCLUDED.property_url_sha256 IS NOT DISTINCT FROM
                property_research_packet_links.property_url_sha256
         )
      )
    RETURNING candidate_ref
"""


def upsert_property_research_packet_links(cursor: Any, links: Iterable[Mapping[str, object]]) -> int:
    from psycopg.types.json import Json

    normalized_links: list[dict[str, object]] = []
    authority_identities: set[tuple[str, str]] = set()
    for raw_link in _bounded_packet_write_values(
        links,
        error_code="packet_max_links_per_write_exceeded",
    ):
        link = dict(raw_link)
        normalized_principal = str(link.get("principal_id") or "").strip()
        normalized_run_id = str(link.get("last_run_id") or "").strip()
        if not normalized_principal or not normalized_run_id:
            raise PropertyResearchPacketProjectionError(
                "packet_link_write_identity_missing"
            )
        link["principal_id"] = normalized_principal
        link["last_run_id"] = normalized_run_id
        normalized_links.append(link)
        authority_identities.add((normalized_principal, normalized_run_id))
    _assert_property_research_packet_write_authorities(
        cursor,
        authority_identities,
    )

    written = 0
    for link in normalized_links:
        cursor.execute(
            """
            SELECT retention_state, property_url_sha256
            FROM property_research_packet_links
            WHERE principal_id = %s AND candidate_ref = %s
            FOR UPDATE
            """,
            (link["principal_id"], link["candidate_ref"]),
        )
        existing = cursor.fetchone()
        if existing:
            existing_url_sha256 = existing[1]
            incoming_url_sha256 = link.get("property_url_sha256")
            if (
                existing_url_sha256 is not None
                and existing_url_sha256 != incoming_url_sha256
            ):
                raise PropertyResearchPacketConflictError(
                    f"candidate_ref_packet_conflict:{link['candidate_ref']}"
                )
        if existing and str(existing[0] or "").strip() == "legal_hold":
            if existing[1] != link.get("property_url_sha256"):
                raise PropertyResearchPacketConflictError(
                    f"candidate_ref_packet_conflict:{link['candidate_ref']}"
                )
            written += 1
            continue
        cursor.execute(
            _PROPERTY_RESEARCH_PACKET_LINK_UPSERT_SQL,
            (
                link["principal_id"],
                link["candidate_ref"],
                link["candidate_ref_algorithm"],
                Json(link["packet_json"]),
                link["packet_canonical_json"],
                link["packet_size_bytes"],
                link["packet_schema_version"],
                link["packet_sha256"],
                link.get("property_url_sha256"),
                link["first_run_id"],
                link["last_run_id"],
                link["first_seen_at"],
                link["last_seen_at"],
                link["retention_state"],
            ),
        )
        if cursor.fetchone() is None:
            raise PropertyResearchPacketConflictError(
                f"candidate_ref_packet_conflict:{link['candidate_ref']}"
            )
        written += 1
    return written


_PROPERTY_RESEARCH_PACKET_MEMBERSHIP_UPSERT_SQL = """
    INSERT INTO property_research_packet_run_memberships (
        principal_id,
        run_id,
        candidate_ref,
        candidate_ref_algorithm,
        packet_json,
        packet_canonical_json,
        packet_size_bytes,
        packet_schema_version,
        packet_sha256,
        property_url_sha256,
        observed_at,
        source_rank
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (principal_id, run_id, candidate_ref) DO UPDATE
    SET candidate_ref_algorithm = CASE
            WHEN EXCLUDED.observed_at > property_research_packet_run_memberships.observed_at
            THEN EXCLUDED.candidate_ref_algorithm
            ELSE property_research_packet_run_memberships.candidate_ref_algorithm
        END,
        packet_json = CASE
            WHEN EXCLUDED.observed_at > property_research_packet_run_memberships.observed_at
            THEN EXCLUDED.packet_json
            ELSE property_research_packet_run_memberships.packet_json
        END,
        packet_canonical_json = CASE
            WHEN EXCLUDED.observed_at > property_research_packet_run_memberships.observed_at
            THEN EXCLUDED.packet_canonical_json
            ELSE property_research_packet_run_memberships.packet_canonical_json
        END,
        packet_size_bytes = CASE
            WHEN EXCLUDED.observed_at > property_research_packet_run_memberships.observed_at
            THEN EXCLUDED.packet_size_bytes
            ELSE property_research_packet_run_memberships.packet_size_bytes
        END,
        packet_schema_version = CASE
            WHEN EXCLUDED.observed_at > property_research_packet_run_memberships.observed_at
            THEN EXCLUDED.packet_schema_version
            ELSE property_research_packet_run_memberships.packet_schema_version
        END,
        packet_sha256 = CASE
            WHEN EXCLUDED.observed_at > property_research_packet_run_memberships.observed_at
            THEN EXCLUDED.packet_sha256
            ELSE property_research_packet_run_memberships.packet_sha256
        END,
        property_url_sha256 = CASE
            WHEN property_research_packet_run_memberships.property_url_sha256 IS NULL
             AND EXCLUDED.observed_at > property_research_packet_run_memberships.observed_at
            THEN EXCLUDED.property_url_sha256
            ELSE property_research_packet_run_memberships.property_url_sha256
        END,
        observed_at = GREATEST(
            property_research_packet_run_memberships.observed_at,
            EXCLUDED.observed_at
        ),
        source_rank = CASE
            WHEN EXCLUDED.observed_at > property_research_packet_run_memberships.observed_at
            THEN EXCLUDED.source_rank
            ELSE property_research_packet_run_memberships.source_rank
        END
    WHERE (
            property_research_packet_run_memberships.property_url_sha256 = EXCLUDED.property_url_sha256
         OR (
                property_research_packet_run_memberships.property_url_sha256 IS NULL
            AND (
                    EXCLUDED.property_url_sha256 IS NULL
                 OR EXCLUDED.observed_at > property_research_packet_run_memberships.observed_at
            )
         )
    )
      AND (
            EXCLUDED.observed_at <> property_research_packet_run_memberships.observed_at
         OR (
                EXCLUDED.packet_sha256 = property_research_packet_run_memberships.packet_sha256
            AND EXCLUDED.packet_canonical_json = property_research_packet_run_memberships.packet_canonical_json
            AND EXCLUDED.candidate_ref_algorithm = property_research_packet_run_memberships.candidate_ref_algorithm
            AND EXCLUDED.property_url_sha256 IS NOT DISTINCT FROM
                property_research_packet_run_memberships.property_url_sha256
         )
      )
    RETURNING candidate_ref
"""


def refresh_property_research_packet_links_for_refs(
    cursor: Any,
    *,
    principal_id: str,
    candidate_refs: Iterable[str],
) -> int:
    """Reselect each materialized packet from its newest surviving run membership."""

    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise PropertyResearchPacketProjectionError(
            "packet_refresh_principal_identity_missing"
        )
    raw_refs = _bounded_packet_write_values(
        candidate_refs,
        error_code="packet_max_refresh_refs_exceeded",
    )
    refs = tuple(
        sorted(
            {
                str(value or "").strip()
                for value in raw_refs
                if str(value or "").strip()
            }
        )
    )
    if refs:
        _assert_property_research_packet_write_authorities(
            cursor,
            ((normalized_principal, ""),),
        )
    return _refresh_property_research_packet_links_for_refs_unchecked(
        cursor,
        principal_id=normalized_principal,
        candidate_refs=refs,
    )


def _refresh_property_research_packet_links_for_refs_unchecked(
    cursor: Any,
    *,
    principal_id: str,
    candidate_refs: Iterable[str],
) -> int:
    from psycopg.types.json import Json

    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise PropertyResearchPacketProjectionError(
            "packet_refresh_principal_identity_missing"
        )
    raw_refs = _bounded_packet_write_values(
        candidate_refs,
        error_code="packet_max_refresh_refs_exceeded",
    )
    refs = tuple(
        sorted(
            {
                str(value or "").strip()
                for value in raw_refs
                if str(value or "").strip()
            }
        )
    )
    refreshed = 0
    for candidate_ref in refs:
        cursor.execute(
            """
            SELECT retention_state
            FROM property_research_packet_links
            WHERE principal_id = %s AND candidate_ref = %s
            FOR UPDATE
            """,
            (normalized_principal, candidate_ref),
        )
        materialized_state = cursor.fetchone()
        if materialized_state and str(materialized_state[0] or "").strip() == "legal_hold":
            refreshed += 1
            continue
        cursor.execute(
            """
            SELECT candidate_ref_algorithm,
                   packet_json,
                   packet_canonical_json,
                   packet_size_bytes,
                   packet_schema_version,
                   packet_sha256,
                   property_url_sha256,
                   run_id,
                   observed_at
            FROM property_research_packet_run_memberships
            WHERE principal_id = %s AND candidate_ref = %s
            ORDER BY observed_at DESC, run_id DESC
            LIMIT 1
            """,
            (normalized_principal, candidate_ref),
        )
        winner = cursor.fetchone()
        if not winner:
            cursor.execute(
                """
                DELETE FROM property_research_packet_links
                WHERE principal_id = %s AND candidate_ref = %s
                  AND retention_state <> 'legal_hold'
                """,
                (normalized_principal, candidate_ref),
            )
            refreshed += max(0, int(getattr(cursor, "rowcount", 0) or 0))
            continue
        cursor.execute(
            """
            SELECT run_id, observed_at
            FROM property_research_packet_run_memberships
            WHERE principal_id = %s AND candidate_ref = %s
            ORDER BY observed_at ASC, run_id ASC
            LIMIT 1
            """,
            (normalized_principal, candidate_ref),
        )
        first = cursor.fetchone()
        if not first:
            raise PropertyResearchPacketConflictError(
                f"candidate_ref_membership_reselection_failed:{candidate_ref}"
            )
        cursor.execute(
            """
            UPDATE property_research_packet_links
            SET candidate_ref_algorithm = %s,
                packet_json = %s,
                packet_canonical_json = %s,
                packet_size_bytes = %s,
                packet_schema_version = %s,
                packet_sha256 = %s,
                property_url_sha256 = %s,
                first_run_id = %s,
                last_run_id = %s,
                first_seen_at = %s,
                last_seen_at = %s
            WHERE principal_id = %s AND candidate_ref = %s
            """,
            (
                winner[0],
                Json(winner[1]),
                winner[2],
                winner[3],
                winner[4],
                winner[5],
                winner[6],
                first[0],
                winner[7],
                first[1],
                winner[8],
                normalized_principal,
                candidate_ref,
            ),
        )
        if int(getattr(cursor, "rowcount", 0) or 0) != 1:
            raise PropertyResearchPacketConflictError(
                f"candidate_ref_materialized_link_missing:{candidate_ref}"
            )
        refreshed += 1
    return refreshed


def sync_property_research_packet_run_memberships(
    cursor: Any,
    *,
    principal_id: str,
    run_id: str,
    links: Iterable[Mapping[str, object]],
) -> int:
    """Make one run's membership set exact and reselect links removed from that run."""

    from psycopg.types.json import Json

    normalized_principal = str(principal_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_links = tuple(
        dict(link)
        for link in _bounded_packet_write_values(
            links,
            error_code="packet_max_membership_links_exceeded",
        )
    )
    if not normalized_principal or not normalized_run_id:
        raise PropertyResearchPacketProjectionError("packet_membership_identity_missing")
    for link in normalized_links:
        if (
            str(link.get("principal_id") or "").strip() != normalized_principal
            or str(link.get("last_run_id") or "").strip() != normalized_run_id
        ):
            raise PropertyResearchPacketProjectionError("packet_membership_identity_mismatch")
    _assert_property_research_packet_write_authorities(
        cursor,
        ((normalized_principal, normalized_run_id),),
    )
    cursor.execute(
        """
        SELECT candidate_ref
        FROM property_research_packet_run_memberships
        WHERE principal_id = %s AND run_id = %s
        FOR UPDATE
        """,
        (normalized_principal, normalized_run_id),
    )
    existing_refs = {str(row[0] or "").strip() for row in list(cursor.fetchall() or [])}
    incoming_refs: set[str] = set()
    written = 0
    for link in normalized_links:
        candidate_ref = str(link["candidate_ref"] or "").strip()
        incoming_refs.add(candidate_ref)
        packet = dict(link["packet_json"] or {})
        cursor.execute(
            _PROPERTY_RESEARCH_PACKET_MEMBERSHIP_UPSERT_SQL,
            (
                normalized_principal,
                normalized_run_id,
                candidate_ref,
                link["candidate_ref_algorithm"],
                Json(packet),
                link["packet_canonical_json"],
                link["packet_size_bytes"],
                link["packet_schema_version"],
                link["packet_sha256"],
                link.get("property_url_sha256"),
                link["last_seen_at"],
                max(0, int(packet.get("packet_source_rank") or 0)),
            ),
        )
        if cursor.fetchone() is None:
            raise PropertyResearchPacketConflictError(
                f"candidate_ref_membership_conflict:{candidate_ref}"
            )
        written += 1
    if incoming_refs:
        cursor.execute(
            """
            DELETE FROM property_research_packet_run_memberships
            WHERE principal_id = %s AND run_id = %s
              AND NOT (candidate_ref = ANY(%s))
            RETURNING candidate_ref
            """,
            (normalized_principal, normalized_run_id, list(sorted(incoming_refs))),
        )
    else:
        cursor.execute(
            """
            DELETE FROM property_research_packet_run_memberships
            WHERE principal_id = %s AND run_id = %s
            RETURNING candidate_ref
            """,
            (normalized_principal, normalized_run_id),
        )
    removed_refs = {str(row[0] or "").strip() for row in list(cursor.fetchall() or [])}
    _refresh_property_research_packet_links_for_refs_unchecked(
        cursor,
        principal_id=normalized_principal,
        candidate_refs=(existing_refs | removed_refs) - incoming_refs,
    )
    return written


def load_property_research_packet_link(
    cursor: Any,
    *,
    principal_id: str,
    candidate_ref: str,
    active_only: bool = True,
) -> dict[str, object] | None:
    normalized_principal_id = str(principal_id or "").strip()
    normalized_candidate_ref = str(candidate_ref or "").strip()
    if not normalized_principal_id or not normalized_candidate_ref:
        return None
    cursor.execute(
        (
            _PROPERTY_RESEARCH_PACKET_LINK_LOOKUP_SQL
            if active_only
            else _PROPERTY_RESEARCH_PACKET_LINK_LOOKUP_SQL.replace(
                "      AND retention_state = 'active'\n", ""
            )
        ),
        (normalized_principal_id, normalized_candidate_ref),
    )
    row = cursor.fetchone()
    if not row:
        return None
    stored_candidate_ref = str(row[0] or "").strip()
    packet = dict(row[1] or {}) if isinstance(row[1], dict) else None
    if packet is None:
        raise PropertyResearchPacketVersionError("packet_json_not_object")
    try:
        canonical_json = str(row[2] or "")
        row_size = int(row[3])
        row_version = int(row[4])
        embedded_version = int(packet.get("packet_schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise PropertyResearchPacketVersionError("packet_schema_version_invalid") from exc
    if row_version != PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION or embedded_version != row_version:
        raise PropertyResearchPacketVersionError("packet_schema_version_mismatch")
    if stored_candidate_ref != normalized_candidate_ref or str(packet.get("candidate_ref") or "").strip() != stored_candidate_ref:
        raise PropertyResearchPacketVersionError("packet_candidate_ref_mismatch")
    try:
        bounded_packet = _bounded_json_value(packet)
    except PropertyResearchPacketProjectionError as exc:
        raise PropertyResearchPacketVersionError("packet_structural_limits_invalid") from exc
    if not isinstance(bounded_packet, dict):
        raise PropertyResearchPacketVersionError("packet_json_not_object")
    actual_canonical_json = property_research_packet_canonical_json(bounded_packet)
    actual_size = len(actual_canonical_json.encode("utf-8"))
    if (
        not canonical_json
        or canonical_json != actual_canonical_json
        or row_size != actual_size
        or row_size > PROPERTY_RESEARCH_PACKET_MAX_BYTES
    ):
        raise PropertyResearchPacketVersionError("packet_size_or_canonical_json_mismatch")
    expected_sha = str(row[5] or "").strip()
    embedded_sha = str(packet.get("packet_sha256") or "").strip()
    actual_sha = _packet_content_sha256(packet)
    if not expected_sha or expected_sha != embedded_sha or expected_sha != actual_sha:
        raise PropertyResearchPacketVersionError("packet_sha256_mismatch")
    row_algorithm = str(row[6] or "").strip()
    embedded_algorithm = str(packet.get("candidate_ref_algorithm") or "").strip()
    if row_algorithm not in {"explicit", "derived_v1"} or embedded_algorithm != row_algorithm:
        raise PropertyResearchPacketVersionError("packet_candidate_ref_algorithm_mismatch")
    row_property_url_sha256 = str(row[7] or "").strip() or None
    if row_property_url_sha256 != _property_url_sha256(packet):
        raise PropertyResearchPacketVersionError("packet_property_url_sha256_mismatch")
    return {
        "candidate": packet,
        "packet_json": packet,
        "packet_schema_version": row_version,
        "packet_sha256": expected_sha,
        "packet_size_bytes": row_size,
        "candidate_ref_algorithm": row_algorithm,
        "property_url_sha256": row_property_url_sha256,
        "first_run_id": str(row[8] or ""),
        "last_run_id": str(row[9] or ""),
        "first_seen_at": row[10],
        "last_seen_at": row[11],
        "retention_state": str(row[12] or ""),
    }
