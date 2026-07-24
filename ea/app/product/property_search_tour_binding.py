from __future__ import annotations

import copy
import hashlib
import json
import re
import urllib.parse
from collections.abc import Iterator, Mapping, MutableMapping
from datetime import datetime, timezone

from app.product.property_research_packet_links import (
    _RUN_CANDIDATE_KEYS,
    _SOURCE_CANDIDATE_KEYS,
    property_research_candidate_ref,
)


class PropertySearchTourBindingError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "property_search_tour_binding_failed").strip()
        super().__init__(self.code)


_GENERIC_PROVIDER_KEYS = {
    "",
    "marketplace",
    "propertyscout",
    "propertysearch",
    "provider",
    "source",
}
_PROVIDER_ALIASES = {
    "willhaben": "willhaben",
    "willhabenat": "willhaben",
    "immobilienscout24": "immobilienscout24",
    "immoscout24": "immobilienscout24",
    "findmyhome": "findmyhome",
    "kalandra": "kalandra",
}


def _normalized_provider_key(value: object) -> str:
    token = re.sub(r"[^a-z0-9]+", "", _normalized_text(value).lower())
    token = _PROVIDER_ALIASES.get(token, token)
    return "" if token in _GENERIC_PROVIDER_KEYS else token


def canonical_property_source_url(value: object) -> str:
    """Return a stable, credential-free source URL used only for identity checks."""

    normalized = _normalized_text(value)
    if not normalized:
        return ""
    try:
        parsed = urllib.parse.urlsplit(normalized)
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    scheme = parsed.scheme.lower()
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in parsed.path
        or "\x00" in parsed.path
    ):
        return ""
    try:
        hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    display_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    netloc = display_host if port in (None, default_port) else f"{display_host}:{port}"
    path = parsed.path or "/"
    return urllib.parse.urlunsplit((scheme, netloc, path, parsed.query, ""))


def property_search_source_url_sha256(value: object) -> str:
    canonical = canonical_property_source_url(value)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest() if canonical else ""


def _provider_key_from_url(value: object) -> str:
    canonical = canonical_property_source_url(value)
    if not canonical:
        return ""
    hostname = str(urllib.parse.urlsplit(canonical).hostname or "").lower()
    if hostname == "willhaben.at" or hostname.endswith(".willhaben.at"):
        return "willhaben"
    if hostname == "immobilienscout24.at" or hostname.endswith(".immobilienscout24.at"):
        return "immobilienscout24"
    if hostname == "findmyhome.at" or hostname.endswith(".findmyhome.at"):
        return "findmyhome"
    if hostname == "kalandra.at" or hostname.endswith(".kalandra.at"):
        return "kalandra"
    labels = [label for label in hostname.split(".") if label and label != "www"]
    return _normalized_provider_key(labels[-2] if len(labels) >= 2 else hostname)


def _source_ref_identity(value: object) -> tuple[str, str]:
    normalized = _normalized_text(value)
    if not normalized or ":" not in normalized:
        return "", ""
    provider, _separator, listing_id = normalized.partition(":")
    return _normalized_provider_key(provider), listing_id.strip()


