#!/usr/bin/env python3
"""Fail-closed PropertyQuarry global-experience evidence gate.

The checked-in contract describes required proof; it is never proof by itself.
Only a fresh, independently attested live receipt bound to the exact release may
make this gate pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

if __package__:
    from scripts.propertyquarry_global_governance_attestation import (
        GLOBAL_EXPERIENCE_GATE_ID,
        GlobalGovernanceAttestationError,
        verify_global_governance_attestation,
    )
    from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot
else:
    from propertyquarry_global_governance_attestation import (  # type: ignore[no-redef]
        GLOBAL_EXPERIENCE_GATE_ID,
        GlobalGovernanceAttestationError,
        verify_global_governance_attestation,
    )
    from propertyquarry_strict_json import load_strict_json_object_snapshot


CONTRACT_SCHEMA = "propertyquarry.global_experience.v1"
LIVE_RECEIPT_SCHEMA = "propertyquarry.global_experience_live_receipt.v1"
GATE_RECEIPT_SCHEMA = "propertyquarry.global_experience_gate.v1"

DEFAULT_CONTRACT_PATH = Path("config/monitoring/propertyquarry_global_experience.v1.json")

EXPECTED_MARKETS = {"AT": "de-AT", "DE": "de-DE", "CR": "es-CR"}
EXPECTED_MARKET_METADATA = {
    "AT": {"currency": "EUR", "timezone": "Europe/Vienna"},
    "DE": {"currency": "EUR", "timezone": "Europe/Berlin"},
    "CR": {"currency": "CRC", "timezone": "America/Costa_Rica"},
}
REQUIRED_CUSTOMER_ROUTES = {
    "/",
    "/pricing",
    "/security",
    "/support",
    "/privacy",
    "/terms",
    "/cookies",
    "/subprocessors",
    "/refunds",
    "/disclaimers",
    "/imprint",
    "/integrations",
    "/docs",
    "/guides/wohnung-kaufen-wien-checkliste",
    "/markets/vienna",
    "/sign-in",
    "/register",
    "/app/search",
    "/app/properties",
    "/app/shortlist",
    "/app/agents",
    "/app/alerts",
    "/app/research",
    "/app/account",
    "/app/billing",
    "/app/support",
    "/app/settings/google",
    "/app/settings/access",
    "/app/settings/usage",
    "/app/settings/support",
    "/app/settings/trust",
    "/app/settings/invitations",
    "/app/settings/outcomes",
    "/app/settings/plan",
    "/app/properties/packets",
    "/app/properties/notifications/preview",
    "/app/research/{candidate_ref}",
    "/app/shortlist/run/{run_id}",
    "/tours/{slug}",
}
_DYNAMIC_CUSTOMER_ROUTE_PATTERNS = {
    "/app/research/{candidate_ref}": re.compile(r"^/app/research/[^/{}]+$"),
    "/app/shortlist/run/{run_id}": re.compile(r"^/app/shortlist/run/[^/{}]+$"),
    "/tours/{slug}": re.compile(r"^/tours/[^/{}]+$"),
}
REQUIRED_CRITICAL_SCENARIOS = {
    "authentication_success",
    "authentication_failure",
    "expired_session_recovery",
    "billing_handoff_ready",
    "billing_handoff_unavailable",
    "http_401",
    "http_403",
    "http_404",
    "http_422",
    "http_429",
    "http_500",
    "http_503",
    "tour_ready",
    "tour_unavailable",
    "tour_revoked",
}
REQUIRED_ENGINES = {"chromium", "firefox", "webkit"}
REQUIRED_WCAG_TAGS = {"wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"}
REQUIRED_MANUAL_TASKS = {
    "keyboard_navigation",
    "screen_reader_desktop",
    "screen_reader_mobile",
    "zoom_200_percent",
    "zoom_400_percent",
    "reduced_motion",
}
REQUIRED_SCREEN_READERS = {
    "nvda_windows",
    "voiceover_macos",
    "voiceover_ios",
    "talkback_android",
}
REQUIRED_NETWORK_SCENARIOS = {
    "slow_3g",
    "offline_reconnect",
    "packet_loss_retry",
    "request_timeout_recovery",
}
REQUIRED_HREFLANG = {"de-AT", "de-DE", "es-CR", "x-default"}
REQUIRED_APPROVALS = {
    "global_experience_owner",
    "accessibility_owner",
    "localization_owner",
    "performance_owner",
    "seo_owner",
}
REQUIRED_MOBILE_PROFILES = {
    "ios_safari_390x844": {
        "engine": "webkit",
        "browser_family": "safari",
        "operating_system": "ios",
        "execution_environment": "physical_device",
        "viewport_width": 390,
        "viewport_height": 844,
    },
    "android_chrome_412x915": {
        "engine": "chromium",
        "browser_family": "chrome",
        "operating_system": "android",
        "execution_environment": "physical_device",
        "viewport_width": 412,
        "viewport_height": 915,
    },
}
REQUIRED_CWV_COHORTS = {"desktop", "mobile"}
REQUIRED_SEO_CHECKS = {
    "html_lang",
    "content_language",
    "self_canonical",
    "reciprocal_hreflang",
    "localized_title_and_description",
    "sitemap_membership",
    "robots_indexable",
}
REQUIRED_NATIVE_CHECKS = {
    "native_ui_copy",
    "native_public_content",
    "forms_and_validation",
    "currency_number_date_time",
    "address_and_region_conventions",
    "text_expansion_and_layout",
}
CWV_CEILINGS = {
    "LCP": (2500.0, "ms"),
    "INP": (200.0, "ms"),
    "CLS": (0.1, "score"),
}

_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLACEHOLDER_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:placeholder|example|unconfigured|unassigned|unknown|todo|tbd|fake|mock|demo|sample|test|invalid|changeme|dummy|pending|localhost)(?:[^a-z0-9]|$)",
    re.IGNORECASE,
)


def _load_json(path: Path) -> Mapping[str, Any]:
    payload, _raw, _digest = load_strict_json_object_snapshot(
        path,
        field="global experience artifact",
    )
    return payload


def _load_json_with_sha256(path: Path) -> tuple[Mapping[str, Any], str]:
    payload, _raw, digest = load_strict_json_object_snapshot(
        path,
        field="global experience artifact",
    )
    return payload, digest


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _string_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _concrete_route_path(value: object) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        return "", "nonempty relative route required"
    route = value.strip()
    try:
        parsed = urllib.parse.urlsplit(route)
    except ValueError:
        return "", "valid relative route required"
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "", "same-origin relative route required"
    decoded_path = parsed.path
    for _index in range(2):
        decoded_path = urllib.parse.unquote(decoded_path)
    normalized_path = decoded_path.rstrip("/") or "/"
    if (
        "{" in route
        or "}" in route
        or "{" in decoded_path
        or "}" in decoded_path
        or "\\" in decoded_path
        or any(character.isspace() or ord(character) < 32 for character in decoded_path)
        or (
            normalized_path != "/"
            and any(segment in {"", ".", ".."} for segment in normalized_path.split("/")[1:])
        )
    ):
        return "", "concrete non-template route required"
    return normalized_path, ""


def _concrete_route_coverage(
    *,
    required_routes: set[str],
    observed_routes: object,
) -> tuple[list[str], list[dict[str, str]]]:
    if not isinstance(observed_routes, list):
        return sorted(required_routes), [{"route": "", "error": "route list required"}]
    normalized_paths: list[str] = []
    invalid_routes: list[dict[str, str]] = []
    for observed in observed_routes:
        path, error = _concrete_route_path(observed)
        if error:
            invalid_routes.append({"route": str(observed or "")[:300], "error": error})
            continue
        normalized_paths.append(path)
    missing_routes: list[str] = []
    for required in sorted(required_routes):
        pattern = _DYNAMIC_CUSTOMER_ROUTE_PATTERNS.get(required)
        if pattern is not None:
            if not any(pattern.fullmatch(path) for path in normalized_paths):
                missing_routes.append(required)
        elif (required.rstrip("/") or "/") not in normalized_paths:
            missing_routes.append(required)
    return missing_routes, invalid_routes


def _validate_concrete_route_coverage(
    errors: list[str],
    *,
    path: str,
    required_routes: set[str],
    observed_routes: object,
) -> None:
    missing, invalid = _concrete_route_coverage(
        required_routes=required_routes,
        observed_routes=observed_routes,
    )
    if invalid:
        _add(errors, path, f"invalid or non-concrete routes: {invalid}")
    if missing:
        _add(errors, path, f"required route families are missing: {missing}")


def _opaque_ref(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value.strip()) >= 12
        and _PLACEHOLDER_RE.search(value) is None
    )


def _exact_git_sha(value: Any) -> bool:
    normalized = str(value or "")
    return bool(_GIT_SHA_RE.fullmatch(normalized) and len(set(normalized)) >= 4)


def _exact_sha256(value: Any) -> bool:
    normalized = str(value or "")
    payload = normalized.removeprefix("sha256:")
    return bool(_SHA256_RE.fullmatch(normalized) and len(set(payload)) >= 4)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _attested_payload_digest(receipt: Mapping[str, Any]) -> str:
    """Digest the complete asserted payload without its detached attestation."""

    payload = dict(receipt)
    payload.pop("independent_attestation", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _rows_by(rows: Any, key: str) -> tuple[dict[str, Mapping[str, Any]], bool]:
    if not isinstance(rows, list):
        return {}, False
    indexed: dict[str, Mapping[str, Any]] = {}
    valid = True
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get(key), str):
            valid = False
            continue
        row_key = str(row[key])
        if row_key in indexed:
            valid = False
        indexed[row_key] = row
    return indexed, valid


def _add(errors: list[str], path: str, message: str) -> None:
    errors.append(f"{path}: {message}")


def _evidence_errors(
    value: Any,
    *,
    path: str,
    now: datetime,
    maximum_age_hours: float,
) -> list[str]:
    errors: list[str] = []
    evidence = _mapping(value)
    if not evidence:
        return [f"{path}: evidence object is required"]
    if evidence.get("status") != "pass":
        _add(errors, path, "status must be pass")
    observed_at = _parse_time(evidence.get("observed_at"))
    if observed_at is None:
        _add(errors, path, "observed_at must be a timezone-aware timestamp")
    else:
        age_seconds = (now - observed_at).total_seconds()
        if age_seconds < -300:
            _add(errors, path, "observed_at is more than five minutes in the future")
        elif age_seconds > maximum_age_hours * 3600:
            _add(errors, path, f"evidence is stale (maximum {maximum_age_hours:g} hours)")
    if not _exact_sha256(evidence.get("evidence_digest")):
        _add(errors, path, "evidence_digest must be a sha256 image-style digest")
    if not _opaque_ref(evidence.get("workflow_ref")):
        _add(errors, path, "workflow_ref must be an opaque, non-placeholder reference")
    return errors


def validate_contract(contract: Mapping[str, Any]) -> list[str]:
    """Reject a weakened or placeholder source contract."""

    errors: list[str] = []
    if contract.get("schema") != CONTRACT_SCHEMA:
        _add(errors, "contract.schema", f"must be {CONTRACT_SCHEMA}")
    if contract.get("service") != "propertyquarry":
        _add(errors, "contract.service", "must be propertyquarry")
    if contract.get("source_contract_status") != "defined_not_live_evidence":
        _add(errors, "contract.source_contract_status", "must remain defined_not_live_evidence")

    markets, valid = _rows_by(contract.get("required_markets"), "country_code")
    if not valid or set(markets) != set(EXPECTED_MARKETS):
        _add(errors, "contract.required_markets", "must contain exactly AT, DE, and CR once each")
    for country_code, locale in EXPECTED_MARKETS.items():
        if markets.get(country_code, {}).get("locale") != locale:
            _add(errors, f"contract.required_markets.{country_code}.locale", f"must be {locale}")
        for field, expected in EXPECTED_MARKET_METADATA[country_code].items():
            if markets.get(country_code, {}).get(field) != expected:
                _add(
                    errors,
                    f"contract.required_markets.{country_code}.{field}",
                    f"must be {expected}",
                )

    routes = _string_set(contract.get("required_customer_routes"))
    if routes != REQUIRED_CUSTOMER_ROUTES:
        _add(errors, "contract.required_customer_routes", "required customer route set changed")
    states = _mapping(contract.get("critical_state_scenarios"))
    if _string_set(states.get("required_scenarios")) != REQUIRED_CRITICAL_SCENARIOS:
        _add(errors, "contract.critical_state_scenarios.required_scenarios", "critical state scenario set changed")
    if states.get("require_useful_next_action") is not True:
        _add(errors, "contract.critical_state_scenarios.require_useful_next_action", "must be true")
    if states.get("require_customer_data_preserved") is not True:
        _add(errors, "contract.critical_state_scenarios.require_customer_data_preserved", "must be true")

    native = _mapping(contract.get("native_content_review"))
    if _string_set(native.get("required_checks")) != REQUIRED_NATIVE_CHECKS:
        _add(errors, "contract.native_content_review.required_checks", "required native checks changed")
    if _number(native.get("minimum_reviewed_route_count")) is None or float(
        native.get("minimum_reviewed_route_count", 0)
    ) < len(REQUIRED_CUSTOMER_ROUTES):
        _add(
            errors,
            "contract.native_content_review.minimum_reviewed_route_count",
            f"must be at least {len(REQUIRED_CUSTOMER_ROUTES)}",
        )
    if native.get("required_proficiency") != "native":
        _add(errors, "contract.native_content_review.required_proficiency", "must be native")
    if native.get("require_market_specific_qualification") is not True:
        _add(
            errors,
            "contract.native_content_review.require_market_specific_qualification",
            "must be true",
        )

    accessibility = _mapping(contract.get("accessibility"))
    if accessibility.get("standard") != "WCAG 2.2 AA":
        _add(errors, "contract.accessibility.standard", "must be WCAG 2.2 AA")
    automated = _mapping(accessibility.get("automated"))
    if _string_set(automated.get("required_engines")) != REQUIRED_ENGINES:
        _add(errors, "contract.accessibility.automated.required_engines", "must be chromium, firefox, and webkit")
    if _string_set(automated.get("required_tags")) != REQUIRED_WCAG_TAGS:
        _add(errors, "contract.accessibility.automated.required_tags", "required WCAG tag set changed")
    if _number(automated.get("minimum_route_count")) is None or float(
        automated.get("minimum_route_count", 0)
    ) < len(REQUIRED_CUSTOMER_ROUTES):
        _add(errors, "contract.accessibility.automated.minimum_route_count", "must cover every required customer route")
    if automated.get("maximum_serious_or_critical_violations") != 0:
        _add(errors, "contract.accessibility.automated.maximum_serious_or_critical_violations", "must be zero")
    manual = _mapping(accessibility.get("manual"))
    if _string_set(manual.get("required_tasks")) != REQUIRED_MANUAL_TASKS:
        _add(errors, "contract.accessibility.manual.required_tasks", "manual task set changed")
    if _string_set(manual.get("required_screen_reader_platforms")) != REQUIRED_SCREEN_READERS:
        _add(errors, "contract.accessibility.manual.required_screen_reader_platforms", "screen-reader set changed")

    coverage = _mapping(contract.get("browser_device_coverage"))
    if _string_set(coverage.get("required_desktop_engines")) != REQUIRED_ENGINES:
        _add(errors, "contract.browser_device_coverage.required_desktop_engines", "must cover three engines")
    profiles, profiles_valid = _rows_by(coverage.get("required_mobile_profiles"), "profile_id")
    if not profiles_valid or set(profiles) != set(REQUIRED_MOBILE_PROFILES):
        _add(errors, "contract.browser_device_coverage.required_mobile_profiles", "required profiles changed")
    for profile_id, expected in REQUIRED_MOBILE_PROFILES.items():
        row = profiles.get(profile_id, {})
        for field, expected_value in expected.items():
            if row.get(field) != expected_value:
                _add(
                    errors,
                    f"contract.browser_device_coverage.required_mobile_profiles.{profile_id}.{field}",
                    f"must be {expected_value}",
                )
    if coverage.get("require_desktop_browser_version") is not True:
        _add(errors, "contract.browser_device_coverage.require_desktop_browser_version", "must be true")
    if coverage.get("require_desktop_binary_digest") is not True:
        _add(errors, "contract.browser_device_coverage.require_desktop_binary_digest", "must be true")
    if coverage.get("require_physical_mobile_devices") is not True:
        _add(errors, "contract.browser_device_coverage.require_physical_mobile_devices", "must be true")

    cwv = _mapping(contract.get("field_core_web_vitals"))
    if cwv.get("measurement_scope") != "field_rum" or cwv.get("percentile") != 75:
        _add(errors, "contract.field_core_web_vitals", "must require field_rum at p75")
    if _number(cwv.get("minimum_window_days")) is None or float(cwv.get("minimum_window_days", 0)) < 28:
        _add(errors, "contract.field_core_web_vitals.minimum_window_days", "must be at least 28")
    if _string_set(cwv.get("required_device_cohorts")) != REQUIRED_CWV_COHORTS:
        _add(errors, "contract.field_core_web_vitals.required_device_cohorts", "must be desktop and mobile")
    if _number(cwv.get("minimum_samples_per_market_device_cohort")) is None or float(
        cwv.get("minimum_samples_per_market_device_cohort", 0)
    ) < 200:
        _add(
            errors,
            "contract.field_core_web_vitals.minimum_samples_per_market_device_cohort",
            "must be at least 200",
        )
    thresholds = _mapping(cwv.get("thresholds"))
    for metric, (ceiling, unit) in CWV_CEILINGS.items():
        threshold = _mapping(thresholds.get(metric))
        maximum = _number(threshold.get("maximum"))
        if maximum is None or maximum > ceiling or threshold.get("unit") != unit:
            _add(errors, f"contract.field_core_web_vitals.thresholds.{metric}", f"must be no weaker than {ceiling:g} {unit}")

    network = _mapping(contract.get("degraded_network_recovery"))
    if _string_set(network.get("required_scenarios")) != REQUIRED_NETWORK_SCENARIOS:
        _add(errors, "contract.degraded_network_recovery.required_scenarios", "required scenarios changed")
    if network.get("require_no_data_loss") is not True or network.get("require_no_duplicate_mutation") is not True:
        _add(errors, "contract.degraded_network_recovery", "must forbid data loss and duplicate mutations")

    seo = _mapping(contract.get("localized_seo"))
    if _string_set(seo.get("required_hreflang_values")) != REQUIRED_HREFLANG:
        _add(errors, "contract.localized_seo.required_hreflang_values", "hreflang set changed")
    if _string_set(seo.get("required_checks")) != REQUIRED_SEO_CHECKS:
        _add(errors, "contract.localized_seo.required_checks", "localized SEO checks changed")
    if _number(seo.get("minimum_indexable_route_count")) is None or float(
        seo.get("minimum_indexable_route_count", 0)
    ) < 3:
        _add(errors, "contract.localized_seo.minimum_indexable_route_count", "must be at least 3")

    policy = _mapping(contract.get("live_evidence_policy"))
    policy_age = _number(policy.get("maximum_receipt_age_hours"))
    if policy_age is None or policy_age <= 0 or policy_age > 24:
        _add(errors, "contract.live_evidence_policy.maximum_receipt_age_hours", "must be between 0 and 24")
    if policy.get("independent_attestation_authority") != "independent_release_controller":
        _add(errors, "contract.live_evidence_policy.independent_attestation_authority", "authority changed")
    if any(
        policy.get(key) is not True
        for key in (
            "require_exact_git_sha",
            "require_sha256_image_digest",
            "require_attested_payload_digest",
            "reject_placeholders",
        )
    ):
        _add(errors, "contract.live_evidence_policy", "release binding and placeholder rejection must remain enabled")
    if _string_set(contract.get("required_approvals")) != REQUIRED_APPROVALS:
        _add(errors, "contract.required_approvals", "approval set changed")
    return errors


def _validate_native_review(
    market: Mapping[str, Any],
    contract: Mapping[str, Any],
    *,
    locale: str,
    path: str,
    now: datetime,
    age: float,
) -> list[str]:
    errors: list[str] = []
    review = _mapping(market.get("native_content_review"))
    errors.extend(_evidence_errors(review.get("evidence"), path=f"{path}.evidence", now=now, maximum_age_hours=age))
    if review.get("reviewer_independent") is not True or not _opaque_ref(review.get("native_reviewer_ref")):
        _add(errors, path, "an independent native reviewer with an opaque, non-placeholder reference is required")
    if review.get("reviewer_locale") != locale or review.get("reviewer_proficiency") != "native":
        _add(errors, path, f"reviewer must attest native proficiency for {locale}")
    if not _opaque_ref(review.get("reviewer_qualification_ref")):
        _add(errors, path, "reviewer_qualification_ref must be an opaque, non-placeholder reference")
    required_routes = _string_set(contract.get("required_customer_routes"))
    _validate_concrete_route_coverage(
        errors,
        path=f"{path}.reviewed_routes",
        required_routes=required_routes,
        observed_routes=review.get("reviewed_routes"),
    )
    if not REQUIRED_CRITICAL_SCENARIOS.issubset(_string_set(review.get("reviewed_scenarios"))):
        _add(errors, f"{path}.reviewed_scenarios", "all critical state scenarios must be reviewed")
    checks = _mapping(review.get("checks"))
    for check in sorted(REQUIRED_NATIVE_CHECKS):
        if checks.get(check) is not True:
            _add(errors, f"{path}.checks.{check}", "must be true")
    return errors


def _validate_accessibility(
    market: Mapping[str, Any], contract: Mapping[str, Any], *, path: str, now: datetime, age: float
) -> list[str]:
    errors: list[str] = []
    accessibility = _mapping(market.get("accessibility"))
    if accessibility.get("standard") != "WCAG 2.2 AA":
        _add(errors, f"{path}.standard", "must be WCAG 2.2 AA")

    automated = _mapping(accessibility.get("automated"))
    runs, valid = _rows_by(automated.get("runs"), "engine")
    if not valid or set(runs) != REQUIRED_ENGINES:
        _add(errors, f"{path}.automated.runs", "must contain exactly one run for each required engine")
    automated_contract = _mapping(_mapping(contract.get("accessibility")).get("automated"))
    minimum_routes = int(automated_contract.get("minimum_route_count", 7))
    required_tags = _string_set(automated_contract.get("required_tags"))
    required_routes = _string_set(contract.get("required_customer_routes"))
    for engine in sorted(REQUIRED_ENGINES):
        run = runs.get(engine, {})
        if run.get("outcome") != "pass":
            _add(errors, f"{path}.automated.runs.{engine}.outcome", "must be pass")
        if _number(run.get("route_count")) is None or float(run.get("route_count", 0)) < minimum_routes:
            _add(errors, f"{path}.automated.runs.{engine}.route_count", f"must be at least {minimum_routes}")
        _validate_concrete_route_coverage(
            errors,
            path=f"{path}.automated.runs.{engine}.tested_routes",
            required_routes=required_routes,
            observed_routes=run.get("tested_routes"),
        )
        if not REQUIRED_CRITICAL_SCENARIOS.issubset(_string_set(run.get("tested_scenarios"))):
            _add(errors, f"{path}.automated.runs.{engine}.tested_scenarios", "all critical state scenarios must be tested")
        if not required_tags.issubset(_string_set(run.get("wcag_tags"))):
            _add(errors, f"{path}.automated.runs.{engine}.wcag_tags", "required WCAG tags are missing")
        if run.get("serious_or_critical_violations") != 0:
            _add(errors, f"{path}.automated.runs.{engine}", "serious_or_critical_violations must be zero")
        errors.extend(_evidence_errors(run.get("evidence"), path=f"{path}.automated.runs.{engine}.evidence", now=now, maximum_age_hours=age))

    manual = _mapping(accessibility.get("manual"))
    if not REQUIRED_CRITICAL_SCENARIOS.issubset(_string_set(manual.get("tested_scenarios"))):
        _add(errors, f"{path}.manual.tested_scenarios", "all critical state scenarios must be covered manually")
    tasks, tasks_valid = _rows_by(manual.get("tasks"), "task_id")
    if not tasks_valid or set(tasks) != REQUIRED_MANUAL_TASKS:
        _add(errors, f"{path}.manual.tasks", "manual accessibility task set is incomplete")
    for task_id in sorted(REQUIRED_MANUAL_TASKS):
        task = tasks.get(task_id, {})
        if task.get("outcome") != "pass":
            _add(errors, f"{path}.manual.tasks.{task_id}.outcome", "must be pass")
        errors.extend(_evidence_errors(task.get("evidence"), path=f"{path}.manual.tasks.{task_id}.evidence", now=now, maximum_age_hours=age))

    readers, readers_valid = _rows_by(manual.get("screen_reader_platforms"), "platform_id")
    if not readers_valid or set(readers) != REQUIRED_SCREEN_READERS:
        _add(errors, f"{path}.manual.screen_reader_platforms", "required screen-reader platforms are incomplete")
    for platform_id in sorted(REQUIRED_SCREEN_READERS):
        row = readers.get(platform_id, {})
        if row.get("outcome") != "pass":
            _add(errors, f"{path}.manual.screen_reader_platforms.{platform_id}.outcome", "must be pass")
        errors.extend(_evidence_errors(row.get("evidence"), path=f"{path}.manual.screen_reader_platforms.{platform_id}.evidence", now=now, maximum_age_hours=age))
    return errors


def _validate_browser_device(
    market: Mapping[str, Any], contract: Mapping[str, Any], *, path: str, now: datetime, age: float
) -> list[str]:
    errors: list[str] = []
    coverage = _mapping(market.get("browser_device_coverage"))
    required_routes = _string_set(contract.get("required_customer_routes"))
    desktops, valid = _rows_by(coverage.get("desktop_runs"), "engine")
    if not valid or set(desktops) != REQUIRED_ENGINES:
        _add(errors, f"{path}.desktop_runs", "must contain chromium, firefox, and webkit")
    for engine in sorted(REQUIRED_ENGINES):
        row = desktops.get(engine, {})
        if row.get("outcome") != "pass":
            _add(errors, f"{path}.desktop_runs.{engine}.outcome", "must be pass")
        _validate_concrete_route_coverage(
            errors,
            path=f"{path}.desktop_runs.{engine}.tested_routes",
            required_routes=required_routes,
            observed_routes=row.get("tested_routes"),
        )
        if not REQUIRED_CRITICAL_SCENARIOS.issubset(_string_set(row.get("tested_scenarios"))):
            _add(errors, f"{path}.desktop_runs.{engine}.tested_scenarios", "all critical state scenarios must be tested")
        if not _opaque_ref(row.get("browser_version")):
            _add(errors, f"{path}.desktop_runs.{engine}.browser_version", "must identify the tested browser build")
        if not _exact_sha256(row.get("browser_binary_digest")):
            _add(errors, f"{path}.desktop_runs.{engine}.browser_binary_digest", "must be an exact sha256 digest")
        if not _opaque_ref(row.get("execution_environment")):
            _add(errors, f"{path}.desktop_runs.{engine}.execution_environment", "must identify the test environment")
        errors.extend(_evidence_errors(row.get("evidence"), path=f"{path}.desktop_runs.{engine}.evidence", now=now, maximum_age_hours=age))

    mobiles, mobile_valid = _rows_by(coverage.get("mobile_runs"), "profile_id")
    if not mobile_valid or set(mobiles) != set(REQUIRED_MOBILE_PROFILES):
        _add(errors, f"{path}.mobile_runs", "required iOS Safari and Android Chrome profiles are incomplete")
    for profile_id, expected in REQUIRED_MOBILE_PROFILES.items():
        row = mobiles.get(profile_id, {})
        if row.get("outcome") != "pass":
            _add(errors, f"{path}.mobile_runs.{profile_id}.outcome", "must be pass")
        for field, expected_value in expected.items():
            if row.get(field) != expected_value:
                _add(errors, f"{path}.mobile_runs.{profile_id}.{field}", f"must be {expected_value}")
        for field in ("browser_version", "operating_system_version", "device_model", "device_lab_ref"):
            if not _opaque_ref(row.get(field)):
                _add(errors, f"{path}.mobile_runs.{profile_id}.{field}", "must be an opaque, non-placeholder value")
        _validate_concrete_route_coverage(
            errors,
            path=f"{path}.mobile_runs.{profile_id}.tested_routes",
            required_routes=required_routes,
            observed_routes=row.get("tested_routes"),
        )
        if not REQUIRED_CRITICAL_SCENARIOS.issubset(_string_set(row.get("tested_scenarios"))):
            _add(errors, f"{path}.mobile_runs.{profile_id}.tested_scenarios", "all critical state scenarios must be tested")
        errors.extend(_evidence_errors(row.get("evidence"), path=f"{path}.mobile_runs.{profile_id}.evidence", now=now, maximum_age_hours=age))
    return errors


def _validate_cwv(
    market: Mapping[str, Any], contract: Mapping[str, Any], *, path: str, now: datetime, age: float
) -> list[str]:
    errors: list[str] = []
    cwv = _mapping(market.get("field_core_web_vitals"))
    errors.extend(_evidence_errors(cwv.get("evidence"), path=f"{path}.evidence", now=now, maximum_age_hours=age))
    source = _mapping(contract.get("field_core_web_vitals"))
    if cwv.get("measurement_scope") != "field_rum" or cwv.get("percentile") != 75:
        _add(errors, path, "measurement_scope must be field_rum and percentile must be 75")
    minimum_samples = float(source.get("minimum_samples_per_market_device_cohort", 200))
    minimum_days = float(source.get("minimum_window_days", 28))
    thresholds = _mapping(source.get("thresholds"))
    cohorts, valid = _rows_by(cwv.get("device_cohorts"), "cohort_id")
    if not valid or set(cohorts) != REQUIRED_CWV_COHORTS:
        _add(errors, f"{path}.device_cohorts", "must contain exactly desktop and mobile")
    for cohort_id in sorted(REQUIRED_CWV_COHORTS):
        cohort = cohorts.get(cohort_id, {})
        cohort_path = f"{path}.device_cohorts.{cohort_id}"
        sample_count = _number(cohort.get("sample_count"))
        if sample_count is None or sample_count < minimum_samples:
            _add(errors, f"{cohort_path}.sample_count", f"must be at least {minimum_samples:g}")
        start = _parse_time(cohort.get("window_start"))
        end = _parse_time(cohort.get("window_end"))
        if start is None or end is None:
            _add(errors, cohort_path, "window_start and window_end must be timezone-aware timestamps")
        else:
            if end <= start or (end - start).total_seconds() < minimum_days * 86400:
                _add(errors, cohort_path, f"field window must span at least {minimum_days:g} days")
            end_age = (now - end).total_seconds()
            if end_age < -300 or end_age > age * 3600:
                _add(errors, f"{cohort_path}.window_end", f"must be current within {age:g} hours")
        metrics = _mapping(cohort.get("metrics"))
        for metric, (_, fallback_unit) in CWV_CEILINGS.items():
            row = _mapping(metrics.get(metric))
            value = _number(row.get("value"))
            threshold = _mapping(thresholds.get(metric))
            maximum = float(threshold.get("maximum", CWV_CEILINGS[metric][0]))
            unit = str(threshold.get("unit", fallback_unit))
            if value is None or value > maximum:
                _add(errors, f"{cohort_path}.metrics.{metric}.value", f"must be at or below {maximum:g}")
            if row.get("unit") != unit:
                _add(errors, f"{cohort_path}.metrics.{metric}.unit", f"must be {unit}")
        errors.extend(
            _evidence_errors(
                cohort.get("evidence"),
                path=f"{cohort_path}.evidence",
                now=now,
                maximum_age_hours=age,
            )
        )
    return errors


def _validate_network(market: Mapping[str, Any], *, path: str, now: datetime, age: float) -> list[str]:
    errors: list[str] = []
    recovery = _mapping(market.get("degraded_network_recovery"))
    scenarios, valid = _rows_by(recovery.get("scenarios"), "scenario_id")
    if not valid or set(scenarios) != REQUIRED_NETWORK_SCENARIOS:
        _add(errors, f"{path}.scenarios", "required recovery scenarios are incomplete")
    for scenario_id in sorted(REQUIRED_NETWORK_SCENARIOS):
        row = scenarios.get(scenario_id, {})
        if row.get("outcome") != "pass" or row.get("recovered") is not True:
            _add(errors, f"{path}.scenarios.{scenario_id}", "must pass and recover")
        if row.get("no_data_loss") is not True or row.get("no_duplicate_mutation") is not True:
            _add(errors, f"{path}.scenarios.{scenario_id}", "must prove no data loss or duplicate mutation")
        errors.extend(_evidence_errors(row.get("evidence"), path=f"{path}.scenarios.{scenario_id}.evidence", now=now, maximum_age_hours=age))
    return errors


def _validate_critical_states(
    market: Mapping[str, Any], *, path: str, now: datetime, age: float
) -> list[str]:
    errors: list[str] = []
    state_group = _mapping(market.get("critical_state_scenarios"))
    scenarios, valid = _rows_by(state_group.get("scenarios"), "scenario_id")
    if not valid or set(scenarios) != REQUIRED_CRITICAL_SCENARIOS:
        _add(errors, f"{path}.scenarios", "critical state scenario set is incomplete")
    for scenario_id in sorted(REQUIRED_CRITICAL_SCENARIOS):
        row = scenarios.get(scenario_id, {})
        scenario_path = f"{path}.scenarios.{scenario_id}"
        if row.get("outcome") != "pass":
            _add(errors, f"{scenario_path}.outcome", "must be pass")
        if row.get("useful_next_action") is not True:
            _add(errors, f"{scenario_path}.useful_next_action", "must be true")
        if row.get("customer_data_preserved") is not True:
            _add(errors, f"{scenario_path}.customer_data_preserved", "must be true")
        errors.extend(
            _evidence_errors(
                row.get("evidence"),
                path=f"{scenario_path}.evidence",
                now=now,
                maximum_age_hours=age,
            )
        )
    return errors


def _validate_seo(
    market: Mapping[str, Any], contract: Mapping[str, Any], *, locale: str, path: str, now: datetime, age: float
) -> list[str]:
    errors: list[str] = []
    seo = _mapping(market.get("localized_seo"))
    errors.extend(_evidence_errors(seo.get("evidence"), path=f"{path}.evidence", now=now, maximum_age_hours=age))
    if seo.get("html_lang") != locale or seo.get("content_language") != locale:
        _add(errors, path, f"html_lang and content_language must both be {locale}")
    minimum_routes = float(_mapping(contract.get("localized_seo")).get("minimum_indexable_route_count", 3))
    if _number(seo.get("indexable_route_count")) is None or float(seo.get("indexable_route_count", 0)) < minimum_routes:
        _add(errors, f"{path}.indexable_route_count", f"must be at least {minimum_routes:g}")
    if _string_set(seo.get("hreflang_values")) != REQUIRED_HREFLANG:
        _add(errors, f"{path}.hreflang_values", "must contain the exact localized and x-default set")
    checks = _mapping(seo.get("checks"))
    for check in sorted(REQUIRED_SEO_CHECKS):
        if checks.get(check) is not True:
            _add(errors, f"{path}.checks.{check}", "must be true")
    return errors


def _validate_release_binding(
    receipt: Mapping[str, Any], *, expected_commit: str, expected_image: str, errors: list[str]
) -> None:
    identity = _mapping(receipt.get("release_identity"))
    if identity.get("git_commit") != expected_commit:
        _add(errors, "live_receipt.release_identity.git_commit", "does not match the expected release")
    if identity.get("image_digest") != expected_image:
        _add(errors, "live_receipt.release_identity.image_digest", "does not match the expected release")


def _validate_approvals_and_attestation(
    receipt: Mapping[str, Any],
    *,
    contract_sha256: str,
    expected_commit: str,
    expected_image: str,
    now: datetime,
    age: float,
) -> tuple[list[str], bool]:
    errors: list[str] = []
    approvals, valid = _rows_by(receipt.get("approvals"), "role")
    if not valid or set(approvals) != REQUIRED_APPROVALS:
        _add(errors, "live_receipt.approvals", "must contain every required approval exactly once")
    for role in sorted(REQUIRED_APPROVALS):
        approval = approvals.get(role, {})
        if approval.get("outcome") != "approved" or not _opaque_ref(approval.get("approver_ref")):
            _add(errors, f"live_receipt.approvals.{role}", "must be approved by an opaque, non-placeholder approver")
        errors.extend(_evidence_errors(approval.get("evidence"), path=f"live_receipt.approvals.{role}.evidence", now=now, maximum_age_hours=age))

    attestation = _mapping(receipt.get("independent_attestation"))
    try:
        verify_global_governance_attestation(
            attestation,
            expected_subject={
                "gate_id": GLOBAL_EXPERIENCE_GATE_ID,
                "receipt_contract": LIVE_RECEIPT_SCHEMA,
                "release_commit_sha": expected_commit,
                "release_image_digest": expected_image,
                "source_digests": {
                    "global_experience_contract_sha256": f"sha256:{contract_sha256}",
                },
                "payload_sha256": _attested_payload_digest(receipt),
            },
            observed_at=now,
        )
    except GlobalGovernanceAttestationError:
        independently_attested = False
    else:
        independently_attested = True
    if not independently_attested:
        _add(errors, "live_receipt.independent_attestation", "must independently attest the exact release")
    return errors, independently_attested


def build_global_experience_gate_receipt(
    *,
    contract_path: Path = DEFAULT_CONTRACT_PATH,
    live_receipt_path: Path | None,
    expected_commit: str,
    expected_image: str,
    maximum_age_hours: float | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate source and live evidence without inferring or generating proof."""

    evaluated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    contract, contract_sha256 = _load_json_with_sha256(contract_path)
    blockers = validate_contract(contract)
    policy_age = _number(_mapping(contract.get("live_evidence_policy")).get("maximum_receipt_age_hours"))
    policy_age = policy_age if policy_age and policy_age > 0 else 24.0
    requested_age = policy_age if maximum_age_hours is None else maximum_age_hours
    if not math.isfinite(float(requested_age)) or requested_age <= 0:
        blockers.append("maximum_age_hours: must be finite and greater than zero")
        requested_age = policy_age
    effective_age = min(float(policy_age), float(requested_age))

    if not _exact_git_sha(expected_commit):
        blockers.append("expected_commit: must be an exact, non-placeholder 40-character lowercase Git SHA")
    if not _exact_sha256(expected_image):
        blockers.append("expected_image: must be an exact, non-placeholder sha256 image digest")

    market_results: list[dict[str, Any]] = []
    live_receipt_age_seconds: float | None = None
    independently_attested = False
    receipt: Mapping[str, Any] = {}
    if live_receipt_path is None:
        blockers.append("live_receipt: required; the source contract is defined_not_live_evidence and cannot pass the gate")
    else:
        try:
            receipt = _load_json(live_receipt_path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            blockers.append(f"live_receipt: unreadable ({exc})")
            receipt = {}

    if receipt:
        if receipt.get("schema") != LIVE_RECEIPT_SCHEMA:
            _add(blockers, "live_receipt.schema", f"must be {LIVE_RECEIPT_SCHEMA}")
        if receipt.get("profile") != "launch" or receipt.get("claim_scope") != "core":
            _add(blockers, "live_receipt", "profile must be launch and claim_scope must be core")
        if receipt.get("contract_sha256") != contract_sha256:
            _add(blockers, "live_receipt.contract_sha256", "does not match the evaluated source contract")
        _validate_release_binding(receipt, expected_commit=expected_commit, expected_image=expected_image, errors=blockers)

        generated_at = _parse_time(receipt.get("generated_at"))
        if generated_at is None:
            _add(blockers, "live_receipt.generated_at", "must be a timezone-aware timestamp")
        else:
            live_receipt_age_seconds = (evaluated_at - generated_at).total_seconds()
            if live_receipt_age_seconds < -300:
                _add(blockers, "live_receipt.generated_at", "is more than five minutes in the future")
            elif live_receipt_age_seconds > effective_age * 3600:
                _add(blockers, "live_receipt.generated_at", f"is stale (maximum {effective_age:g} hours)")

        markets, valid = _rows_by(receipt.get("markets"), "country_code")
        if not valid or set(markets) != set(EXPECTED_MARKETS):
            _add(blockers, "live_receipt.markets", "must contain exactly AT, DE, and CR once each")
        for country_code, locale in EXPECTED_MARKETS.items():
            market = markets.get(country_code, {})
            market_errors: list[str] = []
            root = f"markets.{country_code}"
            if market.get("locale") != locale:
                _add(market_errors, f"{root}.locale", f"must be {locale}")
            for field, expected in EXPECTED_MARKET_METADATA[country_code].items():
                if market.get(field) != expected:
                    _add(market_errors, f"{root}.{field}", f"must be {expected}")
            market_errors.extend(_validate_native_review(market, contract, locale=locale, path=f"{root}.native_content_review", now=evaluated_at, age=effective_age))
            market_errors.extend(_validate_accessibility(market, contract, path=f"{root}.accessibility", now=evaluated_at, age=effective_age))
            market_errors.extend(_validate_browser_device(market, contract, path=f"{root}.browser_device_coverage", now=evaluated_at, age=effective_age))
            market_errors.extend(_validate_cwv(market, contract, path=f"{root}.field_core_web_vitals", now=evaluated_at, age=effective_age))
            market_errors.extend(_validate_network(market, path=f"{root}.degraded_network_recovery", now=evaluated_at, age=effective_age))
            market_errors.extend(_validate_critical_states(market, path=f"{root}.critical_state_scenarios", now=evaluated_at, age=effective_age))
            market_errors.extend(_validate_seo(market, contract, locale=locale, path=f"{root}.localized_seo", now=evaluated_at, age=effective_age))
            blockers.extend(market_errors)
            market_results.append(
                {
                    "country_code": country_code,
                    "locale": locale,
                    "status": "pass" if not market_errors else "blocked",
                    "blockers": market_errors,
                }
            )

        attestation_errors, independently_attested = _validate_approvals_and_attestation(
            receipt,
            contract_sha256=contract_sha256,
            expected_commit=expected_commit,
            expected_image=expected_image,
            now=evaluated_at,
            age=effective_age,
        )
        blockers.extend(attestation_errors)

    blockers = list(dict.fromkeys(blockers))
    return {
        "schema": GATE_RECEIPT_SCHEMA,
        "generated_at": evaluated_at.isoformat().replace("+00:00", "Z"),
        "status": "pass" if not blockers else "blocked",
        "service": "propertyquarry",
        "profile": "launch",
        "claim_scope": "core",
        "source_contract_status": contract.get("source_contract_status"),
        "contract_path": str(contract_path),
        "contract_sha256": contract_sha256,
        "live_receipt_path": str(live_receipt_path) if live_receipt_path is not None else None,
        "live_receipt_age_seconds": live_receipt_age_seconds,
        "maximum_age_hours": effective_age,
        "release_identity": {
            "commit_sha": expected_commit,
            "image_digest": expected_image,
        },
        "required_markets": [
            {
                "country_code": country_code,
                "locale": locale,
                **EXPECTED_MARKET_METADATA[country_code],
            }
            for country_code, locale in EXPECTED_MARKETS.items()
        ],
        "required_customer_routes": sorted(REQUIRED_CUSTOMER_ROUTES),
        "required_critical_scenarios": sorted(REQUIRED_CRITICAL_SCENARIOS),
        "independently_attested": independently_attested,
        "market_results": market_results,
        "blockers": blockers,
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary_path = Path(handle.name)
    os.replace(temporary_path, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT_PATH)
    parser.add_argument("--live-receipt", type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--maximum-age-hours", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-on-blocked", action="store_true")
    args = parser.parse_args(argv)

    receipt = build_global_experience_gate_receipt(
        contract_path=args.contract,
        live_receipt_path=args.live_receipt,
        expected_commit=args.expected_commit,
        expected_image=args.expected_image,
        maximum_age_hours=args.maximum_age_hours,
    )
    if args.output:
        _write_json_atomic(args.output, receipt)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 1 if args.fail_on_blocked and receipt["status"] != "pass" else 0


if __name__ == "__main__":
    raise SystemExit(main())
