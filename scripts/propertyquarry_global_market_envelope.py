#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__:
    from scripts.propertyquarry_global_governance_attestation import (
        GLOBAL_MARKET_GATE_ID,
        GlobalGovernanceAttestationError,
        verify_global_governance_attestation,
    )
    from scripts.propertyquarry_strict_json import (
        StrictJsonError,
        load_strict_json_object_snapshot,
    )
else:
    from propertyquarry_global_governance_attestation import (  # type: ignore[no-redef]
        GLOBAL_MARKET_GATE_ID,
        GlobalGovernanceAttestationError,
        verify_global_governance_attestation,
    )
    from propertyquarry_strict_json import (  # type: ignore[no-redef]
        StrictJsonError,
        load_strict_json_object_snapshot,
    )


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
DEFAULT_ENVELOPE_PATH = ROOT / "docs" / "propertyquarry_global_market_envelope.v1.json"

ENVELOPE_SCHEMA = "propertyquarry.global_market_envelope.v1"
RECEIPT_SCHEMA = "propertyquarry.global_market_envelope_receipt.v1"
LIVE_RECEIPT_SCHEMA = "propertyquarry.global_market_envelope_live_receipt.v1"
REQUIRED_MARKETS = ("AT", "DE", "CR")
EXPECTED_MARKET_SEMANTICS = {
    "AT": {
        "market_language": "de-AT",
        "currency_code": "EUR",
        "default_timezone": "Europe/Vienna",
    },
    "DE": {
        "market_language": "de-DE",
        "currency_code": "EUR",
        "default_timezone": "Europe/Berlin",
    },
    "CR": {
        "market_language": "es-CR",
        "currency_code": "CRC",
        "default_timezone": "America/Costa_Rica",
    },
}
MARKET_CONTRACT_FIELDS = (
    "accepted_content_languages",
    "measurement_system",
    "timezone_policy",
    "address_model",
    "provider_set",
    "listing_modes",
    "privacy_region",
    "support_window",
)
CONTENT_LANGUAGE_FIELDS = (
    "listing_content",
    "customer_ui",
    "fallback",
)
MEASUREMENT_SYSTEM_FIELDS = (
    "system",
    "area_unit",
    "distance_units",
)
TIMEZONE_POLICY_FIELDS = (
    "storage_timezone",
    "display_timezone",
    "rules_source",
)
ADDRESS_MODEL_FIELDS = (
    "administrative_levels",
    "entry_fields",
    "postal_code_pattern",
    "freeform_fallback",
)
PROVIDER_SET_FIELDS = ("policy", "providers")
PROVIDER_ENTRY_FIELDS = ("provider_id", "listing_modes")
PRIVACY_REGION_FIELDS = (
    "jurisdiction_code",
    "region_code",
    "verification_status",
)
SUPPORT_WINDOW_FIELDS = (
    "coverage_status",
    "timezone",
    "weekly_windows",
    "channels",
)
SUPPORT_WINDOW_ENTRY_FIELDS = ("days", "start_local", "end_local")
ALLOWED_LISTING_MODES = ("rent", "buy")
ALLOWED_SUPPORT_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALLOWED_SUPPORT_CHANNELS = ("email", "telegram", "whatsapp", "web")
ALLOWED_SUPPORT_COVERAGE = (
    "not_committed",
    "committed_business_hours",
    "committed_24x7",
)
EXPECTED_CONTENT_LANGUAGE_CONTRACTS = {
    "AT": {
        "listing_content": ["de-AT", "de", "en"],
        "customer_ui": ["en"],
        "fallback": "en",
    },
    "DE": {
        "listing_content": ["de-DE", "de", "en"],
        "customer_ui": ["en"],
        "fallback": "en",
    },
    "CR": {
        "listing_content": ["es-CR", "es", "en"],
        "customer_ui": ["en"],
        "fallback": "en",
    },
}
EXPECTED_MEASUREMENT_SYSTEM = {
    "system": "metric",
    "area_unit": "m2",
    "distance_units": ["m", "km"],
}
EXPECTED_ADDRESS_MODELS = {
    "AT": {
        "administrative_levels": [
            "country",
            "federal_state",
            "district_or_municipality",
            "postal_code",
            "street",
            "unit",
        ],
        "entry_fields": ["country_code", "region_code", "location_query"],
        "postal_code_pattern": "^[0-9]{4}$",
        "freeform_fallback": True,
    },
    "DE": {
        "administrative_levels": [
            "country",
            "federal_state_or_city",
            "locality_or_district",
            "postal_code",
            "street",
            "unit",
        ],
        "entry_fields": ["country_code", "region_code", "location_query"],
        "postal_code_pattern": "^[0-9]{5}$",
        "freeform_fallback": True,
    },
    "CR": {
        "administrative_levels": [
            "country",
            "province",
            "canton",
            "district",
            "postal_code",
            "street_or_landmark",
            "unit",
        ],
        "entry_fields": ["country_code", "region_code", "location_query"],
        "postal_code_pattern": "^[0-9]{5}$",
        "freeform_fallback": True,
    },
}
EXPECTED_PRIVACY_REGIONS = {
    "AT": ("AT", "EU_EEA"),
    "DE": ("DE", "EU_EEA"),
    "CR": ("CR", "CR"),
}
ALLOWED_CLASSIFICATIONS = (
    "launch_supported",
    "private_beta",
    "preview",
    "catalog",
    "browser_state_only",
)
ALLOWED_DIMENSION_STATUSES = (
    "proven",
    "implemented_unproven",
    "missing",
    "external_blocked",
    "not_applicable",
)
WORKFLOW_DIMENSIONS = {
    "buyer_decision_support": "buyer_journey",
    "renter_discovery": "renter_journey",
    "seller_supply": "seller_journey",
}
ALL_DIMENSIONS = (
    "market_catalog",
    "content_locale",
    "address_region",
    "currency_number",
    "date_timezone",
    "rtl_layout",
    "text_expansion",
    "wcag_22_aa",
    "manual_assistive_technology",
    "responsive_devices",
    "browser_matrix",
    "seo_discoverability",
    "performance",
    "degraded_network",
    "provider_rights",
    "live_provider_e2e",
    "market_browser_journey",
    "buyer_journey",
    "renter_journey",
    "seller_journey",
)
MANDATORY_LAUNCH_DIMENSIONS = (
    "market_catalog",
    "content_locale",
    "address_region",
    "currency_number",
    "date_timezone",
    "text_expansion",
    "wcag_22_aa",
    "manual_assistive_technology",
    "responsive_devices",
    "browser_matrix",
    "seo_discoverability",
    "performance",
    "degraded_network",
    "provider_rights",
    "live_provider_e2e",
    "market_browser_journey",
)