def property_search_run_record_sha256(record: Mapping[str, object]) -> str:
    payload = json.dumps(
        dict(record),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def property_search_tour_run_is_terminal(record: Mapping[str, object]) -> bool:
    return _normalized_text(record.get("status")).lower() in {
        "complete",
        "completed",
        "processed",
        "succeeded",
        "success",
    }


def _normalized_text(value: object) -> str:
    return str(value or "").strip()


def _candidate_listing_ids(candidate: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    payloads: list[Mapping[str, object]] = [candidate]
    for key in ("property_facts", "facts", "preview"):
        nested = candidate.get(key)
        if isinstance(nested, Mapping):
            payloads.append(nested)
    for payload in payloads:
        for key in (
            "listing_id",
            "source_listing_id",
            "provider_listing_id",
            "external_id",
        ):
            value = _normalized_text(payload.get(key))
            if value and value not in values:
                values.append(value)
    for payload in payloads:
        _provider, source_listing_id = _source_ref_identity(payload.get("source_ref"))
        if source_listing_id and source_listing_id not in values:
            values.append(source_listing_id)
    return tuple(values)


def _candidate_source_refs(candidate: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    payloads: list[Mapping[str, object]] = [candidate]
    for key in ("property_facts", "facts", "preview"):
        nested = candidate.get(key)
        if isinstance(nested, Mapping):
            payloads.append(nested)
    for payload in payloads:
        value = _normalized_text(payload.get("source_ref"))
        if value and value not in values:
            values.append(value)
    return tuple(values)


def _property_url_contains_listing_id(property_url: str, listing_id: str) -> bool:
    parsed = urllib.parse.urlsplit(property_url)
    searchable = f"{parsed.path}?{parsed.query}"
    return bool(re.search(rf"(?<!\d){re.escape(listing_id)}(?!\d)", searchable))


def _candidate_property_urls(candidate: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []
    # `review_url` is an authenticated PropertyQuarry handoff URL, not source
    # listing identity. Treating it as a listing URL creates a false conflict.
    for key in ("property_url", "listing_url"):
        canonical = canonical_property_source_url(candidate.get(key))
        if canonical and canonical not in values:
            values.append(canonical)
    return tuple(values)


def _candidate_provider_keys(candidate: Mapping[str, object]) -> tuple[str, ...]:
    values: list[str] = []

    def _append(value: str) -> None:
        if value and value not in values:
            values.append(value)

    for property_url in _candidate_property_urls(candidate):
        _append(_provider_key_from_url(property_url))
    for key in ("source_ref",):
        provider, _listing_id = _source_ref_identity(candidate.get(key))
        _append(provider)
    for key in ("platform", "provider_family"):
        _append(_normalized_provider_key(candidate.get(key)))
    source_label = _normalized_provider_key(candidate.get("source_label"))
    if source_label in set(_PROVIDER_ALIASES.values()):
        _append(source_label)
    return tuple(values)


def _validated_bundle_identity(
    bundle_identity: Mapping[str, object],
    *,
    run_id: str,
    candidate_ref: str,
    expected_listing_id: str,
) -> dict[str, str]:
    if bundle_identity.get("owner_verified") is not True:
        raise PropertySearchTourBindingError("property_search_tour_bundle_owner_mismatch")
    bundle_run_id = _normalized_text(bundle_identity.get("search_run_id"))
    if not bundle_run_id:
        raise PropertySearchTourBindingError("property_search_tour_bundle_run_identity_missing")
    if bundle_run_id != run_id:
        raise PropertySearchTourBindingError("property_search_tour_bundle_run_identity_mismatch")
    bundle_candidate_ref = _normalized_text(
        bundle_identity.get("candidate_ref")
        or bundle_identity.get("research_candidate_ref")
    )
    if bundle_candidate_ref and bundle_candidate_ref != candidate_ref:
        raise PropertySearchTourBindingError("property_search_tour_bundle_candidate_ref_mismatch")

    canonical_urls = {
        canonical_property_source_url(bundle_identity.get(key))
        for key in ("property_url", "listing_url")
        if _normalized_text(bundle_identity.get(key))
    }
    if "" in canonical_urls or not canonical_urls:
        raise PropertySearchTourBindingError("property_search_tour_bundle_property_url_missing")
    if len(canonical_urls) != 1:
        raise PropertySearchTourBindingError("property_search_tour_bundle_property_url_conflict")
    property_url = next(iter(canonical_urls))
    property_url_sha256 = property_search_source_url_sha256(property_url)
    declared_property_url_sha256 = _normalized_text(
        bundle_identity.get("property_url_sha256")
    ).lower()
    if (
        not re.fullmatch(r"[0-9a-f]{64}", declared_property_url_sha256)
        or not property_url_sha256
        or declared_property_url_sha256 != property_url_sha256
    ):
        raise PropertySearchTourBindingError("property_search_tour_bundle_property_url_sha256_mismatch")

    listing_ids: list[str] = []
    for key in ("external_id", "listing_id", "source_listing_id"):
        value = _normalized_text(bundle_identity.get(key))
        if value and value not in listing_ids:
            listing_ids.append(value)
    source_provider, source_listing_id = _source_ref_identity(
        bundle_identity.get("source_ref")
    )
    if source_listing_id and source_listing_id not in listing_ids:
        listing_ids.append(source_listing_id)
    if not listing_ids:
        raise PropertySearchTourBindingError("property_search_tour_bundle_listing_identity_missing")
    if any(value != expected_listing_id for value in listing_ids):
        raise PropertySearchTourBindingError("property_search_tour_bundle_listing_identity_mismatch")

    providers = {
        value
        for value in (
            _provider_key_from_url(property_url),
            source_provider,
            _normalized_provider_key(bundle_identity.get("provider_key")),
        )
        if value
    }
    if len(providers) != 1:
        raise PropertySearchTourBindingError("property_search_tour_bundle_provider_identity_conflict")
    provider_key = next(iter(providers))
    return {
        "property_url": property_url,
        "property_url_sha256": property_url_sha256,
        "provider_key": provider_key,
    }


def _candidate_occurrence_refs(
    record: MutableMapping[str, object],
) -> Iterator[tuple[MutableMapping[str, object], dict[str, object], str]]:
    summary = record.get("summary")
    if not isinstance(summary, MutableMapping):
        return
    for key in _RUN_CANDIDATE_KEYS:
        rows = summary.get(key)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if isinstance(row, MutableMapping):
                yield row, {}, f"summary.{key}[{index}]"
    sources = summary.get("sources")
    if not isinstance(sources, list):
        return
    for source_index, source in enumerate(sources):
        if not isinstance(source, MutableMapping):
            continue
        defaults = {
            "source_label": source.get("source_label") or source.get("label"),
            "source_url": source.get("source_url"),
            "platform": source.get("platform"),
            "provider_family": source.get("provider_family"),
        }
        for key in _SOURCE_CANDIDATE_KEYS:
            rows = source.get(key)
            if not isinstance(rows, list):
                continue
            for candidate_index, row in enumerate(rows):
                if isinstance(row, MutableMapping):
                    yield (
                        row,
                        defaults,
                        f"summary.sources[{source_index}].{key}[{candidate_index}]",
                    )


def _effective_candidate(
    candidate: Mapping[str, object],
    defaults: Mapping[str, object],
) -> dict[str, object]:
    effective = dict(candidate)
    for key, value in defaults.items():
        if value not in (None, ""):
            effective.setdefault(key, value)
    return effective


def _validate_matching_candidate_identity(
    matches: list[tuple[MutableMapping[str, object], dict[str, object], str]],
    *,
    expected_listing_id: str,
    expected_property_url: str,
    expected_property_url_sha256: str,
    expected_provider_key: str,
) -> None:
    anchored = False
    for candidate, defaults, _path in matches:
        effective = _effective_candidate(candidate, defaults)
        property_urls = set(_candidate_property_urls(effective))
        if not property_urls:
            raise PropertySearchTourBindingError("property_search_tour_listing_anchor_missing")
        if len(property_urls) != 1:
            raise PropertySearchTourBindingError("property_search_tour_candidate_url_conflict")
        property_url = next(iter(property_urls))
        if (
            property_url != expected_property_url
            or property_search_source_url_sha256(property_url)
            != expected_property_url_sha256
        ):
            raise PropertySearchTourBindingError("property_search_tour_candidate_url_mismatch")

        listing_ids = _candidate_listing_ids(effective)
        if listing_ids:
            if any(value != expected_listing_id for value in listing_ids):
                raise PropertySearchTourBindingError("property_search_tour_listing_id_mismatch")
        elif not _property_url_contains_listing_id(property_url, expected_listing_id):
            raise PropertySearchTourBindingError("property_search_tour_listing_anchor_missing")

        provider_keys = set(_candidate_provider_keys(effective))
        if not provider_keys:
            raise PropertySearchTourBindingError("property_search_tour_provider_anchor_missing")
        if len(provider_keys) != 1 or expected_provider_key not in provider_keys:
            raise PropertySearchTourBindingError("property_search_tour_provider_identity_conflict")
        anchored = True
    if not anchored:
        raise PropertySearchTourBindingError("property_search_tour_listing_anchor_missing")


def _apply_candidate_tour_fields(
    candidate: MutableMapping[str, object],
    *,
    candidate_ref: str,
    generated_reconstruction_url: str,
    reconstruction_kind: str,
    disclosure: str,
    bound_at: str,
) -> bool:
    provider = _tour_provider_for_reconstruction_kind(reconstruction_kind)
    desired: dict[str, object] = {
        "candidate_ref": candidate_ref,
        "generated_reconstruction_url": generated_reconstruction_url,
        "generated_reconstruction_kind": reconstruction_kind,
        "tour_status": "ready",
        "tour_progress_pct": 100,
        "tour_eta_minutes": 0,
        "tour_provider": provider,
    }
    if disclosure:
        desired["generated_reconstruction_disclosure"] = disclosure
    changed = any(candidate.get(key) != value for key, value in desired.items())
    if (
        not disclosure
        and _normalized_text(candidate.get("generated_reconstruction_disclosure"))
    ):
        changed = True
    for key in ("blocked_reason", "tour_reason", "tour_reason_key"):
        if key in candidate:
            changed = True
    raw_tour = candidate.get("tour")
    tour = dict(raw_tour) if isinstance(raw_tour, Mapping) else {}
    desired_tour: dict[str, object] = {
        "status": "ready",
        "progress_pct": 100,
        "provider": provider,
        "generated_reconstruction_url": generated_reconstruction_url,
        "reconstruction_kind": reconstruction_kind,
    }
    if disclosure:
        desired_tour["disclosure"] = disclosure
    if any(tour.get(key) != value for key, value in desired_tour.items()):
        changed = True
    if not disclosure and _normalized_text(tour.get("disclosure")):
        changed = True
    for key in ("blocked_reason", "reason", "reason_key"):
        if key in tour:
            changed = True
    if not changed:
        return False
    candidate.update(desired)
    if not disclosure:
        candidate.pop("generated_reconstruction_disclosure", None)
    for key in ("blocked_reason", "tour_reason", "tour_reason_key"):
        candidate.pop(key, None)
    tour.update(desired_tour)
    if not disclosure:
        tour.pop("disclosure", None)
    for key in ("blocked_reason", "reason", "reason_key"):
        tour.pop(key, None)
    candidate["tour"] = tour
    candidate["tour_status_updated_at"] = bound_at
    return True


def _tour_provider_for_reconstruction_kind(reconstruction_kind: str) -> str:
    return (
        "propertyquarry_ai_360"
        if reconstruction_kind == "ai_panorama_360"
        else "propertyquarry_generated_reconstruction"
    )


def exact_property_search_candidate_tour_binding_receipt(
    record: Mapping[str, object],
    *,
    principal_id: str,
    run_id: str,
    candidate_ref: str,
    expected_listing_id: str,
    generated_reconstruction_url: str,
    reconstruction_kind: str = "ai_panorama_360",
    disclosure: str = "",
) -> dict[str, object] | None:
    """Return a receipt only for one complete, internally consistent saved binding.

    This is deliberately not an ownership fallback for a first or changed
    binding. It recognizes only an exact binding already stored in the
    principal-scoped terminal run, and it never mutates that run.
    """

    normalized_principal = _normalized_text(principal_id)
    normalized_run_id = _normalized_text(run_id)
    normalized_candidate_ref = _normalized_text(candidate_ref)
    normalized_listing_id = _normalized_text(expected_listing_id)
    normalized_url = _normalized_text(generated_reconstruction_url)
    normalized_kind = _normalized_text(reconstruction_kind).lower()
    normalized_disclosure = _normalized_text(disclosure)
    if not normalized_principal:
        raise PropertySearchTourBindingError("property_search_tour_principal_required")
    if not normalized_run_id:
        raise PropertySearchTourBindingError("property_search_tour_run_id_required")
    if not normalized_candidate_ref:
        raise PropertySearchTourBindingError("property_search_tour_candidate_ref_required")
    if not normalized_listing_id:
        raise PropertySearchTourBindingError("property_search_tour_listing_id_required")
    if not normalized_url:
        raise PropertySearchTourBindingError("property_search_tour_url_required")
    if normalized_kind not in {"ai_panorama_360", "layout_preview"}:
        raise PropertySearchTourBindingError(
            "property_search_tour_reconstruction_kind_invalid"
        )
    if _normalized_text(record.get("run_id")) != normalized_run_id:
        raise PropertySearchTourBindingError("property_search_tour_run_id_mismatch")
    if _normalized_text(record.get("principal_id")) != normalized_principal:
        raise PropertySearchTourBindingError("property_search_tour_principal_mismatch")
    if not property_search_tour_run_is_terminal(record):
        raise PropertySearchTourBindingError("property_search_tour_run_not_terminal")

    snapshot = copy.deepcopy(dict(record))
    occurrences = list(_candidate_occurrence_refs(snapshot))
    matches: list[tuple[MutableMapping[str, object], dict[str, object], str]] = []
    for candidate, defaults, path in occurrences:
        effective = _effective_candidate(candidate, defaults)
        if property_research_candidate_ref(effective) == normalized_candidate_ref:
            matches.append((candidate, defaults, path))
    if not matches:
        raise PropertySearchTourBindingError("property_search_tour_candidate_not_found")

    source_property_url = ""
    source_property_url_sha256 = ""
    source_provider_key = ""
    for candidate, defaults, _path in matches:
        effective = _effective_candidate(candidate, defaults)
        property_urls = set(_candidate_property_urls(effective))
        if not property_urls:
            raise PropertySearchTourBindingError(
                "property_search_tour_listing_anchor_missing"
            )
        if len(property_urls) != 1:
            raise PropertySearchTourBindingError(
                "property_search_tour_candidate_url_conflict"
            )
        property_url = next(iter(property_urls))
        listing_ids = _candidate_listing_ids(effective)
        if listing_ids:
            if any(value != normalized_listing_id for value in listing_ids):
                raise PropertySearchTourBindingError(
                    "property_search_tour_listing_id_mismatch"
                )
        elif not _property_url_contains_listing_id(
            property_url,
            normalized_listing_id,
        ):
            raise PropertySearchTourBindingError(
                "property_search_tour_listing_anchor_missing"
            )
        provider_keys = set(_candidate_provider_keys(effective))
        if not provider_keys:
            raise PropertySearchTourBindingError(
                "property_search_tour_provider_anchor_missing"
            )
        if len(provider_keys) != 1:
            raise PropertySearchTourBindingError(
                "property_search_tour_provider_identity_conflict"
            )
        provider_key = next(iter(provider_keys))
        property_url_sha256 = property_search_source_url_sha256(property_url)
        if not source_property_url:
            source_property_url = property_url
            source_property_url_sha256 = property_url_sha256
            source_provider_key = provider_key
        elif (
            property_url != source_property_url
            or property_url_sha256 != source_property_url_sha256
        ):
            raise PropertySearchTourBindingError(
                "property_search_tour_candidate_url_mismatch"
            )
        elif provider_key != source_provider_key:
            raise PropertySearchTourBindingError(
                "property_search_tour_provider_identity_conflict"
            )

    matched_paths = {path for _candidate, _defaults, path in matches}
    for candidate, defaults, path in occurrences:
        if path in matched_paths:
            continue
        effective = _effective_candidate(candidate, defaults)
        same_property_url = source_property_url in _candidate_property_urls(effective)
        same_provider_and_listing = (
            source_provider_key in _candidate_provider_keys(effective)
            and normalized_listing_id in _candidate_listing_ids(effective)
        )
        if same_property_url or same_provider_and_listing:
            raise PropertySearchTourBindingError(
                "property_search_tour_candidate_ref_identity_conflict"
            )

    provider = _tour_provider_for_reconstruction_kind(normalized_kind)
    expected_candidate_fields: dict[str, object] = {
        "candidate_ref": normalized_candidate_ref,
        "generated_reconstruction_url": normalized_url,
        "generated_reconstruction_kind": normalized_kind,
        "tour_status": "ready",
        "tour_progress_pct": 100,
        "tour_eta_minutes": 0,
        "tour_provider": provider,
    }
    expected_tour_fields: dict[str, object] = {
        "status": "ready",
        "progress_pct": 100,
        "provider": provider,
        "generated_reconstruction_url": normalized_url,
        "reconstruction_kind": normalized_kind,
    }
    bound_at_values: set[str] = set()
    for candidate, _defaults, _path in matches:
        if any(
            candidate.get(key) != value
            for key, value in expected_candidate_fields.items()
        ):
            return None
        if _normalized_text(
            candidate.get("generated_reconstruction_disclosure")
        ) != normalized_disclosure:
            return None
        if any(
            key in candidate
            for key in ("blocked_reason", "tour_reason", "tour_reason_key")
        ):
            return None
        tour = candidate.get("tour")
        if not isinstance(tour, Mapping):
            return None
        if any(tour.get(key) != value for key, value in expected_tour_fields.items()):
            return None
        if _normalized_text(tour.get("disclosure")) != normalized_disclosure:
            return None
        if any(key in tour for key in ("blocked_reason", "reason", "reason_key")):
            return None
        bound_at = _normalized_text(candidate.get("tour_status_updated_at"))
        if not bound_at:
            return None
        bound_at_values.add(bound_at)
    if len(bound_at_values) != 1:
        return None

    record_sha256 = property_search_run_record_sha256(record)
    return {
        "contract": "property_search_candidate_tour_binding_v1",
        "run_id": normalized_run_id,
        "candidate_ref": normalized_candidate_ref,
        "expected_listing_id": normalized_listing_id,
        "property_url_sha256": source_property_url_sha256,
        "provider_key": source_provider_key,
        "generated_reconstruction_url": normalized_url,
        "reconstruction_kind": normalized_kind,
        "before_sha256": record_sha256,
        "after_sha256": record_sha256,
        "changed": False,
        "occurrences_matched": len(matches),
        "occurrences_updated": 0,
        "changed_paths": [],
        "binding_verified_from": "principal_scoped_terminal_run",
    }


def authorize_property_search_candidate_tour_install(
    record: Mapping[str, object],
    *,
    principal_id: str,
    run_id: str,
    candidate_ref: str,
    expected_listing_id: str,
    expected_source_ref: str,
    bundle_identity: Mapping[str, object],
) -> dict[str, object]:
    """Verify a sealed install against one principal-scoped terminal run.

    This is intentionally read-only.  It proves that the request principal
    owns the durable run and that every occurrence of the requested candidate
    carries the same listing URL, provider, listing ID, and exact source
    reference as the sealed bundle.
    """

    normalized_principal = _normalized_text(principal_id)
    normalized_run_id = _normalized_text(run_id)
    normalized_candidate_ref = _normalized_text(candidate_ref)
    normalized_listing_id = _normalized_text(expected_listing_id)
    normalized_source_ref = _normalized_text(expected_source_ref)
    if not normalized_principal:
        raise PropertySearchTourBindingError("property_search_tour_principal_required")
    if not normalized_run_id:
        raise PropertySearchTourBindingError("property_search_tour_run_id_required")
    if not normalized_candidate_ref:
        raise PropertySearchTourBindingError("property_search_tour_candidate_ref_required")
    if not normalized_listing_id:
        raise PropertySearchTourBindingError("property_search_tour_listing_id_required")
    if not normalized_source_ref:
        raise PropertySearchTourBindingError("property_search_tour_source_ref_required")
    if _normalized_text(record.get("run_id")) != normalized_run_id:
        raise PropertySearchTourBindingError("property_search_tour_run_id_mismatch")
    if _normalized_text(record.get("principal_id")) != normalized_principal:
        raise PropertySearchTourBindingError("property_search_tour_principal_mismatch")
    if not property_search_tour_run_is_terminal(record):
        raise PropertySearchTourBindingError("property_search_tour_run_not_terminal")

    expected_identity = _validated_bundle_identity(
        bundle_identity,
        run_id=normalized_run_id,
        candidate_ref=normalized_candidate_ref,
        expected_listing_id=normalized_listing_id,
    )
    occurrences = list(_candidate_occurrence_refs(record))
    matches: list[tuple[MutableMapping[str, object], dict[str, object], str]] = []
    for candidate, defaults, path in occurrences:
        effective = _effective_candidate(candidate, defaults)
        if property_research_candidate_ref(effective) == normalized_candidate_ref:
            matches.append((candidate, defaults, path))
    if not matches:
        raise PropertySearchTourBindingError("property_search_tour_candidate_not_found")
    _validate_matching_candidate_identity(
        matches,
        expected_listing_id=normalized_listing_id,
        expected_property_url=expected_identity["property_url"],
        expected_property_url_sha256=expected_identity["property_url_sha256"],
        expected_provider_key=expected_identity["provider_key"],
    )
    for candidate, defaults, _path in matches:
        source_refs = _candidate_source_refs(_effective_candidate(candidate, defaults))
        if not source_refs:
            raise PropertySearchTourBindingError(
                "property_search_tour_candidate_source_ref_missing"
            )
        if any(value != normalized_source_ref for value in source_refs):
            raise PropertySearchTourBindingError(
                "property_search_tour_candidate_source_ref_mismatch"
            )

    matched_paths = {path for _candidate, _defaults, path in matches}
    for candidate, defaults, path in occurrences:
        if path in matched_paths:
            continue
        effective = _effective_candidate(candidate, defaults)
        same_property_url = (
            expected_identity["property_url"] in _candidate_property_urls(effective)
        )
        same_provider_and_listing = (
            expected_identity["provider_key"] in _candidate_provider_keys(effective)
            and normalized_listing_id in _candidate_listing_ids(effective)
        )
        if same_property_url or same_provider_and_listing:
            raise PropertySearchTourBindingError(
                "property_search_tour_candidate_ref_identity_conflict"
            )

    return {
        "contract": "property_search_candidate_tour_install_authority_v1",
        "record_sha256": property_search_run_record_sha256(record),
        "property_url_sha256": expected_identity["property_url_sha256"],
        "provider_key": expected_identity["provider_key"],
        "occurrences_matched": len(matches),
        "principal_binding_verified": True,
        "run_binding_verified": True,
        "candidate_binding_verified": True,
        "listing_identity_verified": True,
        "source_identity_verified": True,
        "run_terminal_verified": True,
        "private_values_redacted": True,
    }


def plan_property_search_candidate_tour_binding(
    record: Mapping[str, object],
    *,
    principal_id: str,
    run_id: str,
    candidate_ref: str,
    expected_listing_id: str,
    generated_reconstruction_url: str,
    bundle_identity: Mapping[str, object],
    reconstruction_kind: str = "ai_panorama_360",
    disclosure: str = "",
    bound_at: str = "",
) -> tuple[dict[str, object], dict[str, object]]:
    normalized_principal = _normalized_text(principal_id)
    normalized_run_id = _normalized_text(run_id)
    normalized_candidate_ref = _normalized_text(candidate_ref)
    normalized_listing_id = _normalized_text(expected_listing_id)
    normalized_url = _normalized_text(generated_reconstruction_url)
    normalized_kind = _normalized_text(reconstruction_kind).lower()
    if not normalized_principal:
        raise PropertySearchTourBindingError("property_search_tour_principal_required")
    if not normalized_run_id:
        raise PropertySearchTourBindingError("property_search_tour_run_id_required")
    if not normalized_candidate_ref:
        raise PropertySearchTourBindingError("property_search_tour_candidate_ref_required")
    if not normalized_listing_id:
        raise PropertySearchTourBindingError("property_search_tour_listing_id_required")
    if not normalized_url:
        raise PropertySearchTourBindingError("property_search_tour_url_required")
    if normalized_kind not in {"ai_panorama_360", "layout_preview"}:
        raise PropertySearchTourBindingError("property_search_tour_reconstruction_kind_invalid")
    if _normalized_text(record.get("run_id")) != normalized_run_id:
        raise PropertySearchTourBindingError("property_search_tour_run_id_mismatch")
    if _normalized_text(record.get("principal_id")) != normalized_principal:
        raise PropertySearchTourBindingError("property_search_tour_principal_mismatch")

    expected_identity = _validated_bundle_identity(
        bundle_identity,
        run_id=normalized_run_id,
        candidate_ref=normalized_candidate_ref,
        expected_listing_id=normalized_listing_id,
    )

    updated = copy.deepcopy(dict(record))
    occurrences = list(_candidate_occurrence_refs(updated))
    matches: list[tuple[MutableMapping[str, object], dict[str, object], str]] = []
    for candidate, defaults, path in occurrences:
        effective = _effective_candidate(candidate, defaults)
        if property_research_candidate_ref(effective) == normalized_candidate_ref:
            matches.append((candidate, defaults, path))
    if not matches:
        raise PropertySearchTourBindingError("property_search_tour_candidate_not_found")
    _validate_matching_candidate_identity(
        matches,
        expected_listing_id=normalized_listing_id,
        expected_property_url=expected_identity["property_url"],
        expected_property_url_sha256=expected_identity["property_url_sha256"],
        expected_provider_key=expected_identity["provider_key"],
    )
    matched_paths = {path for _candidate, _defaults, path in matches}
    for candidate, defaults, path in occurrences:
        if path in matched_paths:
            continue
        effective = _effective_candidate(candidate, defaults)
        same_property_url = (
            expected_identity["property_url"] in _candidate_property_urls(effective)
        )
        same_provider_and_listing = (
            expected_identity["provider_key"] in _candidate_provider_keys(effective)
            and normalized_listing_id in _candidate_listing_ids(effective)
        )
        if same_property_url or same_provider_and_listing:
            raise PropertySearchTourBindingError(
                "property_search_tour_candidate_ref_identity_conflict"
            )

    before_sha256 = property_search_run_record_sha256(record)
    timestamp = _normalized_text(bound_at) or datetime.now(timezone.utc).isoformat()
    changed_paths: list[str] = []
    for candidate, _defaults, path in matches:
        if _apply_candidate_tour_fields(
            candidate,
            candidate_ref=normalized_candidate_ref,
            generated_reconstruction_url=normalized_url,
            reconstruction_kind=normalized_kind,
            disclosure=_normalized_text(disclosure),
            bound_at=timestamp,
        ):
            changed_paths.append(path)
    bound_at_values = {
        _normalized_text(candidate.get("tour_status_updated_at"))
        for candidate, _defaults, _path in matches
    }
    if changed_paths or len(bound_at_values) != 1 or "" in bound_at_values:
        for candidate, _defaults, path in matches:
            if candidate.get("tour_status_updated_at") != timestamp:
                candidate["tour_status_updated_at"] = timestamp
                if path not in changed_paths:
                    changed_paths.append(path)
    changed = bool(changed_paths)
    if changed:
        updated["updated_at"] = timestamp
    after_sha256 = property_search_run_record_sha256(updated)
    receipt = {
        "contract": "property_search_candidate_tour_binding_v1",
        "run_id": normalized_run_id,
        "candidate_ref": normalized_candidate_ref,
        "expected_listing_id": normalized_listing_id,
        "property_url_sha256": expected_identity["property_url_sha256"],
        "provider_key": expected_identity["provider_key"],
        "generated_reconstruction_url": normalized_url,
        "reconstruction_kind": normalized_kind,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "changed": changed,
        "occurrences_matched": len(matches),
        "occurrences_updated": len(changed_paths),
        "changed_paths": changed_paths,
    }
    return updated, receipt