class EnvelopeError(ValueError):
    pass


_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPAQUE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{11,255}$")
_PLACEHOLDER_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:placeholder|example|unconfigured|unassigned|unknown|todo|tbd|fake|mock|demo|sample|test|invalid|changeme|dummy|pending|localhost)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)
_LANGUAGE_TAG_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
_LOCAL_TIME_RE = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


def _exact_git_sha(value: object) -> bool:
    raw = str(value or "").strip().lower()
    return bool(_GIT_SHA_RE.fullmatch(raw) and len(set(raw)) >= 4)


def _exact_image_digest(value: object) -> bool:
    raw = str(value or "").strip().lower()
    payload = raw.removeprefix("sha256:")
    return bool(_IMAGE_DIGEST_RE.fullmatch(raw) and len(set(payload)) >= 4)


def _opaque_ref(value: object) -> bool:
    raw = str(value or "").strip()
    return bool(_OPAQUE_REF_RE.fullmatch(raw) and _PLACEHOLDER_RE.search(raw) is None)


def _parse_time(value: object) -> datetime | None:
    raw = str(value or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _attested_payload_digest(receipt: Mapping[str, Any]) -> str:
    payload = dict(receipt)
    payload.pop("independent_attestation", None)
    return "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _fresh_evidence(
    value: object,
    *,
    now: datetime,
    maximum_age_hours: float,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    observed_at = _parse_time(value.get("observed_at"))
    if observed_at is None:
        return False
    age_seconds = (now - observed_at).total_seconds()
    return (
        value.get("status") == "pass"
        and -300 <= age_seconds <= maximum_age_hours * 3600
        and _exact_image_digest(value.get("evidence_digest"))
        and _opaque_ref(value.get("workflow_ref"))
    )


def _validate_live_launch_evidence(
    live: Mapping[str, Any] | None,
    *,
    source: dict[str, Any],
    source_sha256: str,
    expected_release_sha: str,
    expected_image_digest: str,
    now: datetime,
    maximum_age_hours: float = 24.0,
) -> tuple[list[str], bool, float | None]:
    if live is None:
        return ["fresh independently attested live market evidence is required"], False, None
    errors: list[str] = []
    if live.get("schema") != LIVE_RECEIPT_SCHEMA:
        errors.append("live market evidence has the wrong schema")
    if live.get("profile") != "launch" or live.get("claim_scope") != "core":
        errors.append("live market evidence must be launch-tier Core evidence")
    if live.get("source_envelope_id") != source.get("envelope_id"):
        errors.append("live market evidence targets a different source envelope")
    if str(live.get("source_sha256") or "").strip().lower() != source_sha256:
        errors.append("live market evidence is not bound to the current source envelope")
    identity = live.get("release_identity") if isinstance(live.get("release_identity"), Mapping) else {}
    if str(identity.get("commit_sha") or "").strip().lower() != expected_release_sha:
        errors.append("live market evidence commit does not match the expected release")
    if str(identity.get("image_digest") or "").strip().lower() != expected_image_digest:
        errors.append("live market evidence image does not match the expected release")
    generated_at = _parse_time(live.get("generated_at"))
    age_seconds: float | None = None
    if generated_at is None:
        errors.append("live market evidence has no timezone-aware generated_at")
    else:
        age_seconds = (now - generated_at).total_seconds()
        if age_seconds < -300 or age_seconds > maximum_age_hours * 3600:
            errors.append("live market evidence is stale or future-dated")

    market_rows = live.get("markets")
    market_index: dict[str, Mapping[str, Any]] = {}
    rows_valid = isinstance(market_rows, list)
    for row in market_rows if isinstance(market_rows, list) else []:
        if not isinstance(row, Mapping):
            rows_valid = False
            continue
        country_code = str(row.get("country_code") or "").strip().upper()
        if not country_code or country_code in market_index:
            rows_valid = False
            continue
        market_index[country_code] = row
    if not rows_valid or set(market_index) != set(REQUIRED_MARKETS):
        errors.append("live market evidence must contain exactly AT, DE, and CR once each")
    source_markets = {
        str(row.get("country_code") or "").strip().upper(): row
        for row in source.get("markets") or []
        if isinstance(row, dict)
    }
    for country_code in REQUIRED_MARKETS:
        source_market = source_markets.get(country_code, {})
        required_dimensions = set(_market_required_dimensions(source_market)) if source_market else set()
        live_market = market_index.get(country_code, {})
        dimensions = live_market.get("dimensions")
        dimension_index: dict[str, Mapping[str, Any]] = {}
        dimensions_valid = isinstance(dimensions, list)
        for row in dimensions if isinstance(dimensions, list) else []:
            if not isinstance(row, Mapping):
                dimensions_valid = False
                continue
            dimension = str(row.get("dimension") or "").strip()
            if not dimension or dimension in dimension_index:
                dimensions_valid = False
                continue
            dimension_index[dimension] = row
        if not dimensions_valid or set(dimension_index) != required_dimensions:
            errors.append(f"live market evidence {country_code} has an incomplete dimension set")
        for dimension in sorted(required_dimensions):
            if not _fresh_evidence(
                dimension_index.get(dimension),
                now=now,
                maximum_age_hours=maximum_age_hours,
            ):
                errors.append(f"live market evidence {country_code}/{dimension} is not fresh pass evidence")

    attestation = (
        live.get("independent_attestation")
        if isinstance(live.get("independent_attestation"), Mapping)
        else {}
    )
    try:
        verify_global_governance_attestation(
            attestation,
            expected_subject={
                "gate_id": GLOBAL_MARKET_GATE_ID,
                "receipt_contract": LIVE_RECEIPT_SCHEMA,
                "release_commit_sha": expected_release_sha,
                "release_image_digest": expected_image_digest,
                "source_digests": {
                    "market_envelope_sha256": f"sha256:{source_sha256}",
                },
                "payload_sha256": _attested_payload_digest(live),
            },
            observed_at=now,
        )
    except GlobalGovernanceAttestationError:
        independently_attested = False
    else:
        independently_attested = True
    if not independently_attested:
        errors.append("live market evidence lacks independent exact-release payload attestation")
    return list(dict.fromkeys(errors)), independently_attested, age_seconds


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_envelope(path: Path = DEFAULT_ENVELOPE_PATH) -> dict[str, Any]:
    try:
        payload, _raw, _digest = load_strict_json_object_snapshot(
            path,
            field="global market envelope",
            maximum_bytes=4 * 1024 * 1024,
        )
    except (OSError, StrictJsonError) as exc:
        raise EnvelopeError(f"envelope_unreadable:{path}:{type(exc).__name__}") from exc
    return payload


def _nonempty_strings(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise EnvelopeError(f"{field}_must_be_list")
    resolved = [str(item or "").strip() for item in value]
    if any(not item for item in resolved):
        raise EnvelopeError(f"{field}_contains_empty_value")
    if len(resolved) != len(set(resolved)):
        raise EnvelopeError(f"{field}_contains_duplicate")
    return resolved


def _closed_object(
    value: object,
    *,
    field: str,
    fields: tuple[str, ...],
) -> dict[str, Any]:
    if type(value) is not dict:
        raise EnvelopeError(f"{field}_must_be_object")
    resolved = value
    if tuple(resolved) != fields:
        raise EnvelopeError(
            f"{field}_fields_must_be_exact_ordered_set:"
            + ",".join(str(key) for key in resolved)
        )
    return resolved


def _strict_nonempty_strings(value: object, *, field: str) -> list[str]:
    if type(value) is not list:
        raise EnvelopeError(f"{field}_must_be_list")
    if any(type(item) is not str for item in value):
        raise EnvelopeError(f"{field}_must_contain_strings")
    resolved = [item.strip() for item in value]
    if any(not item for item in resolved):
        raise EnvelopeError(f"{field}_contains_empty_value")
    if len(resolved) != len(set(resolved)):
        raise EnvelopeError(f"{field}_contains_duplicate")
    return resolved


def _governed_provider_contracts() -> dict[str, dict[str, Any]]:
    ea_root = str(EA_ROOT)
    if not EA_ROOT.is_dir():
        raise EnvelopeError(f"governed_provider_catalog_root_missing:{EA_ROOT}")
    if ea_root not in sys.path:
        sys.path.insert(0, ea_root)
    try:
        from app.services import property_market_catalog as governed_catalog
    except Exception as exc:
        raise EnvelopeError(
            f"governed_provider_catalog_unavailable:{type(exc).__name__}"
        ) from exc
    expected_catalog_path = (EA_ROOT / "app" / "services" / "property_market_catalog.py").resolve()
    observed_catalog_path = Path(str(getattr(governed_catalog, "__file__", ""))).resolve()
    if observed_catalog_path != expected_catalog_path:
        raise EnvelopeError(
            "governed_provider_catalog_origin_mismatch:"
            f"expected={expected_catalog_path}:observed={observed_catalog_path}"
        )

    contracts: dict[str, dict[str, Any]] = {}
    for provider in governed_catalog.PROVIDERS:
        raw_provider_id = getattr(provider, "key", "")
        raw_country_code = getattr(provider, "country_code", "")
        raw_supported_modes = getattr(provider, "supported_listing_modes", ())
        raw_search_ready = getattr(provider, "search_ready", None)
        if type(raw_provider_id) is not str or type(raw_country_code) is not str:
            raise EnvelopeError("governed_provider_catalog_identity_type_invalid")
        if type(raw_supported_modes) is not tuple or type(raw_search_ready) is not bool:
            raise EnvelopeError(
                f"governed_provider_catalog_contract_type_invalid:{raw_provider_id}"
            )
        if any(type(mode) is not str for mode in raw_supported_modes):
            raise EnvelopeError(
                f"governed_provider_catalog_modes_type_invalid:{raw_provider_id}"
            )
        provider_id = raw_provider_id.strip()
        country_code = raw_country_code.strip().upper()
        supported_modes = tuple(
            mode.strip().lower()
            for mode in raw_supported_modes
            if mode.strip()
        )
        if not provider_id or provider_id in contracts:
            raise EnvelopeError(
                f"governed_provider_catalog_duplicate_or_empty:{provider_id or '<empty>'}"
            )
        if re.fullmatch(r"[A-Z]{2}", country_code) is None:
            raise EnvelopeError(
                f"governed_provider_catalog_country_invalid:{provider_id}:{country_code}"
            )
        if (
            not supported_modes
            or len(supported_modes) != len(set(supported_modes))
            or any(mode not in ALLOWED_LISTING_MODES for mode in supported_modes)
        ):
            raise EnvelopeError(
                f"governed_provider_catalog_modes_invalid:{provider_id}:"
                + ",".join(supported_modes)
            )
        contracts[provider_id] = {
            "country_code": country_code,
            "listing_modes": supported_modes,
            "search_ready": raw_search_ready,
        }
    return contracts


def _validate_market_contract(
    market: dict[str, Any],
    *,
    phase_content_language: str,
    operating_mode: str,
) -> None:
    country_code = str(market.get("country_code") or "").strip().upper()
    contract = _closed_object(
        market.get("market_contract"),
        field=f"market_contract:{country_code}",
        fields=MARKET_CONTRACT_FIELDS,
    )

    languages = _closed_object(
        contract["accepted_content_languages"],
        field=f"accepted_content_languages:{country_code}",
        fields=CONTENT_LANGUAGE_FIELDS,
    )
    listing_languages = _strict_nonempty_strings(
        languages["listing_content"],
        field=f"listing_content_languages:{country_code}",
    )
    customer_ui_languages = _strict_nonempty_strings(
        languages["customer_ui"],
        field=f"customer_ui_languages:{country_code}",
    )
    fallback_language = languages["fallback"]
    if type(fallback_language) is not str or not fallback_language.strip():
        raise EnvelopeError(f"content_language_fallback_must_be_string:{country_code}")
    all_languages = [*listing_languages, *customer_ui_languages, fallback_language]
    if any(_LANGUAGE_TAG_RE.fullmatch(language) is None for language in all_languages):
        raise EnvelopeError(f"content_language_tag_invalid:{country_code}")
    if phase_content_language not in customer_ui_languages or fallback_language != phase_content_language:
        raise EnvelopeError(f"content_language_phase_mismatch:{country_code}")
    if languages != EXPECTED_CONTENT_LANGUAGE_CONTRACTS[country_code]:
        raise EnvelopeError(f"content_language_semantics_mismatch:{country_code}")

    measurement = _closed_object(
        contract["measurement_system"],
        field=f"measurement_system:{country_code}",
        fields=MEASUREMENT_SYSTEM_FIELDS,
    )
    _strict_nonempty_strings(
        measurement["distance_units"],
        field=f"measurement_distance_units:{country_code}",
    )
    if measurement != EXPECTED_MEASUREMENT_SYSTEM:
        raise EnvelopeError(f"measurement_system_semantics_mismatch:{country_code}")

    timezone_policy = _closed_object(
        contract["timezone_policy"],
        field=f"timezone_policy:{country_code}",
        fields=TIMEZONE_POLICY_FIELDS,
    )
    expected_timezone = str(market.get("default_timezone") or "").strip()
    if timezone_policy != {
        "storage_timezone": "UTC",
        "display_timezone": expected_timezone,
        "rules_source": "iana_tzdb",
    }:
        raise EnvelopeError(f"timezone_policy_semantics_mismatch:{country_code}")

    address_model = _closed_object(
        contract["address_model"],
        field=f"address_model:{country_code}",
        fields=ADDRESS_MODEL_FIELDS,
    )
    _strict_nonempty_strings(
        address_model["administrative_levels"],
        field=f"address_administrative_levels:{country_code}",
    )
    _strict_nonempty_strings(
        address_model["entry_fields"],
        field=f"address_entry_fields:{country_code}",
    )
    if type(address_model["postal_code_pattern"]) is not str:
        raise EnvelopeError(f"address_postal_code_pattern_must_be_string:{country_code}")
    try:
        re.compile(address_model["postal_code_pattern"])
    except re.error as exc:
        raise EnvelopeError(f"address_postal_code_pattern_invalid:{country_code}") from exc
    if type(address_model["freeform_fallback"]) is not bool:
        raise EnvelopeError(f"address_freeform_fallback_must_be_boolean:{country_code}")
    if address_model != EXPECTED_ADDRESS_MODELS[country_code]:
        raise EnvelopeError(f"address_model_semantics_mismatch:{country_code}")

    listing_modes = _strict_nonempty_strings(
        contract["listing_modes"],
        field=f"market_listing_modes:{country_code}",
    )
    if any(mode not in ALLOWED_LISTING_MODES for mode in listing_modes):
        raise EnvelopeError(f"market_listing_mode_invalid:{country_code}")
    claims = market["workflow_claims"]
    expected_listing_modes = [
        mode
        for mode, workflow in (
            ("rent", "renter_discovery"),
            ("buy", "buyer_decision_support"),
        )
        if claims[workflow] is True
    ]
    if listing_modes != expected_listing_modes:
        raise EnvelopeError(
            f"market_listing_modes_workflow_mismatch:{country_code}:"
            f"expected={','.join(expected_listing_modes)}:observed={','.join(listing_modes)}"
        )

    provider_set = _closed_object(
        contract["provider_set"],
        field=f"provider_set:{country_code}",
        fields=PROVIDER_SET_FIELDS,
    )
    if provider_set["policy"] != "search_ready_allowlist":
        raise EnvelopeError(f"provider_set_policy_invalid:{country_code}")
    provider_rows = provider_set["providers"]
    if type(provider_rows) is not list or not provider_rows:
        raise EnvelopeError(f"provider_set_providers_must_be_nonempty_list:{country_code}")
    provider_catalog = _governed_provider_contracts()
    seen_providers: set[str] = set()
    covered_modes: set[str] = set()
    for index, raw_provider in enumerate(provider_rows):
        provider = _closed_object(
            raw_provider,
            field=f"provider_set_entry:{country_code}:{index}",
            fields=PROVIDER_ENTRY_FIELDS,
        )
        provider_id = provider["provider_id"]
        if type(provider_id) is not str or not provider_id.strip():
            raise EnvelopeError(f"market_provider_id_must_be_string:{country_code}:{index}")
        provider_id = provider_id.strip()
        if provider_id in seen_providers:
            raise EnvelopeError(f"market_provider_duplicate:{country_code}:{provider_id}")
        seen_providers.add(provider_id)
        catalog_contract = provider_catalog.get(provider_id)
        if catalog_contract is None:
            raise EnvelopeError(f"market_provider_unknown:{country_code}:{provider_id}")
        provider_country = str(catalog_contract["country_code"])
        if provider_country != country_code:
            raise EnvelopeError(
                f"market_provider_country_mismatch:{country_code}:{provider_id}:"
                f"expected={country_code}:observed={provider_country}"
            )
        if catalog_contract["search_ready"] is not True:
            raise EnvelopeError(f"market_provider_not_search_ready:{country_code}:{provider_id}")
        declared_provider_modes = _strict_nonempty_strings(
            provider["listing_modes"],
            field=f"market_provider_listing_modes:{country_code}:{provider_id}",
        )
        expected_provider_modes = [
            mode
            for mode in listing_modes
            if mode in set(catalog_contract["listing_modes"])
        ]
        if declared_provider_modes != expected_provider_modes:
            raise EnvelopeError(
                f"market_provider_listing_modes_mismatch:{country_code}:{provider_id}:"
                f"expected={','.join(expected_provider_modes)}:"
                f"observed={','.join(declared_provider_modes)}"
            )
        covered_modes.update(declared_provider_modes)
    if covered_modes != set(listing_modes):
        raise EnvelopeError(
            f"market_provider_mode_coverage_incomplete:{country_code}:"
            f"expected={','.join(listing_modes)}:observed={','.join(sorted(covered_modes))}"
        )

    privacy_region = _closed_object(
        contract["privacy_region"],
        field=f"privacy_region:{country_code}",
        fields=PRIVACY_REGION_FIELDS,
    )
    expected_jurisdiction, expected_region = EXPECTED_PRIVACY_REGIONS[country_code]
    if (
        privacy_region["jurisdiction_code"] != expected_jurisdiction
        or privacy_region["region_code"] != expected_region
    ):
        raise EnvelopeError(f"privacy_region_semantics_mismatch:{country_code}")
    if (
        type(privacy_region["verification_status"]) is not str
        or privacy_region["verification_status"] not in {"external_unverified", "verified"}
    ):
        raise EnvelopeError(f"privacy_region_verification_status_invalid:{country_code}")

    support_window = _closed_object(
        contract["support_window"],
        field=f"support_window:{country_code}",
        fields=SUPPORT_WINDOW_FIELDS,
    )
    coverage_status = support_window["coverage_status"]
    if type(coverage_status) is not str or coverage_status not in ALLOWED_SUPPORT_COVERAGE:
        raise EnvelopeError(f"support_window_coverage_status_invalid:{country_code}")
    if support_window["timezone"] != expected_timezone:
        raise EnvelopeError(f"support_window_timezone_mismatch:{country_code}")
    raw_channels = support_window["channels"]
    if type(raw_channels) is not list:
        raise EnvelopeError(f"support_window_channels:{country_code}_must_be_list")
    channels = (
        _strict_nonempty_strings(
            raw_channels,
            field=f"support_window_channels:{country_code}",
        )
        if raw_channels
        else []
    )
    if any(channel not in ALLOWED_SUPPORT_CHANNELS for channel in channels):
        raise EnvelopeError(f"support_window_channel_invalid:{country_code}")
    weekly_windows = support_window["weekly_windows"]
    if type(weekly_windows) is not list:
        raise EnvelopeError(f"support_window_weekly_windows_must_be_list:{country_code}")
    covered_days: set[str] = set()
    for index, raw_window in enumerate(weekly_windows):
        window = _closed_object(
            raw_window,
            field=f"support_window_entry:{country_code}:{index}",
            fields=SUPPORT_WINDOW_ENTRY_FIELDS,
        )
        days = _strict_nonempty_strings(
            window["days"],
            field=f"support_window_days:{country_code}:{index}",
        )
        if any(day not in ALLOWED_SUPPORT_DAYS for day in days):
            raise EnvelopeError(f"support_window_day_invalid:{country_code}:{index}")
        if covered_days.intersection(days):
            raise EnvelopeError(f"support_window_day_duplicate:{country_code}:{index}")
        covered_days.update(days)
        start_local = window["start_local"]
        end_local = window["end_local"]
        if (
            type(start_local) is not str
            or type(end_local) is not str
            or _LOCAL_TIME_RE.fullmatch(start_local) is None
            or _LOCAL_TIME_RE.fullmatch(end_local) is None
            or start_local >= end_local
        ):
            raise EnvelopeError(f"support_window_time_invalid:{country_code}:{index}")
    if coverage_status == "not_committed" and (weekly_windows or channels):
        raise EnvelopeError(f"uncommitted_support_window_must_be_empty:{country_code}")
    if coverage_status == "committed_business_hours" and (not weekly_windows or not channels):
        raise EnvelopeError(f"business_hours_support_window_incomplete:{country_code}")
    if coverage_status == "committed_24x7" and (weekly_windows or not channels):
        raise EnvelopeError(f"continuous_support_window_invalid:{country_code}")
    if operating_mode == "launch":
        if privacy_region["verification_status"] != "verified":
            raise EnvelopeError(f"launch_requires_verified_privacy_region:{country_code}")
        if coverage_status == "not_committed":
            raise EnvelopeError(f"launch_requires_committed_support_window:{country_code}")


def _market_required_dimensions(market: dict[str, Any]) -> tuple[str, ...]:
    required = list(MANDATORY_LAUNCH_DIMENSIONS)
    claims = market["workflow_claims"]
    for workflow, dimension in WORKFLOW_DIMENSIONS.items():
        if claims[workflow] is True:
            required.append(dimension)
    return tuple(dict.fromkeys(required))


def validate_envelope(payload: dict[str, Any]) -> None:
    if payload.get("schema") != ENVELOPE_SCHEMA:
        raise EnvelopeError(f"unsupported_schema:{payload.get('schema')!r}")
    if payload.get("version") != 1:
        raise EnvelopeError(f"unsupported_version:{payload.get('version')!r}")
    envelope_id = str(payload.get("envelope_id") or "").strip()
    if not envelope_id:
        raise EnvelopeError("envelope_id_required")

    phase_one = payload.get("phase_one")
    if not isinstance(phase_one, dict):
        raise EnvelopeError("phase_one_must_be_object")
    operating_mode = str(phase_one.get("operating_mode") or "")
    if operating_mode not in {"invite_only_private_beta", "launch"}:
        raise EnvelopeError("phase_one_operating_mode_must_be_private_beta_or_launch")
    if str(phase_one.get("content_language") or "") != "en":
        raise EnvelopeError("phase_one_content_language_must_match_honest_english_envelope")
    _nonempty_strings(phase_one.get("supported_workflows"), field="phase_one_supported_workflows")
    excluded_claims = _nonempty_strings(phase_one.get("excluded_claims"), field="phase_one_excluded_claims")
    if operating_mode == "launch" and "fully_localized_global_product" in excluded_claims:
        raise EnvelopeError("launch_operating_mode_conflicts_with_localization_exclusion")

    evidence_catalog = payload.get("evidence_catalog")
    if not isinstance(evidence_catalog, dict) or not evidence_catalog:
        raise EnvelopeError("evidence_catalog_must_be_nonempty_object")
    for evidence_id, evidence in evidence_catalog.items():
        if not str(evidence_id or "").strip() or not isinstance(evidence, dict):
            raise EnvelopeError("evidence_catalog_entry_invalid")
        if not str(evidence.get("kind") or "").strip():
            raise EnvelopeError(f"evidence_kind_required:{evidence_id}")
        if not str(evidence.get("ref") or "").strip():
            raise EnvelopeError(f"evidence_ref_required:{evidence_id}")

    markets = payload.get("markets")
    if not isinstance(markets, list):
        raise EnvelopeError("markets_must_be_list")
    country_codes = [str(row.get("country_code") or "") for row in markets if isinstance(row, dict)]
    if tuple(country_codes) != REQUIRED_MARKETS:
        raise EnvelopeError(
            "markets_must_be_exact_ordered_AT_DE_CR:"
            + ",".join(country_codes)
        )

    for market in markets:
        if not isinstance(market, dict):
            raise EnvelopeError("market_entry_must_be_object")
        country_code = str(market.get("country_code") or "")
        classification = str(market.get("declared_classification") or "")
        if classification not in ALLOWED_CLASSIFICATIONS:
            raise EnvelopeError(f"unsupported_classification:{country_code}:{classification}")
        expected_semantics = EXPECTED_MARKET_SEMANTICS[country_code]
        for field, expected_value in expected_semantics.items():
            observed_value = str(market.get(field) or "").strip()
            if observed_value != expected_value:
                raise EnvelopeError(
                    f"market_semantics_mismatch:{country_code}:{field}:"
                    f"expected={expected_value}:observed={observed_value}"
                )

        claims = market.get("workflow_claims")
        if not isinstance(claims, dict) or set(claims) != set(WORKFLOW_DIMENSIONS):
            raise EnvelopeError(f"workflow_claims_invalid:{country_code}")
        if any(type(value) is not bool for value in claims.values()):
            raise EnvelopeError(f"workflow_claims_must_be_boolean:{country_code}")

        _validate_market_contract(
            market,
            phase_content_language=str(phase_one["content_language"]),
            operating_mode=operating_mode,
        )

        dimensions = market.get("dimensions")
        if not isinstance(dimensions, dict) or tuple(dimensions) != ALL_DIMENSIONS:
            raise EnvelopeError(f"dimensions_must_be_exact_ordered_set:{country_code}")
        required_dimensions = set(_market_required_dimensions(market))
        for dimension_name, dimension in dimensions.items():
            if not isinstance(dimension, dict):
                raise EnvelopeError(f"dimension_must_be_object:{country_code}:{dimension_name}")
            status = str(dimension.get("status") or "")
            if status not in ALLOWED_DIMENSION_STATUSES:
                raise EnvelopeError(f"dimension_status_invalid:{country_code}:{dimension_name}:{status}")
            evidence = _nonempty_strings(
                dimension.get("evidence", []),
                field=f"dimension_evidence:{country_code}:{dimension_name}",
            )
            missing_evidence = _nonempty_strings(
                dimension.get("missing_evidence", []),
                field=f"dimension_missing_evidence:{country_code}:{dimension_name}",
            )
            unknown_evidence = [item for item in evidence if item not in evidence_catalog]
            if unknown_evidence:
                raise EnvelopeError(
                    f"dimension_unknown_evidence:{country_code}:{dimension_name}:"
                    + ",".join(unknown_evidence)
                )
            if status == "proven" and (not evidence or missing_evidence):
                raise EnvelopeError(f"proven_dimension_requires_evidence_only:{country_code}:{dimension_name}")
            if status == "not_applicable" and not str(dimension.get("reason") or "").strip():
                raise EnvelopeError(f"not_applicable_reason_required:{country_code}:{dimension_name}")
            if dimension_name in required_dimensions and status != "proven" and not missing_evidence:
                raise EnvelopeError(
                    f"unproven_required_dimension_needs_missing_evidence:{country_code}:{dimension_name}"
                )

        for workflow, dimension_name in WORKFLOW_DIMENSIONS.items():
            status = str(dimensions[dimension_name]["status"])
            if claims[workflow] is False and status != "not_applicable":
                raise EnvelopeError(
                    f"excluded_workflow_dimension_must_be_not_applicable:{country_code}:{workflow}"
                )
            if claims[workflow] is True and status == "not_applicable":
                raise EnvelopeError(
                    f"claimed_workflow_dimension_cannot_be_not_applicable:{country_code}:{workflow}"
                )


def compute_market_classification(market: dict[str, Any]) -> str:
    dimensions = market["dimensions"]
    required_dimensions = _market_required_dimensions(market)
    if all(dimensions[name]["status"] == "proven" for name in required_dimensions):
        return "launch_supported"
    if (
        dimensions["market_catalog"]["status"] == "proven"
        and dimensions["market_browser_journey"]["status"] == "proven"
        and dimensions["buyer_journey"]["status"] == "proven"
    ):
        return "private_beta"
    browser_state = dimensions["market_browser_journey"]
    if (
        dimensions["market_catalog"]["status"] == "proven"
        and browser_state["status"] == "implemented_unproven"
        and browser_state["evidence"]
    ):
        return "browser_state_only"
    if dimensions["responsive_devices"]["evidence"]:
        return "preview"
    return "catalog"


def materialize_envelope(
    payload: dict[str, Any],
    *,
    expected_release_sha: str = "",
    expected_image_digest: str = "",
    live_launch_evidence: Mapping[str, Any] | None = None,
    live_receipt_ref: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    validate_envelope(payload)
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    release_sha = str(expected_release_sha or "").strip().lower()
    image_digest = str(expected_image_digest or "").strip().lower()
    source_sha256 = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    market_results: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []

    for market in payload["markets"]:
        country_code = market["country_code"]
        declared = market["declared_classification"]
        computed = compute_market_classification(market)
        required_dimensions = _market_required_dimensions(market)
        missing_dimensions: list[dict[str, Any]] = []
        for dimension_name in required_dimensions:
            dimension = market["dimensions"][dimension_name]
            if dimension["status"] == "proven":
                continue
            gap = {
                "dimension": dimension_name,
                "status": dimension["status"],
                "missing_evidence": list(dimension["missing_evidence"]),
                "reason": str(dimension.get("reason") or ""),
            }
            missing_dimensions.append(gap)
            blockers.append(
                {
                    "code": f"{country_code}:{dimension_name}:{dimension['status']}",
                    "country_code": country_code,
                    **gap,
                }
            )
        classification_match = declared == computed
        if not classification_match:
            blockers.append(
                {
                    "code": f"{country_code}:classification_mismatch",
                    "country_code": country_code,
                    "declared_classification": declared,
                    "computed_classification": computed,
                }
            )
        market_results.append(
            {
                "country_code": country_code,
                "market_language": market["market_language"],
                "currency_code": market["currency_code"],
                "default_timezone": market["default_timezone"],
                "market_contract": json.loads(
                    json.dumps(market["market_contract"], ensure_ascii=True)
                ),
                "declared_classification": declared,
                "computed_classification": computed,
                "classification_match": classification_match,
                "launch_supported": computed == "launch_supported" and classification_match,
                "status": "READY" if computed == "launch_supported" and classification_match else "BLOCKED",
                "workflow_claims": dict(market["workflow_claims"]),
                "missing_dimensions": missing_dimensions,
            }
        )

    launch_supported = [row["country_code"] for row in market_results if row["launch_supported"]]
    if not launch_supported:
        blockers.insert(
            0,
            {
                "code": "global:no_launch_supported_market",
                "reason": "AT, DE, and CR lack complete live launch evidence; the honest envelope is private beta/preview only.",
            },
        )
    if str(payload["phase_one"].get("operating_mode") or "") != "launch":
        blockers.append(
            {
                "code": "global:operating_mode_not_launch",
                "reason": "The governed market envelope remains private beta and cannot authorize launch.",
            }
        )
    if not _exact_git_sha(release_sha):
        blockers.append(
            {
                "code": "global:release_sha_missing_or_placeholder",
                "reason": "An exact non-placeholder 40-character release SHA is required.",
            }
        )
    if not _exact_image_digest(image_digest):
        blockers.append(
            {
                "code": "global:image_digest_missing_or_placeholder",
                "reason": "An exact non-placeholder immutable image digest is required.",
            }
        )
    live_errors, independently_attested, live_receipt_age_seconds = _validate_live_launch_evidence(
        live_launch_evidence,
        source=payload,
        source_sha256=source_sha256,
        expected_release_sha=release_sha,
        expected_image_digest=image_digest,
        now=observed_at,
    )
    blockers.extend(
        {
            "code": "global:live_launch_evidence_invalid",
            "reason": error,
        }
        for error in live_errors
    )
    status = "READY" if launch_supported and not blockers else "BLOCKED"
    classifications = {
        classification: [
            row["country_code"]
            for row in market_results
            if row["computed_classification"] == classification
        ]
        for classification in ALLOWED_CLASSIFICATIONS
    }
    return {
        "schema": RECEIPT_SCHEMA,
        "generated_at": observed_at.isoformat().replace("+00:00", "Z"),
        "release_identity": {
            "commit_sha": release_sha,
            "image_digest": image_digest,
        },
        "live_receipt_ref": str(live_receipt_ref or ""),
        "live_receipt_age_seconds": live_receipt_age_seconds,
        "independently_attested": independently_attested,
        "source_schema": ENVELOPE_SCHEMA,
        "source_envelope_id": payload["envelope_id"],
        "source_sha256": source_sha256,
        "audit_basis": dict(payload.get("audit_basis") or {}),
        "status": status,
        "phase_one": dict(payload["phase_one"]),
        "summary": {
            "launch_supported_markets": launch_supported,
            "classifications": classifications,
            "market_count": len(market_results),
            "blocker_count": len(blockers),
        },
        "markets": market_results,
        "blockers": blockers,
    }


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Materialize and verify the PropertyQuarry global market envelope.")
    parser.add_argument("--input", type=Path, default=DEFAULT_ENVELOPE_PATH)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expected-release-sha", default="")
    parser.add_argument("--expected-image-digest", default="")
    parser.add_argument("--live-receipt", type=Path)
    return parser.parse_args(argv)


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        source = load_envelope(args.input)
        live_evidence = load_envelope(args.live_receipt) if args.live_receipt is not None else None
        receipt = materialize_envelope(
            source,
            expected_release_sha=args.expected_release_sha,
            expected_image_digest=args.expected_image_digest,
            live_launch_evidence=live_evidence,
            live_receipt_ref=str(args.live_receipt) if args.live_receipt is not None else "",
        )
    except EnvelopeError as exc:
        invalid = {
            "schema": RECEIPT_SCHEMA,
            "status": "INVALID",
            "error": str(exc),
        }
        rendered = json.dumps(invalid, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            _write_json_atomic(args.output, invalid)
        else:
            sys.stdout.write(rendered)
        return 2
    rendered = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        _write_json_atomic(args.output, receipt)
    else:
        sys.stdout.write(rendered)
    return 0 if receipt["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
