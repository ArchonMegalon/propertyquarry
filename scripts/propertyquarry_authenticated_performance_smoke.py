#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import html
import importlib.metadata
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import urlparse, urlsplit

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ea"))
sys.path.insert(0, str(ROOT / "scripts"))

try:
    from scripts.propertyquarry_playwright_runtime import (
        normalize_playwright_engine,
        playwright_engine_executable,
        playwright_engine_launch_browser,
    )
except ModuleNotFoundError:
    from propertyquarry_playwright_runtime import (  # type: ignore[no-redef]
        normalize_playwright_engine,
        playwright_engine_executable,
        playwright_engine_launch_browser,
    )


DEFAULT_ROUTE_BUDGET_MS = {
    "/sign-in": 1200,
    "/app/search": 1200,
    "/app/agents": 1200,
    "/app/properties": 1200,
    "/app/shortlist": 1200,
    "/app/alerts": 1200,
    "/app/account": 1200,
    "/app/billing": 1200,
    "/app/settings/google": 1200,
    "/app/settings/access": 1200,
    "/app/settings/usage": 1200,
    "/app/settings/support": 1200,
    "/app/settings/trust": 1200,
    "/app/settings/invitations": 1200,
}
DEFAULT_SEARCH_COMPRESSED_MAX_BYTES = 240_000
AUTHENTICATED_PERFORMANCE_SCHEMA = "propertyquarry.authenticated_performance.v2"
RELEASE_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_IMAGE_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RELEASE_DEPLOYMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REPLICA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_VERSION_RESPONSE_BYTES = 256 * 1024
MIN_CHROMIUM_EXECUTABLE_BYTES = 1_048_576
MAX_CHROMIUM_EXECUTABLE_BYTES = 1_073_741_824
DEFAULT_COLD_ROUTE_BUDGET_MS = 2400
MIN_ROUTE_BUDGET_MS = 50
MAX_ROUTE_BUDGET_MS = 60_000
CONSTRAINED_CLIENT_PROFILE_NAME = "low_end_mobile_lab_v1"
CONSTRAINED_CLIENT_PROFILE_FIELDS = (
    "name",
    "cpu_slowdown_rate",
    "network_latency_ms",
    "download_kbps",
    "upload_kbps",
    "viewport_width",
    "viewport_height",
    "device_scale_factor",
    "cold_navigation_budget_ms",
    "warm_navigation_budget_ms",
    "max_request_count",
    "max_transferred_bytes",
    "max_failed_requests",
    "slowest_resource_limit",
    "navigation_timeout_ms",
)
PASSING_ENGINE_RECEIPT_FIELDS = (
    "status",
    "browser_engine",
    "identity",
    "launch_binding",
    "profile_support",
    "authentication",
    "measurements",
    "cold_to_warm",
    "limitations",
    "field_core_web_vitals_claimed",
    "physical_device_claimed",
)
CHROMIUM_LAUNCH_BINDING_FIELDS = (
    "mechanism",
    "executable_path",
    "executable_sha256",
    "prelaunch_bytes",
    "postlaunch_identity_match",
)
BROWSER_IDENTITY_FIELDS = (
    "engine",
    "browser_version",
    "playwright_version",
    "executable_path",
    "executable_sha256",
    "executable_bytes",
)
BROWSER_MEASUREMENT_FIELDS = (
    "phase",
    "cache_state",
    "duration_ms",
    "status_code",
    "final_url",
    "document_release_identity",
    "document_authentication_binding",
    "request_count",
    "transferred_bytes",
    "failed_request_count",
    "failed_requests",
    "incomplete_request_count",
    "cache_hit_count",
    "subresource_cache_hit_count",
    "slowest_resources",
    "navigation_timing",
    "checks",
    "ok",
)
DOCUMENT_RELEASE_IDENTITY_FIELDS = (
    "commit_sha",
    "image_digest",
    "deployment_id",
    "manifest_status",
    "manifest_sha256",
    "replica_id",
)
DOCUMENT_RELEASE_IDENTITY_HEADERS = {
    "commit_sha": "x-propertyquarry-release-commit",
    "image_digest": "x-propertyquarry-release-image",
    "deployment_id": "x-propertyquarry-release-deployment",
    "manifest_status": "x-propertyquarry-release-manifest-status",
    "manifest_sha256": "x-propertyquarry-release-manifest-sha256",
    "replica_id": "x-propertyquarry-replica-id",
}
RELEASE_PROBE_NONCE_SHA256_RESPONSE_HEADER = (
    "x-propertyquarry-release-probe-nonce-sha256"
)
DOCUMENT_AUTHENTICATION_BINDING_FIELDS = (
    "cache_control",
    "expected_nonce_sha256",
    "acknowledged_nonce_sha256",
)
CDP_RESOURCE_TYPES = frozenset(
    {
        "Document",
        "Stylesheet",
        "Image",
        "Media",
        "Font",
        "Script",
        "TextTrack",
        "XHR",
        "Fetch",
        "Prefetch",
        "EventSource",
        "WebSocket",
        "Manifest",
        "SignedExchange",
        "Ping",
        "CSPViolationReport",
        "Preflight",
        "Other",
    }
)
SIGNED_RELEASE_PROBE_AUTHENTICATION_FIELDS = (
    "method",
    "navigation_signing_mechanism",
    "playwright_routing_used",
    "subresource_http_cache_preserved",
    "signed_navigation_count",
    "distinct_nonce_count",
    "target_surface_observed",
    "release_probe_secret_persisted",
)
WORKSPACE_BOOTSTRAP_AUTHENTICATION_FIELDS = (
    "method",
    "cookie_observed",
    "target_surface_observed",
)
PERFORMANCE_SMOKE_LOCATION_QUERY = "1020 Vienna"
PERFORMANCE_SMOKE_MIN_AREA_M2 = 60
PERFORMANCE_SMOKE_MAX_PRICE_EUR = 1600

FORBIDDEN_CUSTOMER_NOISE = (
    "billing truth",
    "plan and limits",
    "refresh delivery",
    "repair status checked",
    "what still worked",
    "main blocker",
    "best next move",
    "search posture",
    "account posture",
    "latest run posture",
    "saved posture",
    "billing posture",
    "plan and billing posture",
    "energy posture",
    "running-cost posture",
    "authority posture",
    "governed review",
    "workspace diagnostics bundle",
    "open bundle",
    "support posture",
    "runtime posture",
    "provider posture",
    "channel receipt",
    "install receipt",
    "support bundle",
    "export bundle",
    "outcome posture",
    "follow-up artifacts",
    "proof of value",
    "operator center",
)

FORBIDDEN_VISIBLE_INTERNAL_COPY = (
    "current best so far",
    "decision support",
    "dossier",
    "evidence",
    "magic fit",
    "magicfit",
    "no source completed",
    "proof",
    "provider webpage",
    "run ranking",
    "source completed",
    "suppressed_generic_listing_page",
    "verified",
)

SHARED_TOP_NAV_LABELS = (
    "Search",
    "Shortlist",
    "Research",
    "Account",
)

BILLING_FAIL_CLOSED_STATE_MARKERS = (
    "billing portal is still being connected",
    "still opens another sign-in",
    "billing account host is not ready yet",
)

ALLOWED_RYBBIT_APP_EVENTS = {
    "pq.search.started",
    "pq.search.results_viewed",
    "pq.search.agent_created",
    "pq.search.agent_updated",
    "pq.search.agent_notification_sent",
    "pq.search.suppressed_viewed",
    "pq.property.opened",
    "pq.property.map_opened",
    "pq.dossier.opened",
    "pq.tour.opened",
    "pq.flythrough.opened",
    "pq.decision.saved",
    "pq.reason.selected",
    "pq.agent_question.created",
    "pq.document.requested",
    "pq.packet.shared",
    "pq.email.clicked",
}

ALLOWED_RYBBIT_ATTRIBUTE_NAMES = {
    "data-rybbit-event",
    "data-rybbit-prop-cta-key",
    "data-rybbit-prop-surface",
}

FORBIDDEN_RYBBIT_PAYLOAD_TOKENS = (
    "candidate_ref",
    "data-rybbit-prop-candidate",
    "email",
    "exact_address",
    "listing_id",
    "listing_url",
    "phone",
    "principal",
    "property_url",
    "run_id",
    "saved_search_id",
    "selected_platform_count",
    "signed",
    "telegram",
)
PROVIDER_FREE_ENV_PREFIXES = (
    "BROWSERACT_",
    "EA_ENV_TEABLE_",
    "EA_GEMINI_VORTEX_SLOT_",
    "GOOGLE_API_KEY_FALLBACK_",
    "ONEMIN_AI_API_KEY",
    "PROPERTYQUARRY_TEABLE_",
    "TEABLE_",
)
PROVIDER_FREE_ENV_NAMES = {
    "AI_MAGICX_API_KEY",
    "DATABASE_URL",
    "EA_GEMINI_VORTEX_COMMAND",
    "EA_RESPONSES_MAGICX_API_KEY",
    "ONEMIN_DIRECT_API_KEYS_JSON",
    "ONEMIN_DIRECT_API_KEYS_JSON_FILE",
}
PERFORMANCE_SUBPROCESS_ENV_ALLOWLIST = frozenset(
    {
        "CI",
        "DISPLAY",
        "GITHUB_ACTIONS",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PROPERTYQUARRY_PLAYWRIGHT_WEBKIT_CPU_AFFINITY_LIMIT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
    }
)

FORBIDDEN_BILLING_SURFACE_TOKENS = (
    "accounting lane",
    "billing truth",
    "billing history",
    "brilliant directories",
    "brilliantdirectories",
    "commercial truth",
    "compare plans",
    "invoice handoff",
    "invoices",
    "open pricing",
    "view plans",
    "payfunnels",
    "payfunnels/order",
    "plan and limits",
    "plan and payments",
    "plan unit",
    "your plan",
)
FORBIDDEN_COMPARE_CARD_TOKENS = (
    "compare cards",
    "prd-compare",
    "Decision support",
    "The next-best properties from this run",
    "Other ranked homes from this run",
)

CONTENT_FIRST_MOBILE_PATHS = {
    "/app/agents",
    "/app/alerts",
    "/app/account",
    "/app/billing",
}

SETTINGS_MOBILE_PATHS = {
    "/app/settings/google",
    "/app/settings/access",
    "/app/settings/usage",
    "/app/settings/support",
    "/app/settings/trust",
    "/app/settings/invitations",
}


class PerformanceConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ConstrainedClientProfile:
    name: str = CONSTRAINED_CLIENT_PROFILE_NAME
    cpu_slowdown_rate: int = 4
    network_latency_ms: int = 150
    download_kbps: int = 1600
    upload_kbps: int = 750
    viewport_width: int = 390
    viewport_height: int = 844
    device_scale_factor: int = 1
    cold_navigation_budget_ms: int = 15_000
    warm_navigation_budget_ms: int = 8_000
    max_request_count: int = 120
    max_transferred_bytes: int = 3_000_000
    max_failed_requests: int = 0
    slowest_resource_limit: int = 5
    navigation_timeout_ms: int = 30_000


def _bounded_int(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int:
        raise PerformanceConfigError(f"{field}_must_be_integer")
    if value < minimum or value > maximum:
        raise PerformanceConfigError(
            f"{field}_out_of_range:{minimum}:{maximum}"
        )
    return value


def validate_constrained_client_profile(
    profile: ConstrainedClientProfile,
) -> ConstrainedClientProfile:
    if type(profile) is not ConstrainedClientProfile:
        raise PerformanceConfigError("constrained_client_profile_type_invalid")
    if profile.name != CONSTRAINED_CLIENT_PROFILE_NAME:
        raise PerformanceConfigError("constrained_client_profile_name_invalid")
    _bounded_int(
        profile.cpu_slowdown_rate,
        field="cpu_slowdown_rate",
        minimum=1,
        maximum=20,
    )
    _bounded_int(
        profile.network_latency_ms,
        field="network_latency_ms",
        minimum=20,
        maximum=5_000,
    )
    _bounded_int(
        profile.download_kbps,
        field="download_kbps",
        minimum=64,
        maximum=100_000,
    )
    _bounded_int(
        profile.upload_kbps,
        field="upload_kbps",
        minimum=32,
        maximum=100_000,
    )
    _bounded_int(
        profile.viewport_width,
        field="viewport_width",
        minimum=320,
        maximum=1_280,
    )
    _bounded_int(
        profile.viewport_height,
        field="viewport_height",
        minimum=480,
        maximum=2_400,
    )
    _bounded_int(
        profile.device_scale_factor,
        field="device_scale_factor",
        minimum=1,
        maximum=3,
    )
    _bounded_int(
        profile.cold_navigation_budget_ms,
        field="cold_navigation_budget_ms",
        minimum=500,
        maximum=120_000,
    )
    _bounded_int(
        profile.warm_navigation_budget_ms,
        field="warm_navigation_budget_ms",
        minimum=250,
        maximum=120_000,
    )
    if profile.warm_navigation_budget_ms > profile.cold_navigation_budget_ms:
        raise PerformanceConfigError(
            "warm_navigation_budget_ms_must_not_exceed_cold_budget"
        )
    _bounded_int(
        profile.max_request_count,
        field="max_request_count",
        minimum=1,
        maximum=1_000,
    )
    _bounded_int(
        profile.max_transferred_bytes,
        field="max_transferred_bytes",
        minimum=1_024,
        maximum=100_000_000,
    )
    _bounded_int(
        profile.max_failed_requests,
        field="max_failed_requests",
        minimum=0,
        maximum=100,
    )
    _bounded_int(
        profile.slowest_resource_limit,
        field="slowest_resource_limit",
        minimum=1,
        maximum=20,
    )
    _bounded_int(
        profile.navigation_timeout_ms,
        field="navigation_timeout_ms",
        minimum=1_000,
        maximum=120_000,
    )
    if profile.navigation_timeout_ms < profile.cold_navigation_budget_ms:
        raise PerformanceConfigError(
            "navigation_timeout_ms_must_cover_cold_budget"
        )
    return profile


def constrained_client_profile_from_config(
    config: Mapping[str, object] | ConstrainedClientProfile | None,
) -> ConstrainedClientProfile:
    if config is None:
        return validate_constrained_client_profile(ConstrainedClientProfile())
    if type(config) is ConstrainedClientProfile:
        return validate_constrained_client_profile(config)
    if type(config) is not dict:
        raise PerformanceConfigError("constrained_client_config_must_be_object")
    if tuple(config) != CONSTRAINED_CLIENT_PROFILE_FIELDS:
        raise PerformanceConfigError(
            "constrained_client_config_fields_must_be_exact_ordered_set:"
            + ",".join(str(field) for field in config)
        )
    return validate_constrained_client_profile(ConstrainedClientProfile(**config))


def constrained_client_profile_receipt(
    profile: ConstrainedClientProfile,
) -> dict[str, object]:
    validated = validate_constrained_client_profile(profile)
    return {
        "name": validated.name,
        "cpu": {
            "slowdown_rate": validated.cpu_slowdown_rate,
            "claim": "browser_lab_emulation_only",
        },
        "network": {
            "latency_ms": validated.network_latency_ms,
            "download_kbps": validated.download_kbps,
            "upload_kbps": validated.upload_kbps,
            "offline": False,
            "claim": "browser_lab_emulation_only",
        },
        "viewport": {
            "width": validated.viewport_width,
            "height": validated.viewport_height,
            "device_scale_factor": validated.device_scale_factor,
            "is_mobile": True,
            "has_touch": True,
            "claim": "emulated_viewport_not_physical_device",
        },
        "cache_policy": {
            "cold": "browser_http_cache_cleared_before_first_navigation",
            "warm": "same_context_repeat_navigation_cache_eligible",
            "service_workers": "blocked",
        },
        "thresholds": {
            "cold_navigation_budget_ms": validated.cold_navigation_budget_ms,
            "warm_navigation_budget_ms": validated.warm_navigation_budget_ms,
            "max_request_count": validated.max_request_count,
            "max_transferred_bytes": validated.max_transferred_bytes,
            "max_failed_requests": validated.max_failed_requests,
            "slowest_resource_limit": validated.slowest_resource_limit,
            "navigation_timeout_ms": validated.navigation_timeout_ms,
        },
    }


def _validate_route_budgets(
    *,
    warm_route_budget_ms: object,
    cold_route_budget_ms: object,
) -> tuple[int, int]:
    warm_budget = _bounded_int(
        warm_route_budget_ms,
        field="route_budget_ms",
        minimum=MIN_ROUTE_BUDGET_MS,
        maximum=MAX_ROUTE_BUDGET_MS,
    )
    cold_budget = _bounded_int(
        cold_route_budget_ms,
        field="cold_route_budget_ms",
        minimum=MIN_ROUTE_BUDGET_MS,
        maximum=MAX_ROUTE_BUDGET_MS,
    )
    if cold_budget < warm_budget:
        raise PerformanceConfigError(
            "cold_route_budget_ms_must_not_be_less_than_warm_budget"
        )
    return warm_budget, cold_budget


def _validated_browser_url(value: object, *, field: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise PerformanceConfigError(f"{field}_invalid") from exc
    if (
        not raw
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PerformanceConfigError(f"{field}_invalid")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise PerformanceConfigError(f"{field}_must_use_https_or_loopback_http")
    if port is not None and (port < 1 or port > 65_535):
        raise PerformanceConfigError(f"{field}_port_invalid")
    return raw


def _url_origin(value: str) -> str:
    parsed = urlsplit(value)
    hostname = str(parsed.hostname or "").lower()
    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    return f"{parsed.scheme.lower()}://{hostname}:{port}"


def _observed_url_is_exact_target(value: object, *, expected_target: str) -> bool:
    raw = str(value or "").strip()
    try:
        observed = urlsplit(raw)
        expected = urlsplit(expected_target)
        observed.port
        expected.port
    except ValueError:
        return False
    return bool(
        raw
        and observed.scheme in {"http", "https"}
        and observed.hostname
        and observed.username is None
        and observed.password is None
        and not observed.query
        and not observed.fragment
        and _url_origin(raw) == _url_origin(expected_target)
        and observed.path == expected.path == "/app/search"
    )


def _sanitized_resource_url(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return "invalid-url"
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return "invalid-url"
    hostname = str(parsed.hostname or "").lower()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    rendered_port = f":{port}" if port is not None and port != default_port else ""
    origin = f"{parsed.scheme.lower()}://{rendered_host}{rendered_port}"
    path = parsed.path or "/"
    if path in {"/version", "/app/search"}:
        receipt_path = path
    elif path.startswith("/app/assets/"):
        receipt_path = "/app/assets/:asset"
    else:
        receipt_path = "/_path-sha256/" + hashlib.sha256(
            path.encode("utf-8")
        ).hexdigest()[:24]
    return f"{origin}{receipt_path}"[:320]


def _expected_release_identity(
    *,
    commit_sha: object,
    image_digest: object,
    deployment_id: object,
    manifest_sha256: object,
) -> tuple[dict[str, str], list[str]]:
    raw_manifest_sha256 = manifest_sha256
    identity = {
        "commit_sha": str(commit_sha or "").strip().lower(),
        "image_digest": str(image_digest or "").strip().lower(),
        "deployment_id": str(deployment_id or "").strip(),
        "manifest_sha256": str(manifest_sha256 or "").strip().lower(),
    }
    errors: list[str] = []
    if RELEASE_COMMIT_SHA_RE.fullmatch(identity["commit_sha"]) is None:
        errors.append("release_commit_sha_missing_or_invalid")
    if RELEASE_IMAGE_DIGEST_RE.fullmatch(identity["image_digest"]) is None:
        errors.append("release_image_digest_missing_or_invalid")
    if RELEASE_DEPLOYMENT_ID_RE.fullmatch(identity["deployment_id"]) is None:
        errors.append("release_deployment_id_missing_or_invalid")
    if (
        type(raw_manifest_sha256) is not str
        or raw_manifest_sha256 != identity["manifest_sha256"]
        or re.fullmatch(r"[0-9a-f]{64}", identity["manifest_sha256"]) is None
        or len(set(identity["manifest_sha256"])) == 1
    ):
        errors.append("release_manifest_sha256_missing_or_invalid")
    return identity, errors


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


def probe_live_release_identity(
    *,
    target_url: str,
    expected_release_identity: Mapping[str, str],
    timeout_seconds: float = 10.0,
) -> dict[str, object]:
    target = _validated_browser_url(target_url, field="target_url")
    parsed = urlsplit(target)
    hostname = str(parsed.hostname or "").lower()
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme == "https" else 80
    rendered_port = (
        f":{parsed.port}"
        if parsed.port is not None and parsed.port != default_port
        else ""
    )
    version_url = f"{parsed.scheme.lower()}://{rendered_host}{rendered_port}/version"
    request = urllib.request.Request(
        version_url,
        headers={
            "Accept": "application/json",
            "User-Agent": "PropertyQuarry-authenticated-performance/2.0",
        },
        method="GET",
    )
    status_code = 0
    body = b""
    try:
        with urllib.request.build_opener(_NoRedirect).open(
            request,
            timeout=max(1.0, min(float(timeout_seconds), 30.0)),
        ) as response:
            status_code = int(getattr(response, "status", 0) or 0)
            body = response.read(MAX_VERSION_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code or 0)
        body = exc.read(MAX_VERSION_RESPONSE_BYTES + 1)
    except BaseException as exc:
        return {
            "status": "fail",
            "version_url": _sanitized_resource_url(version_url),
            "status_code": 0,
            "tls_verified": parsed.scheme == "https",
            "expected": dict(expected_release_identity),
            "observed": {},
            "matches_expected": False,
            "error": f"version_probe_failed:{type(exc).__name__}",
            "credential_persisted": False,
        }

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {}
        for key, value in pairs:
            if key in payload:
                raise ValueError(f"duplicate_field:{key}")
            payload[key] = value
        return payload

    payload: dict[str, object] = {}
    error = ""
    if len(body) > MAX_VERSION_RESPONSE_BYTES:
        error = "version_response_too_large"
    else:
        try:
            decoded = json.loads(body.decode("utf-8"), object_pairs_hook=reject_duplicates)
            if not isinstance(decoded, dict):
                raise ValueError("version_response_not_object")
            payload = decoded
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            error = f"version_response_invalid:{type(exc).__name__}"
    source_fields = {
        "commit_sha": "release_commit_sha",
        "image_digest": "release_image_digest",
        "deployment_id": "release_deployment_id",
        "manifest_status": "release_manifest_status",
        "manifest_sha256": "release_manifest_sha256",
        "replica_id": "replica_id",
    }
    identity_types_valid = all(
        type(payload.get(source_field)) is str
        for source_field in source_fields.values()
    )
    if not identity_types_valid and not error:
        error = "version_response_identity_types_invalid"
    identity_values_canonical = identity_types_valid and all(
        payload[source_field] == payload[source_field].strip()
        and (
            field not in {"commit_sha", "image_digest", "manifest_sha256"}
            or payload[source_field] == payload[source_field].lower()
        )
        for field, source_field in source_fields.items()
    )
    if not identity_values_canonical and not error:
        error = "version_response_identity_values_noncanonical"
    observed = {
        field: (
            payload[source_field].strip().lower()
            if type(payload.get(source_field)) is str
            and field in {"commit_sha", "image_digest", "manifest_sha256"}
            else payload[source_field].strip()
            if type(payload.get(source_field)) is str
            else ""
        )
        for field, source_field in source_fields.items()
    }
    matches = (
        status_code == 200
        and not error
        and RELEASE_COMMIT_SHA_RE.fullmatch(observed["commit_sha"]) is not None
        and RELEASE_IMAGE_DIGEST_RE.fullmatch(observed["image_digest"]) is not None
        and RELEASE_DEPLOYMENT_ID_RE.fullmatch(observed["deployment_id"])
        is not None
        and observed["manifest_status"] == "complete"
        and observed["commit_sha"] == expected_release_identity.get("commit_sha")
        and observed["image_digest"] == expected_release_identity.get("image_digest")
        and observed["deployment_id"]
        == expected_release_identity.get("deployment_id")
        and re.fullmatch(r"[0-9a-f]{64}", observed["manifest_sha256"])
        is not None
        and observed["manifest_sha256"]
        == expected_release_identity.get("manifest_sha256")
        and REPLICA_ID_RE.fullmatch(observed["replica_id"]) is not None
    )
    return {
        "status": "pass" if matches else "fail",
        "version_url": _sanitized_resource_url(version_url),
        "status_code": status_code,
        "tls_verified": parsed.scheme == "https",
        "expected": dict(expected_release_identity),
        "observed": observed,
        "matches_expected": matches,
        "error": error or ("" if matches else "release_identity_mismatch"),
        "credential_persisted": False,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_secure_chromium_parent(path: Path) -> int:
    """Open and validate every ancestor without following symlinks."""

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, directory_flags)
        components = path.parent.parts[1:]
        for component in (None, *components):
            if component is not None:
                child = os.open(component, directory_flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            sticky_root_directory = (
                metadata.st_uid == 0
                and bool(metadata.st_mode & stat.S_ISVTX)
            )
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in {0, os.geteuid()}
                or (mode & 0o022 and not sticky_root_directory)
            ):
                raise PerformanceConfigError(
                    "expected_chromium_executable_directory_chain_unsafe"
                )
        return descriptor
    except PerformanceConfigError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise PerformanceConfigError(
            f"expected_chromium_executable_directory_chain_failed:{type(exc).__name__}"
        ) from exc


def _verified_expected_chromium_executable(
    *,
    executable_path: object,
    executable_sha256: object,
) -> dict[str, object]:
    raw_path = executable_path if type(executable_path) is str else ""
    raw_sha256 = executable_sha256 if type(executable_sha256) is str else ""
    path = Path(raw_path)
    if (
        not raw_path
        or raw_path != raw_path.strip()
        or not path.is_absolute()
        or os.path.normpath(raw_path) != raw_path
        or os.path.realpath(raw_path) != raw_path
    ):
        raise PerformanceConfigError("expected_chromium_executable_path_invalid")
    if (
        re.fullmatch(r"[0-9a-f]{64}", raw_sha256) is None
        or len(set(raw_sha256)) == 1
    ):
        raise PerformanceConfigError("expected_chromium_executable_sha256_invalid")
    parent_descriptor = _open_secure_chromium_parent(path)
    descriptor = -1
    try:
        before = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        os.close(parent_descriptor)
        raise PerformanceConfigError(
            f"expected_chromium_executable_lstat_failed:{type(exc).__name__}"
        ) from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) & 0o111 == 0
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_uid not in {0, os.geteuid()}
        or before.st_nlink != 1
        or not MIN_CHROMIUM_EXECUTABLE_BYTES
        <= before.st_size
        <= MAX_CHROMIUM_EXECUTABLE_BYTES
        or path.name.lower()
        not in {"chrome", "chromium", "headless_shell", "chrome-headless-shell"}
        or not any("chrom" in component.lower() for component in path.parts[:-1])
    ):
        os.close(parent_descriptor)
        raise PerformanceConfigError("expected_chromium_executable_unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    prefix = b""
    chromium_marker_observed = False
    marker_window = b""
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        os.close(parent_descriptor)
        raise PerformanceConfigError(
            f"expected_chromium_executable_open_failed:{type(exc).__name__}"
        ) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            raise PerformanceConfigError("expected_chromium_executable_changed")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            if not prefix:
                prefix = chunk[:4]
            marker_window = (marker_window + chunk)[-2 * 1024 * 1024 :]
            if b"Chromium" in marker_window or b"Google Chrome" in marker_window:
                chromium_marker_observed = True
            digest.update(chunk)
        after = os.fstat(descriptor)
        after_path = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
            or after.st_ctime_ns != opened.st_ctime_ns
            or after_path.st_dev != after.st_dev
            or after_path.st_ino != after.st_ino
            or after_path.st_size != after.st_size
        ):
            raise PerformanceConfigError("expected_chromium_executable_changed")
    except PerformanceConfigError:
        raise
    except OSError as exc:
        raise PerformanceConfigError(
            f"expected_chromium_executable_hash_failed:{type(exc).__name__}"
        ) from exc
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)
    if prefix != b"\x7fELF" or not chromium_marker_observed:
        raise PerformanceConfigError("expected_chromium_executable_not_chromium_elf")
    observed_sha256 = digest.hexdigest()
    if observed_sha256 != raw_sha256:
        raise PerformanceConfigError("expected_chromium_executable_digest_mismatch")
    return {
        "executable_path": raw_path,
        "executable_sha256": observed_sha256,
        "executable_bytes": before.st_size,
    }


def _safe_browser_error(exc: BaseException) -> str:
    raw_message = str(exc or "").strip()
    # Only retain machine-style error identifiers emitted by this module. Raw
    # Playwright messages can contain URLs, headers, local paths, or command
    # lines and therefore never belong in a portable release receipt.
    safe_message = (
        raw_message
        if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", raw_message)
        else "browser_collection_failed"
    )
    return f"{type(exc).__name__}:{safe_message}"


class _BrowserWaterfallCapture:
    def __init__(self, session: Any, *, slowest_resource_limit: int) -> None:
        self._active_phase = ""
        self._request_count = 0
        self._entries: dict[str, dict[str, object]] = {}
        self._completed: list[dict[str, object]] = []
        self._slowest_resource_limit = slowest_resource_limit
        session.on("Network.requestWillBeSent", self._request_will_be_sent)
        session.on("Network.responseReceived", self._response_received)
        session.on("Network.requestServedFromCache", self._request_served_from_cache)
        session.on("Network.loadingFinished", self._loading_finished)
        session.on("Network.loadingFailed", self._loading_failed)

    def begin(self, phase: str) -> None:
        if phase not in {"cold", "warm"}:
            raise PerformanceConfigError(f"browser_phase_invalid:{phase}")
        self._active_phase = phase
        self._request_count = 0
        self._entries = {}
        self._completed = []

    def _request_will_be_sent(self, event: Mapping[str, object]) -> None:
        if not self._active_phase:
            return
        request_id = str(event.get("requestId") or "").strip()
        request = event.get("request")
        if not request_id or not isinstance(request, Mapping):
            return
        timestamp = event.get("timestamp")
        if type(timestamp) not in {int, float}:
            return
        existing = self._entries.pop(request_id, None)
        if existing is not None:
            existing["end_timestamp"] = float(timestamp)
            existing["redirected"] = True
            self._completed.append(existing)
        self._request_count += 1
        self._entries[request_id] = {
            "url": _sanitized_resource_url(request.get("url")),
            "resource_type": str(event.get("type") or "Other"),
            "start_timestamp": float(timestamp),
            "end_timestamp": float(timestamp),
            "status_code": 0,
            "transferred_bytes": 0,
            "cache_source": "network",
            "failed": False,
            "failure": "",
        }

    def _response_received(self, event: Mapping[str, object]) -> None:
        request_id = str(event.get("requestId") or "").strip()
        entry = self._entries.get(request_id)
        response = event.get("response")
        if entry is None or not isinstance(response, Mapping):
            return
        status = response.get("status")
        if type(status) in {int, float}:
            entry["status_code"] = int(status)
            if int(status) >= 400:
                entry["failed"] = True
                entry["failure"] = f"http_status_{int(status)}"
        if response.get("fromServiceWorker") is True:
            entry["cache_source"] = "service_worker"
            entry["failed"] = True
            entry["failure"] = "service_worker_response_forbidden"
        elif response.get("fromDiskCache") is True:
            entry["cache_source"] = "disk_cache"

    def _request_served_from_cache(self, event: Mapping[str, object]) -> None:
        request_id = str(event.get("requestId") or "").strip()
        entry = self._entries.get(request_id)
        if entry is not None:
            entry["cache_source"] = "browser_cache"

    def _loading_finished(self, event: Mapping[str, object]) -> None:
        request_id = str(event.get("requestId") or "").strip()
        entry = self._entries.pop(request_id, None)
        if entry is None:
            return
        timestamp = event.get("timestamp")
        encoded_data_length = event.get("encodedDataLength")
        if type(timestamp) in {int, float}:
            entry["end_timestamp"] = float(timestamp)
        if type(encoded_data_length) in {int, float}:
            entry["transferred_bytes"] = max(0, round(float(encoded_data_length)))
        if int(entry.get("status_code") or 0) == 0:
            entry["failed"] = True
            entry["failure"] = "http_status_missing"
        self._completed.append(entry)

    def _loading_failed(self, event: Mapping[str, object]) -> None:
        request_id = str(event.get("requestId") or "").strip()
        entry = self._entries.pop(request_id, None)
        if entry is None:
            return
        timestamp = event.get("timestamp")
        if type(timestamp) in {int, float}:
            entry["end_timestamp"] = float(timestamp)
        entry["failed"] = True
        entry["failure"] = str(event.get("errorText") or "request_failed")[:160]
        self._completed.append(entry)

    def finish(self) -> dict[str, object]:
        completed = [
            *((entry, False) for entry in self._completed),
            *((entry, True) for entry in self._entries.values()),
        ]
        waterfall: list[dict[str, object]] = []
        for entry, incomplete in completed:
            started = float(entry.get("start_timestamp") or 0.0)
            ended = float(entry.get("end_timestamp") or started)
            waterfall.append(
                {
                    "url": str(entry.get("url") or ""),
                    "resource_type": str(entry.get("resource_type") or "Other"),
                    "status_code": int(entry.get("status_code") or 0),
                    "duration_ms": max(0, round((ended - started) * 1000)),
                    "transferred_bytes": int(entry.get("transferred_bytes") or 0),
                    "cache_source": str(entry.get("cache_source") or "network"),
                    "failed": entry.get("failed") is True,
                    "incomplete": incomplete,
                }
            )
        waterfall.sort(
            key=lambda row: (
                -int(row["duration_ms"]),
                -int(row["transferred_bytes"]),
                str(row["url"]),
            )
        )
        failed = [row for row in waterfall if row["failed"] is True]
        return {
            "request_count": self._request_count,
            "transferred_bytes": sum(
                int(row["transferred_bytes"]) for row in waterfall
            ),
            "failed_request_count": len(failed),
            "failed_requests": failed[: self._slowest_resource_limit],
            "incomplete_request_count": len(
                [row for row in waterfall if row["incomplete"] is True]
            ),
            "cache_hit_count": len(
                [row for row in waterfall if row["cache_source"] != "network"]
            ),
            "subresource_cache_hit_count": len(
                [
                    row
                    for row in waterfall
                    if row["resource_type"] != "Document"
                    and urlsplit(str(row["url"])).scheme in {"http", "https"}
                    and row["cache_source"] in {"browser_cache", "disk_cache"}
                    and row["failed"] is False
                    and row["incomplete"] is False
                ]
            ),
            "slowest_resources": waterfall[: self._slowest_resource_limit],
        }


def _navigation_timing(page: Any) -> dict[str, int]:
    raw = page.evaluate(
        """() => {
          const row = performance.getEntriesByType('navigation')[0];
          if (!row) return {};
          return {
            responseStartMs: Math.max(0, Math.round(row.responseStart)),
            responseEndMs: Math.max(0, Math.round(row.responseEnd)),
            domContentLoadedMs: Math.max(0, Math.round(row.domContentLoadedEventEnd)),
            loadEventMs: Math.max(0, Math.round(row.loadEventEnd)),
            transferSize: Math.max(0, Math.round(row.transferSize || 0)),
            encodedBodySize: Math.max(0, Math.round(row.encodedBodySize || 0)),
            decodedBodySize: Math.max(0, Math.round(row.decodedBodySize || 0))
          };
        }"""
    )
    if not isinstance(raw, Mapping):
        return {}
    keys = (
        "responseStartMs",
        "responseEndMs",
        "domContentLoadedMs",
        "loadEventMs",
        "transferSize",
        "encodedBodySize",
        "decodedBodySize",
    )
    return {
        key: int(raw[key])
        for key in keys
        if type(raw.get(key)) is int and int(raw[key]) >= 0
    }


def _document_response_headers(response: Any) -> dict[str, str]:
    raw_headers: object = {}
    if response is not None:
        try:
            all_headers = getattr(response, "all_headers", None)
            raw_headers = (
                all_headers()
                if callable(all_headers)
                else getattr(response, "headers", {})
            )
        except Exception:
            raw_headers = {}
    return {
        key.lower(): value
        for key, value in raw_headers.items()
        if type(raw_headers) is dict
        and type(key) is str
        and type(value) is str
    } if type(raw_headers) is dict else {}


def _document_response_release_identity(response: Any) -> dict[str, str]:
    """Extract only a bounded identity envelope from the main response.

    Raw response-header values are never copied to the portable receipt unless
    they satisfy the exact public release-identity grammar.
    """

    headers = _document_response_headers(response)
    raw_commit_sha = headers.get(
        DOCUMENT_RELEASE_IDENTITY_HEADERS["commit_sha"], ""
    )
    raw_image_digest = headers.get(
        DOCUMENT_RELEASE_IDENTITY_HEADERS["image_digest"], ""
    )
    raw_deployment_id = headers.get(
        DOCUMENT_RELEASE_IDENTITY_HEADERS["deployment_id"], ""
    )
    raw_manifest_status = headers.get(
        DOCUMENT_RELEASE_IDENTITY_HEADERS["manifest_status"], ""
    )
    raw_manifest_sha256 = headers.get(
        DOCUMENT_RELEASE_IDENTITY_HEADERS["manifest_sha256"], ""
    )
    raw_replica_id = headers.get(
        DOCUMENT_RELEASE_IDENTITY_HEADERS["replica_id"], ""
    )
    commit_sha = (
        raw_commit_sha
        if raw_commit_sha == raw_commit_sha.strip() == raw_commit_sha.lower()
        else ""
    )
    image_digest = (
        raw_image_digest
        if raw_image_digest
        == raw_image_digest.strip()
        == raw_image_digest.lower()
        else ""
    )
    deployment_id = (
        raw_deployment_id
        if raw_deployment_id == raw_deployment_id.strip()
        else ""
    )
    manifest_status = (
        raw_manifest_status
        if raw_manifest_status == raw_manifest_status.strip()
        else ""
    )
    manifest_sha256 = (
        raw_manifest_sha256
        if raw_manifest_sha256
        == raw_manifest_sha256.strip()
        == raw_manifest_sha256.lower()
        else ""
    )
    replica_id = (
        raw_replica_id if raw_replica_id == raw_replica_id.strip() else ""
    )
    return {
        "commit_sha": (
            commit_sha if RELEASE_COMMIT_SHA_RE.fullmatch(commit_sha) else ""
        ),
        "image_digest": (
            image_digest
            if RELEASE_IMAGE_DIGEST_RE.fullmatch(image_digest)
            else ""
        ),
        "deployment_id": (
            deployment_id
            if RELEASE_DEPLOYMENT_ID_RE.fullmatch(deployment_id)
            else ""
        ),
        "manifest_status": (
            manifest_status
            if manifest_status in {"complete", "invalid", "mismatch"}
            else ""
        ),
        "manifest_sha256": (
            manifest_sha256
            if re.fullmatch(r"[0-9a-f]{64}", manifest_sha256)
            else ""
        ),
        "replica_id": (
            replica_id if REPLICA_ID_RE.fullmatch(replica_id) else ""
        ),
    }


def _document_response_authentication_binding(
    response: Any,
    *,
    expected_nonce_sha256: str,
) -> dict[str, str]:
    headers = _document_response_headers(response)
    raw_cache_control = headers.get("cache-control", "")
    raw_acknowledgment = headers.get(
        RELEASE_PROBE_NONCE_SHA256_RESPONSE_HEADER,
        "",
    )
    cache_control = (
        raw_cache_control
        if raw_cache_control == raw_cache_control.strip()
        and len(raw_cache_control) <= 128
        else ""
    )
    acknowledged_nonce_sha256 = (
        raw_acknowledgment
        if raw_acknowledgment
        == raw_acknowledgment.strip()
        == raw_acknowledgment.lower()
        and re.fullmatch(r"[0-9a-f]{64}", raw_acknowledgment)
        else ""
    )
    return {
        "cache_control": cache_control,
        "expected_nonce_sha256": expected_nonce_sha256,
        "acknowledged_nonce_sha256": acknowledged_nonce_sha256,
    }


def _document_release_identity_is_exact(
    identity: Mapping[str, object],
    *,
    expected_release_identity: Mapping[str, str],
) -> bool:
    return (
        type(identity) is dict
        and tuple(identity) == DOCUMENT_RELEASE_IDENTITY_FIELDS
        and identity.get("commit_sha") == expected_release_identity.get("commit_sha")
        and identity.get("image_digest")
        == expected_release_identity.get("image_digest")
        and identity.get("deployment_id")
        == expected_release_identity.get("deployment_id")
        and identity.get("manifest_status") == "complete"
        and type(identity.get("manifest_sha256")) is str
        and identity.get("manifest_sha256")
        == expected_release_identity.get("manifest_sha256")
        and type(identity.get("replica_id")) is str
        and REPLICA_ID_RE.fullmatch(identity["replica_id"]) is not None
    )


def _browser_threshold_checks(
    *,
    phase: str,
    duration_ms: int,
    metrics: Mapping[str, object],
    profile: ConstrainedClientProfile,
) -> list[dict[str, object]]:
    duration_budget = (
        profile.cold_navigation_budget_ms
        if phase == "cold"
        else profile.warm_navigation_budget_ms
    )
    checks = [
        {
            "name": f"{phase}_navigation_under_budget",
            "ok": duration_ms <= duration_budget,
            "observed_ms": duration_ms,
            "budget_ms": duration_budget,
        },
        {
            "name": f"{phase}_request_observed",
            "ok": int(metrics.get("request_count") or 0) > 0,
            "observed": int(metrics.get("request_count") or 0),
        },
        {
            "name": f"{phase}_request_count_under_budget",
            "ok": int(metrics.get("request_count") or 0) <= profile.max_request_count,
            "observed": int(metrics.get("request_count") or 0),
            "maximum": profile.max_request_count,
        },
        {
            "name": f"{phase}_transferred_bytes_under_budget",
            "ok": int(metrics.get("transferred_bytes") or 0)
            <= profile.max_transferred_bytes,
            "observed": int(metrics.get("transferred_bytes") or 0),
            "maximum": profile.max_transferred_bytes,
        },
        {
            "name": f"{phase}_failed_requests_under_budget",
            "ok": int(metrics.get("failed_request_count") or 0)
            <= profile.max_failed_requests,
            "observed": int(metrics.get("failed_request_count") or 0),
            "maximum": profile.max_failed_requests,
        },
        {
            "name": f"{phase}_requests_completed",
            "ok": int(metrics.get("incomplete_request_count") or 0) == 0,
            "observed_incomplete": int(
                metrics.get("incomplete_request_count") or 0
            ),
        },
    ]
    if phase == "cold":
        checks.append(
            {
                "name": "cold_transferred_bytes_observed",
                "ok": int(metrics.get("transferred_bytes") or 0) > 0,
                "observed": int(metrics.get("transferred_bytes") or 0),
            }
        )
    return checks


def _measure_browser_navigation(
    page: Any,
    capture: _BrowserWaterfallCapture,
    *,
    url: str,
    phase: str,
    cache_state: str,
    profile: ConstrainedClientProfile,
    expected_release_identity: Mapping[str, str],
    signed_navigation_nonce_hashes: list[str] | None = None,
) -> dict[str, object]:
    capture.begin(phase)
    nonce_hash_count_before = len(signed_navigation_nonce_hashes or ())
    started = time.perf_counter()
    response = page.goto(
        url,
        wait_until="load",
        timeout=profile.navigation_timeout_ms,
    )
    page.wait_for_timeout(250)
    duration_ms = max(0, round((time.perf_counter() - started) * 1000))
    metrics = capture.finish()
    status_code = int(getattr(response, "status", 0) or 0)
    raw_final_url = str(getattr(page, "url", "") or "")
    final_target_observed = _observed_url_is_exact_target(
        raw_final_url,
        expected_target=url,
    )
    final_url = (
        _sanitized_resource_url(raw_final_url)
        if final_target_observed
        else "invalid-or-non-target-url"
    )
    expected_final_url = _sanitized_resource_url(url)
    document_release_identity = _document_response_release_identity(response)
    expected_nonce_sha256 = ""
    if signed_navigation_nonce_hashes is not None and len(
        signed_navigation_nonce_hashes
    ) == nonce_hash_count_before + 1:
        expected_nonce_sha256 = signed_navigation_nonce_hashes[-1]
    document_authentication_binding = _document_response_authentication_binding(
        response,
        expected_nonce_sha256=expected_nonce_sha256,
    )
    checks = _browser_threshold_checks(
        phase=phase,
        duration_ms=duration_ms,
        metrics=metrics,
        profile=profile,
    )
    checks.append(
        {
            "name": f"{phase}_navigation_status_ok",
            "ok": 200 <= status_code < 300,
            "status_code": status_code,
        }
    )
    if signed_navigation_nonce_hashes is not None:
        checks.extend(
            (
                {
                    "name": f"{phase}_document_cache_control_no_store",
                    "ok": document_authentication_binding["cache_control"]
                    == "no-store",
                },
                {
                    "name": f"{phase}_server_verified_probe_nonce_acknowledged",
                    "ok": bool(expected_nonce_sha256)
                    and document_authentication_binding[
                        "acknowledged_nonce_sha256"
                    ]
                    == expected_nonce_sha256,
                },
            )
        )
    checks.append(
        {
            "name": f"{phase}_final_target_url_observed",
            "ok": final_target_observed and final_url == expected_final_url,
        }
    )
    checks.append(
        {
            "name": f"{phase}_document_release_identity_exact",
            "ok": _document_release_identity_is_exact(
                document_release_identity,
                expected_release_identity=expected_release_identity,
            ),
        }
    )
    return {
        "phase": phase,
        "cache_state": cache_state,
        "duration_ms": duration_ms,
        "status_code": status_code,
        "final_url": final_url,
        "document_release_identity": document_release_identity,
        "document_authentication_binding": document_authentication_binding,
        **metrics,
        "navigation_timing": _navigation_timing(page),
        "checks": checks,
        "ok": all(check["ok"] is True for check in checks),
    }


def _browser_identity(
    playwright: Any,
    browser: Any,
    *,
    engine: str,
    executable_path: Path | None = None,
) -> dict[str, object]:
    executable = (
        str(executable_path)
        if executable_path is not None
        else playwright_engine_executable(playwright, engine=engine)
    )
    executable_path = Path(executable).expanduser().resolve() if executable else None
    if executable_path is None or not executable_path.is_file():
        raise RuntimeError(f"browser_executable_identity_missing:{engine}")
    try:
        playwright_version = importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("playwright_package_identity_missing") from exc
    browser_version = str(getattr(browser, "version", "") or "").strip()
    if not browser_version:
        raise RuntimeError(f"browser_version_identity_missing:{engine}")
    return {
        "engine": engine,
        "browser_version": browser_version,
        "playwright_version": playwright_version,
        "executable_path": str(executable_path),
        "executable_sha256": _sha256_file(executable_path),
        "executable_bytes": executable_path.stat().st_size,
    }


def collect_constrained_client_browser_evidence(
    *,
    target_url: str,
    authentication_bootstrap_url: str,
    release_probe_secret: str = "",
    profile: ConstrainedClientProfile,
    expected_release_identity: Mapping[str, str] | None = None,
    expected_chromium_executable_path: str = "",
    expected_chromium_executable_sha256: str = "",
    browser_engine: str = "chromium",
    sync_playwright_factory: Callable[[], Any] | None = None,
) -> dict[str, object]:
    validated_profile = validate_constrained_client_profile(profile)
    engine = normalize_playwright_engine(browser_engine)
    normalized_expected_identity, expected_identity_errors = (
        _expected_release_identity(
            commit_sha=(expected_release_identity or {}).get("commit_sha"),
            image_digest=(expected_release_identity or {}).get("image_digest"),
            deployment_id=(expected_release_identity or {}).get("deployment_id"),
            manifest_sha256=(expected_release_identity or {}).get(
                "manifest_sha256"
            ),
        )
    )
    if engine == "chromium" and expected_identity_errors:
        raise PerformanceConfigError(
            "expected_release_identity_required_for_chromium_measurement"
        )
    expected_chromium_identity: dict[str, object] = {}
    if engine == "chromium":
        expected_chromium_identity = _verified_expected_chromium_executable(
            executable_path=expected_chromium_executable_path,
            executable_sha256=expected_chromium_executable_sha256,
        )
    target = _validated_browser_url(target_url, field="target_url")
    bootstrap_raw = str(authentication_bootstrap_url or "").strip()
    probe_secret = str(release_probe_secret or "").strip()
    if bool(bootstrap_raw) == bool(probe_secret):
        raise PerformanceConfigError(
            "exactly_one_browser_authentication_method_must_be_configured"
        )
    bootstrap = ""
    if bootstrap_raw:
        bootstrap = _validated_browser_url(
            bootstrap_raw,
            field="authentication_bootstrap_url",
        )
        if _url_origin(target) != _url_origin(bootstrap):
            raise PerformanceConfigError(
                "authentication_bootstrap_url_must_match_target_origin"
            )
    elif len(probe_secret) < 32:
        raise PerformanceConfigError("release_probe_secret_too_short")
    if probe_secret and urlsplit(target).path != "/app/search":
        raise PerformanceConfigError("release_probe_performance_target_must_be_app_search")
    if sync_playwright_factory is None:
        try:
            from playwright.sync_api import sync_playwright as sync_playwright_factory
        except Exception as exc:
            return {
                "status": "fail",
                "browser_engine": engine,
                "error": f"playwright_unavailable:{type(exc).__name__}",
                "limitations": ["Playwright is unavailable in this runtime."],
                "field_core_web_vitals_claimed": False,
                "physical_device_claimed": False,
            }

    browser: Any | None = None
    context: Any | None = None
    try:
        with sync_playwright_factory() as playwright:  # type: ignore[misc]
            browser = playwright_engine_launch_browser(
                playwright,
                engine=engine,
                executable_path=(
                    str(expected_chromium_identity["executable_path"])
                    if engine == "chromium"
                    else None
                ),
            )
            identity = _browser_identity(
                playwright,
                browser,
                engine=engine,
                executable_path=(
                    Path(str(expected_chromium_identity["executable_path"]))
                    if engine == "chromium"
                    else None
                ),
            )
            postlaunch_chromium_identity: dict[str, object] = {}
            if engine == "chromium":
                try:
                    postlaunch_chromium_identity = (
                        _verified_expected_chromium_executable(
                            executable_path=expected_chromium_identity.get(
                                "executable_path"
                            ),
                            executable_sha256=expected_chromium_identity.get(
                                "executable_sha256"
                            ),
                        )
                    )
                except PerformanceConfigError as exc:
                    raise PerformanceConfigError(
                        "expected_chromium_executable_changed"
                    ) from exc
            launch_binding = {
                "mechanism": "playwright_explicit_executable_path",
                "executable_path": expected_chromium_identity.get(
                    "executable_path", ""
                ),
                "executable_sha256": expected_chromium_identity.get(
                    "executable_sha256", ""
                ),
                "prelaunch_bytes": expected_chromium_identity.get(
                    "executable_bytes", 0
                ),
                "postlaunch_identity_match": (
                    postlaunch_chromium_identity == expected_chromium_identity
                    and identity.get("executable_path")
                    == expected_chromium_identity.get("executable_path")
                    and identity.get("executable_sha256")
                    == expected_chromium_identity.get("executable_sha256")
                    and identity.get("executable_bytes")
                    == expected_chromium_identity.get("executable_bytes")
                ),
            }
            support = {
                "cpu_throttling": {
                    "requested_rate": validated_profile.cpu_slowdown_rate,
                    "applied": engine == "chromium",
                    "mechanism": (
                        "chromium_cdp_Emulation.setCPUThrottlingRate"
                        if engine == "chromium"
                        else "unsupported_by_selected_engine"
                    ),
                },
                "network_throttling": {
                    "latency_ms": validated_profile.network_latency_ms,
                    "download_kbps": validated_profile.download_kbps,
                    "upload_kbps": validated_profile.upload_kbps,
                    "applied": engine == "chromium",
                    "mechanism": (
                        "chromium_cdp_Network.emulateNetworkConditions"
                        if engine == "chromium"
                        else "unsupported_by_selected_engine"
                    ),
                },
                "viewport_emulation": {"applied": True, "mechanism": "playwright_context"},
            }
            if engine != "chromium":
                return {
                    "status": "unsupported",
                    "browser_engine": engine,
                    "identity": identity,
                    "profile_support": support,
                    "measurements": {},
                    "limitations": [
                        f"{engine} has no supported Playwright control for equivalent CPU slowdown.",
                        f"{engine} has no supported Playwright control for equivalent bounded network throughput and latency.",
                        "No cross-engine constrained-performance equivalence is claimed.",
                    ],
                    "field_core_web_vitals_claimed": False,
                    "physical_device_claimed": False,
                }

            context = browser.new_context(
                viewport={
                    "width": validated_profile.viewport_width,
                    "height": validated_profile.viewport_height,
                },
                device_scale_factor=validated_profile.device_scale_factor,
                is_mobile=True,
                has_touch=True,
                service_workers="block",
            )
            page = context.new_page()
            page.set_default_navigation_timeout(validated_profile.navigation_timeout_ms)
            session = context.new_cdp_session(page)
            signed_nonces: set[str] = set()
            signed_nonce_hashes: list[str] = []
            signed_navigation_count = 0
            signing_interception_errors: list[str] = []
            if probe_secret:
                from scripts.propertyquarry_live_probe_auth import (
                    live_probe_request_headers,
                )

                def record_interception_error(reason: str) -> None:
                    if (
                        len(signing_interception_errors) < 8
                        and reason not in signing_interception_errors
                    ):
                        signing_interception_errors.append(reason[:160])

                def authorize_navigation(event: Mapping[str, object]) -> None:
                    nonlocal signed_navigation_count
                    request_id = event.get("requestId")
                    continuation: dict[str, object] = {"requestId": request_id}
                    try:
                        request = event.get("request")
                        if type(request_id) is not str or not request_id:
                            raise ValueError("request_id_invalid")
                        if not isinstance(request, Mapping):
                            raise ValueError("request_invalid")
                        request_url = request.get("url")
                        request_method = request.get("method")
                        request_headers = request.get("headers")
                        if (
                            type(request_url) is not str
                            or type(request_method) is not str
                            or type(request_headers) is not dict
                            or any(
                                type(name) is not str or type(value) is not str
                                for name, value in request_headers.items()
                            )
                        ):
                            raise ValueError("request_envelope_invalid")
                        request_parts = urlsplit(request_url)
                        is_target_document = (
                            event.get("resourceType") == "Document"
                            and request_method == "GET"
                            and _observed_url_is_exact_target(
                                request_url,
                                expected_target=target,
                            )
                            and request_parts.path == "/app/search"
                            and not request_parts.query
                            and not request_parts.fragment
                        )
                        if is_target_document:
                            signed = live_probe_request_headers(
                                url=request_url,
                                authorized_origin=(
                                    f"{request_parts.scheme}://{request_parts.netloc}"
                                ),
                                headers=dict(request_headers),
                                release_probe_secret=probe_secret,
                                method="GET",
                            )
                            nonce = str(
                                signed.get(
                                    "x-propertyquarry-release-probe-nonce"
                                )
                                or ""
                            ).strip()
                            if not nonce or nonce in signed_nonces:
                                raise ValueError("navigation_nonce_invalid")
                            signed_nonces.add(nonce)
                            signed_nonce_hashes.append(
                                hashlib.sha256(
                                    f"propertyquarry-release-probe\0{nonce}".encode(
                                        "utf-8"
                                    )
                                ).hexdigest()
                            )
                            signed_navigation_count += 1
                            continuation["headers"] = [
                                {"name": name, "value": value}
                                for name, value in sorted(signed.items())
                            ]
                    except BaseException as exc:
                        record_interception_error(
                            f"request_signing_failed:{type(exc).__name__}"
                        )
                    try:
                        session.send("Fetch.continueRequest", continuation)
                    except BaseException as exc:
                        record_interception_error(
                            f"request_continue_failed:{type(exc).__name__}"
                        )

                session.on("Fetch.requestPaused", authorize_navigation)
                session.send(
                    "Fetch.enable",
                    {
                        "patterns": [
                            {
                                "urlPattern": "*",
                                "resourceType": "Document",
                                "requestStage": "Request",
                            }
                        ],
                        "handleAuthRequests": False,
                    },
                )
            else:
                bootstrap_response = page.goto(
                    bootstrap,
                    wait_until="load",
                    timeout=validated_profile.navigation_timeout_ms,
                )
                bootstrap_status = int(
                    getattr(bootstrap_response, "status", 0) or 0
                )
                if not 200 <= bootstrap_status < 400:
                    raise RuntimeError(
                        f"authentication_bootstrap_failed_status:{bootstrap_status}"
                    )
                if _url_origin(str(getattr(page, "url", "") or "")) != _url_origin(
                    target
                ):
                    raise RuntimeError("authentication_bootstrap_left_authorized_origin")
                cookies = context.cookies([target])
                if not isinstance(cookies, list) or not cookies:
                    raise RuntimeError("authentication_bootstrap_cookie_missing")

            session.send("Network.enable")
            session.send(
                "Emulation.setCPUThrottlingRate",
                {"rate": validated_profile.cpu_slowdown_rate},
            )
            session.send(
                "Network.emulateNetworkConditions",
                {
                    "offline": False,
                    "latency": validated_profile.network_latency_ms,
                    "downloadThroughput": round(
                        validated_profile.download_kbps * 1000 / 8
                    ),
                    "uploadThroughput": round(
                        validated_profile.upload_kbps * 1000 / 8
                    ),
                    "connectionType": "cellular3g",
                },
            )
            session.send("Network.setCacheDisabled", {"cacheDisabled": False})
            session.send("Network.clearBrowserCache")
            capture = _BrowserWaterfallCapture(
                session,
                slowest_resource_limit=validated_profile.slowest_resource_limit,
            )
            cold = _measure_browser_navigation(
                page,
                capture,
                url=target,
                phase="cold",
                cache_state="cleared_before_navigation",
                profile=validated_profile,
                expected_release_identity=normalized_expected_identity,
                signed_navigation_nonce_hashes=(
                    signed_nonce_hashes if probe_secret else None
                ),
            )
            authenticated_surface_observed = page.evaluate(
                "() => Boolean(document.querySelector('[data-property-app-shell], [data-pq-greenfield-shell]'))"
            ) is True
            cold["checks"].append(
                {
                    "name": "cold_authenticated_app_surface_observed",
                    "ok": authenticated_surface_observed,
                }
            )
            if probe_secret:
                cold["checks"].append(
                    {
                        "name": "cold_cdp_document_signing_interception_ok",
                        "ok": not signing_interception_errors,
                    }
                )
            cold["ok"] = all(
                check["ok"] is True for check in cold["checks"]
            )
            warm = _measure_browser_navigation(
                page,
                capture,
                url=target,
                phase="warm",
                cache_state="same_context_repeat_cache_observed",
                profile=validated_profile,
                expected_release_identity=normalized_expected_identity,
                signed_navigation_nonce_hashes=(
                    signed_nonce_hashes if probe_secret else None
                ),
            )
            warm["checks"].append(
                {
                    "name": "warm_authenticated_app_surface_observed",
                    "ok": page.evaluate(
                        "() => Boolean(document.querySelector('[data-property-app-shell], [data-pq-greenfield-shell]'))"
                    )
                    is True,
                }
            )
            warm_http_cache_reuse_observed = (
                int(warm.get("subresource_cache_hit_count") or 0) >= 1
                and int(warm.get("transferred_bytes") or 0)
                < int(cold.get("transferred_bytes") or 0)
            )
            warm["checks"].append(
                {
                    "name": "warm_http_cache_reuse_observed",
                    "ok": warm_http_cache_reuse_observed,
                }
            )
            if probe_secret:
                warm["checks"].extend(
                    (
                        {
                            "name": "warm_signed_release_probe_nonces_unique",
                            "ok": signed_navigation_count == 2
                            and len(signed_nonces) == 2,
                        },
                        {
                            "name": "warm_cdp_document_signing_interception_ok",
                            "ok": not signing_interception_errors,
                        },
                    )
                )
            warm["ok"] = all(
                check["ok"] is True for check in warm["checks"]
            )
            authentication = (
                {
                    "method": "signed_release_probe_per_navigation",
                    "navigation_signing_mechanism": (
                        "chromium_cdp_Fetch.requestPaused_document_only"
                    ),
                    "playwright_routing_used": False,
                    "subresource_http_cache_preserved": (
                        warm_http_cache_reuse_observed
                    ),
                    "signed_navigation_count": signed_navigation_count,
                    "distinct_nonce_count": len(signed_nonces),
                    "target_surface_observed": authenticated_surface_observed,
                    "release_probe_secret_persisted": False,
                }
                if probe_secret
                else {
                    "method": "workspace_access_bootstrap_cookie",
                    "cookie_observed": True,
                    "target_surface_observed": authenticated_surface_observed,
                }
            )
            return {
                "status": (
                    "pass"
                    if cold["ok"]
                    and warm["ok"]
                    and launch_binding["postlaunch_identity_match"] is True
                    else "fail"
                ),
                "browser_engine": engine,
                "identity": identity,
                "launch_binding": launch_binding,
                "profile_support": support,
                "authentication": authentication,
                "measurements": {"cold": cold, "warm": warm},
                "cold_to_warm": {
                    "duration_delta_ms": int(cold["duration_ms"])
                    - int(warm["duration_ms"]),
                    "request_count_delta": int(cold["request_count"])
                    - int(warm["request_count"]),
                    "transferred_bytes_delta": int(cold["transferred_bytes"])
                    - int(warm["transferred_bytes"]),
                },
                "limitations": [
                    "Lab navigation and resource timing only; no field Core Web Vitals are claimed.",
                    "Emulated viewport, CPU, and network controls are not physical-device evidence.",
                ],
                "field_core_web_vitals_claimed": False,
                "physical_device_claimed": False,
            }
    except BaseException as exc:
        return {
            "status": "fail",
            "browser_engine": engine,
            "error": _safe_browser_error(exc),
            "limitations": [
                "The constrained browser profile did not complete; no performance claim is authorized."
            ],
            "field_core_web_vitals_claimed": False,
            "physical_device_claimed": False,
        }
    finally:
        if context is not None:
            try:
                context.close()
            except BaseException:
                pass
        if browser is not None:
            try:
                browser.close()
            except BaseException:
                pass


def _passing_engine_receipt_errors(
    row: Mapping[str, object],
    *,
    expected_engine: str,
    expected_target_url: str,
    expected_release_identity: Mapping[str, str],
    expected_chromium_executable_path: str,
    expected_chromium_executable_sha256: str,
    profile: ConstrainedClientProfile,
) -> list[str]:
    errors: list[str] = []
    if type(row) is not dict or tuple(row) != PASSING_ENGINE_RECEIPT_FIELDS:
        return ["passing_engine_receipt_fields_invalid"]
    if row.get("status") != "pass" or row.get("browser_engine") != expected_engine:
        errors.append("passing_engine_status_or_engine_invalid")
    if expected_engine != "chromium":
        errors.append("passing_constrained_profile_requires_chromium")

    identity = row.get("identity")
    if type(identity) is not dict or tuple(identity) != BROWSER_IDENTITY_FIELDS:
        errors.append("browser_identity_fields_invalid")
    else:
        executable_path = Path(str(identity.get("executable_path") or ""))
        executable_sha256 = str(identity.get("executable_sha256") or "")
        if identity.get("engine") != expected_engine:
            errors.append("browser_identity_engine_mismatch")
        if not str(identity.get("browser_version") or "").strip():
            errors.append("browser_version_missing")
        if not str(identity.get("playwright_version") or "").strip():
            errors.append("playwright_version_missing")
        if not executable_path.is_absolute() or not executable_path.is_file():
            errors.append("browser_executable_path_invalid")
        if re.fullmatch(r"[0-9a-f]{64}", executable_sha256) is None:
            errors.append("browser_executable_sha256_invalid")
        if type(identity.get("executable_bytes")) is not int or int(
            identity.get("executable_bytes") or 0
        ) <= 0:
            errors.append("browser_executable_bytes_invalid")

    identity_mapping = identity if isinstance(identity, Mapping) else {}
    launch_binding = row.get("launch_binding")
    if (
        type(launch_binding) is not dict
        or tuple(launch_binding) != CHROMIUM_LAUNCH_BINDING_FIELDS
        or launch_binding.get("mechanism")
        != "playwright_explicit_executable_path"
        or launch_binding.get("executable_path")
        != expected_chromium_executable_path
        or launch_binding.get("executable_sha256")
        != expected_chromium_executable_sha256
        or type(launch_binding.get("prelaunch_bytes")) is not int
        or launch_binding.get("prelaunch_bytes")
        != identity_mapping.get("executable_bytes")
        or launch_binding.get("postlaunch_identity_match") is not True
        or launch_binding.get("executable_path")
        != identity_mapping.get("executable_path")
        or launch_binding.get("executable_sha256")
        != identity_mapping.get("executable_sha256")
    ):
        errors.append("chromium_launch_binding_invalid")

    support = row.get("profile_support")
    if type(support) is not dict or tuple(support) != (
        "cpu_throttling",
        "network_throttling",
        "viewport_emulation",
    ):
        errors.append("profile_support_fields_invalid")
    else:
        cpu = support.get("cpu_throttling")
        network = support.get("network_throttling")
        viewport = support.get("viewport_emulation")
        if not isinstance(cpu, Mapping) or (
            cpu.get("requested_rate") != profile.cpu_slowdown_rate
            or cpu.get("applied") is not True
            or cpu.get("mechanism")
            != "chromium_cdp_Emulation.setCPUThrottlingRate"
        ):
            errors.append("cpu_throttling_not_exactly_applied")
        if not isinstance(network, Mapping) or (
            network.get("latency_ms") != profile.network_latency_ms
            or network.get("download_kbps") != profile.download_kbps
            or network.get("upload_kbps") != profile.upload_kbps
            or network.get("applied") is not True
            or network.get("mechanism")
            != "chromium_cdp_Network.emulateNetworkConditions"
        ):
            errors.append("network_throttling_not_exactly_applied")
        if not isinstance(viewport, Mapping) or (
            viewport.get("applied") is not True
            or viewport.get("mechanism") != "playwright_context"
        ):
            errors.append("viewport_emulation_not_applied")

    authentication = row.get("authentication")
    signed_probe_authentication = False
    if type(authentication) is not dict:
        errors.append("authentication_evidence_fields_invalid")
    elif authentication.get("method") == "signed_release_probe_per_navigation":
        signed_probe_authentication = True
        if tuple(authentication) != SIGNED_RELEASE_PROBE_AUTHENTICATION_FIELDS:
            errors.append("authentication_evidence_fields_invalid")
        elif (
            authentication.get("navigation_signing_mechanism")
            != "chromium_cdp_Fetch.requestPaused_document_only"
            or authentication.get("playwright_routing_used") is not False
            or authentication.get("subresource_http_cache_preserved") is not True
            or
            type(authentication.get("signed_navigation_count")) is not int
            or authentication.get("signed_navigation_count") != 2
            or type(authentication.get("distinct_nonce_count")) is not int
            or authentication.get("distinct_nonce_count") != 2
            or authentication.get("target_surface_observed") is not True
            or authentication.get("release_probe_secret_persisted") is not False
        ):
            errors.append("authenticated_target_not_proven")
    elif authentication.get("method") == "workspace_access_bootstrap_cookie":
        if tuple(authentication) != WORKSPACE_BOOTSTRAP_AUTHENTICATION_FIELDS:
            errors.append("authentication_evidence_fields_invalid")
        elif (
            authentication.get("cookie_observed") is not True
            or authentication.get("target_surface_observed") is not True
        ):
            errors.append("authenticated_target_not_proven")
    else:
        errors.append("authenticated_target_not_proven")

    measurements = row.get("measurements")
    if type(measurements) is not dict or tuple(measurements) != ("cold", "warm"):
        errors.append("cold_warm_measurements_fields_invalid")
    else:
        for phase in ("cold", "warm"):
            measurement = measurements.get(phase)
            if type(measurement) is not dict or tuple(measurement) != BROWSER_MEASUREMENT_FIELDS:
                errors.append(f"{phase}_measurement_fields_invalid")
                continue
            expected_cache_state = (
                "cleared_before_navigation"
                if phase == "cold"
                else "same_context_repeat_cache_observed"
            )
            if (
                measurement.get("phase") != phase
                or measurement.get("cache_state") != expected_cache_state
                or measurement.get("ok") is not True
            ):
                errors.append(f"{phase}_measurement_identity_invalid")
            duration_ms = measurement.get("duration_ms")
            status_code = measurement.get("status_code")
            final_url = measurement.get("final_url")
            document_release_identity = measurement.get(
                "document_release_identity"
            )
            document_authentication_binding = measurement.get(
                "document_authentication_binding"
            )
            request_count = measurement.get("request_count")
            transferred_bytes = measurement.get("transferred_bytes")
            failed_request_count = measurement.get("failed_request_count")
            incomplete_request_count = measurement.get(
                "incomplete_request_count"
            )
            cache_hit_count = measurement.get("cache_hit_count")
            subresource_cache_hit_count = measurement.get(
                "subresource_cache_hit_count"
            )
            expected_duration_budget = (
                profile.cold_navigation_budget_ms
                if phase == "cold"
                else profile.warm_navigation_budget_ms
            )
            if type(duration_ms) is not int or not 0 <= duration_ms <= expected_duration_budget:
                errors.append(f"{phase}_duration_invalid")
            if type(status_code) is not int or not 200 <= status_code < 300:
                errors.append(f"{phase}_status_code_invalid")
            if (
                type(final_url) is not str
                or final_url != _sanitized_resource_url(expected_target_url)
            ):
                errors.append(f"{phase}_final_target_url_invalid")
            if not _document_release_identity_is_exact(
                document_release_identity
                if isinstance(document_release_identity, Mapping)
                else {},
                expected_release_identity=expected_release_identity,
            ):
                errors.append(f"{phase}_document_release_identity_invalid")
            if (
                type(document_authentication_binding) is not dict
                or tuple(document_authentication_binding)
                != DOCUMENT_AUTHENTICATION_BINDING_FIELDS
            ):
                errors.append(f"{phase}_document_authentication_binding_invalid")
            elif signed_probe_authentication:
                expected_nonce_sha256 = document_authentication_binding.get(
                    "expected_nonce_sha256"
                )
                acknowledged_nonce_sha256 = document_authentication_binding.get(
                    "acknowledged_nonce_sha256"
                )
                if (
                    document_authentication_binding.get("cache_control")
                    != "no-store"
                    or type(expected_nonce_sha256) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", expected_nonce_sha256)
                    is None
                    or len(set(expected_nonce_sha256)) == 1
                    or acknowledged_nonce_sha256 != expected_nonce_sha256
                ):
                    errors.append(
                        f"{phase}_document_authentication_binding_invalid"
                    )
            elif (
                document_authentication_binding.get("expected_nonce_sha256")
                != ""
                or document_authentication_binding.get(
                    "acknowledged_nonce_sha256"
                )
                != ""
            ):
                errors.append(f"{phase}_document_authentication_binding_invalid")
            if (
                type(request_count) is not int
                or not 1 <= request_count <= profile.max_request_count
            ):
                errors.append(f"{phase}_request_count_invalid")
            if (
                type(transferred_bytes) is not int
                or transferred_bytes < 0
                or transferred_bytes > profile.max_transferred_bytes
                or (phase == "cold" and transferred_bytes == 0)
            ):
                errors.append(f"{phase}_transferred_bytes_invalid")
            if (
                type(failed_request_count) is not int
                or failed_request_count < 0
                or failed_request_count > profile.max_failed_requests
            ):
                errors.append(f"{phase}_failed_request_count_invalid")
            if type(incomplete_request_count) is not int or incomplete_request_count != 0:
                errors.append(f"{phase}_incomplete_request_count_invalid")
            if (
                type(cache_hit_count) is not int
                or cache_hit_count < 0
                or (
                    type(request_count) is int
                    and cache_hit_count > request_count
                )
            ):
                errors.append(f"{phase}_cache_hit_count_invalid")
            if (
                type(subresource_cache_hit_count) is not int
                or subresource_cache_hit_count < 0
                or (
                    type(cache_hit_count) is int
                    and subresource_cache_hit_count > cache_hit_count
                )
                or (phase == "cold" and subresource_cache_hit_count != 0)
                or (phase == "warm" and subresource_cache_hit_count < 1)
            ):
                errors.append(f"{phase}_subresource_cache_hit_count_invalid")
            failed_requests = measurement.get("failed_requests")
            slowest_resources = measurement.get("slowest_resources")
            if not isinstance(failed_requests, list) or len(failed_requests) > profile.slowest_resource_limit:
                errors.append(f"{phase}_failed_requests_summary_invalid")
            if (
                not isinstance(slowest_resources, list)
                or not slowest_resources
                or len(slowest_resources) > profile.slowest_resource_limit
            ):
                errors.append(f"{phase}_slowest_resources_summary_invalid")
            else:
                for resource in slowest_resources:
                    if not isinstance(resource, Mapping):
                        errors.append(f"{phase}_slowest_resource_invalid")
                        break
                    resource_url = str(resource.get("url") or "")
                    if not resource_url or urlsplit(resource_url).query:
                        errors.append(f"{phase}_slowest_resource_url_invalid")
                        break
                    if resource.get("resource_type") not in CDP_RESOURCE_TYPES:
                        errors.append(f"{phase}_slowest_resource_type_invalid")
                        break
                    if (
                        type(resource.get("status_code")) is not int
                        or not 100 <= int(resource.get("status_code") or 0) < 400
                    ):
                        errors.append(f"{phase}_slowest_resource_status_invalid")
                        break
                    if resource.get("cache_source") not in {
                        "network",
                        "disk_cache",
                        "browser_cache",
                    }:
                        errors.append(f"{phase}_slowest_resource_cache_source_invalid")
                        break
                    if (
                        resource.get("failed") is not False
                        or resource.get("incomplete") is not False
                    ):
                        errors.append(f"{phase}_slowest_resource_completion_invalid")
                        break
                    if type(resource.get("duration_ms")) is not int or int(
                        resource.get("duration_ms") or 0
                    ) < 0:
                        errors.append(f"{phase}_slowest_resource_duration_invalid")
                        break
                    if type(resource.get("transferred_bytes")) is not int or int(
                        resource.get("transferred_bytes") or 0
                    ) < 0:
                        errors.append(f"{phase}_slowest_resource_bytes_invalid")
                        break
            navigation_timing = measurement.get("navigation_timing")
            if not isinstance(navigation_timing, Mapping) or any(
                type(value) is not int or value < 0
                for value in navigation_timing.values()
            ):
                errors.append(f"{phase}_navigation_timing_invalid")
            checks = measurement.get("checks")
            if not isinstance(checks, list) or any(
                not isinstance(check, Mapping) or check.get("ok") is not True
                for check in checks
            ):
                errors.append(f"{phase}_checks_invalid")
        if signed_probe_authentication:
            bound_nonce_hashes = [
                acknowledged
                for phase in ("cold", "warm")
                if isinstance(measurements.get(phase), Mapping)
                and isinstance(
                    measurements[phase].get("document_authentication_binding"),
                    Mapping,
                )
                and type(
                    acknowledged := measurements[phase][
                        "document_authentication_binding"
                    ].get("acknowledged_nonce_sha256")
                )
                is str
                and re.fullmatch(r"[0-9a-f]{64}", acknowledged) is not None
            ]
            if len(bound_nonce_hashes) == 2 and (
                bound_nonce_hashes[0] == bound_nonce_hashes[1]
            ):
                errors.append("signed_probe_nonce_bindings_not_distinct")
        if all(
            isinstance(measurements.get(phase), Mapping)
            for phase in ("cold", "warm")
        ):
            cold_transferred = measurements["cold"].get("transferred_bytes")
            warm_transferred = measurements["warm"].get("transferred_bytes")
            warm_subresource_cache_hits = measurements["warm"].get(
                "subresource_cache_hit_count"
            )
            if (
                type(cold_transferred) is not int
                or type(warm_transferred) is not int
                or type(warm_subresource_cache_hits) is not int
                or warm_subresource_cache_hits < 1
                or warm_transferred >= cold_transferred
            ):
                errors.append("warm_http_cache_reuse_not_observed")

    comparison = row.get("cold_to_warm")
    if type(comparison) is not dict or tuple(comparison) != (
        "duration_delta_ms",
        "request_count_delta",
        "transferred_bytes_delta",
    ) or any(type(value) is not int for value in comparison.values()):
        errors.append("cold_to_warm_comparison_invalid")
    limitations = row.get("limitations")
    if not isinstance(limitations, list) or not limitations or any(
        type(item) is not str or not item.strip() for item in limitations
    ):
        errors.append("engine_limitations_invalid")
    if row.get("field_core_web_vitals_claimed") is not False:
        errors.append("field_core_web_vitals_claim_forbidden")
    if row.get("physical_device_claimed") is not False:
        errors.append("physical_device_claim_forbidden")
    return errors


def collect_constrained_client_evidence(
    *,
    target_url: str,
    authentication_bootstrap_url: str,
    release_probe_secret: str = "",
    profile: ConstrainedClientProfile,
    expected_release_identity: Mapping[str, str] | None = None,
    expected_chromium_executable_path: str = "",
    expected_chromium_executable_sha256: str = "",
    browser_engines: Sequence[str] = ("chromium",),
    collector: Callable[..., dict[str, object]] = collect_constrained_client_browser_evidence,
    release_identity_probe: Callable[..., dict[str, object]] = probe_live_release_identity,
) -> dict[str, object]:
    if isinstance(browser_engines, (str, bytes)) or not isinstance(
        browser_engines, Sequence
    ):
        raise PerformanceConfigError("browser_engines_must_be_sequence")
    engines: list[str] = []
    for raw_engine in browser_engines:
        engine = normalize_playwright_engine(raw_engine)
        if engine in engines:
            raise PerformanceConfigError(f"browser_engine_duplicate:{engine}")
        engines.append(engine)
    if not engines:
        raise PerformanceConfigError("browser_engines_must_not_be_empty")
    expected_chromium_identity: dict[str, object] = {}
    if "chromium" in engines:
        expected_chromium_identity = _verified_expected_chromium_executable(
            executable_path=expected_chromium_executable_path,
            executable_sha256=expected_chromium_executable_sha256,
        )
        expected_chromium_executable_path = str(
            expected_chromium_identity["executable_path"]
        )
        expected_chromium_executable_sha256 = str(
            expected_chromium_identity["executable_sha256"]
        )
    normalized_expected_identity, expected_identity_errors = (
        _expected_release_identity(
            commit_sha=(expected_release_identity or {}).get("commit_sha"),
            image_digest=(expected_release_identity or {}).get("image_digest"),
            deployment_id=(expected_release_identity or {}).get("deployment_id"),
            manifest_sha256=(expected_release_identity or {}).get(
                "manifest_sha256"
            ),
        )
    )
    rows: list[dict[str, object]] = []
    for engine in engines:
        try:
            raw_row = collector(
                target_url=target_url,
                authentication_bootstrap_url=authentication_bootstrap_url,
                release_probe_secret=release_probe_secret,
                profile=profile,
                expected_release_identity=(
                    normalized_expected_identity
                    if not expected_identity_errors
                    else None
                ),
                expected_chromium_executable_path=(
                    expected_chromium_executable_path
                    if engine == "chromium"
                    else ""
                ),
                expected_chromium_executable_sha256=(
                    expected_chromium_executable_sha256
                    if engine == "chromium"
                    else ""
                ),
                browser_engine=engine,
            )
        except BaseException as exc:
            raw_row = {
                "status": "fail",
                "browser_engine": engine,
                "error": _safe_browser_error(exc),
                "limitations": ["Browser collector raised before producing evidence."],
            }
        if not isinstance(raw_row, dict):
            raw_row = {
                "status": "fail",
                "browser_engine": engine,
                "error": "browser_collector_receipt_must_be_object",
                "limitations": ["Browser collector returned a malformed receipt."],
            }
        elif raw_row.get("status") == "pass":
            validation_errors = _passing_engine_receipt_errors(
                raw_row,
                expected_engine=engine,
                expected_target_url=target_url,
                expected_release_identity=normalized_expected_identity,
                expected_chromium_executable_path=(
                    expected_chromium_executable_path
                ),
                expected_chromium_executable_sha256=(
                    expected_chromium_executable_sha256
                ),
                profile=profile,
            )
            if validation_errors:
                raw_row = {
                    "status": "fail",
                    "browser_engine": engine,
                    "error": "passing_browser_receipt_validation_failed",
                    "validation_errors": validation_errors[:40],
                    "limitations": [
                        "The browser collector returned an incomplete or inconsistent passing receipt."
                    ],
                }
        elif raw_row.get("browser_engine") != engine:
            raw_row = {
                "status": "fail",
                "browser_engine": engine,
                "error": "browser_collector_engine_mismatch",
                "limitations": ["Browser collector identified a different engine."],
            }
        rows.append(raw_row)
    failed_or_unsupported = [
        row for row in rows if row.get("status") != "pass"
    ]
    if not expected_identity_errors:
        try:
            release_identity = release_identity_probe(
                target_url=target_url,
                expected_release_identity=normalized_expected_identity,
            )
        except BaseException as exc:
            release_identity = {
                "status": "fail",
                "version_url": _sanitized_resource_url(
                    f"{_url_origin(target_url)}/version"
                ),
                "status_code": 0,
                "tls_verified": urlsplit(target_url).scheme == "https",
                "expected": normalized_expected_identity,
                "observed": {},
                "matches_expected": False,
                "error": f"version_probe_failed:{type(exc).__name__}",
                "credential_persisted": False,
            }
    else:
        release_identity = {
            "status": "not_run",
            "version_url": "",
            "status_code": 0,
            "tls_verified": False,
            "expected": {},
            "observed": {},
            "matches_expected": False,
            "error": "expected_release_identity_missing",
            "credential_persisted": False,
        }
    document_version_binding_valid = True
    if release_identity.get("status") == "pass":
        observed_version_identity = release_identity.get("observed")
        version_manifest_sha256 = (
            observed_version_identity.get("manifest_sha256")
            if isinstance(observed_version_identity, Mapping)
            else ""
        )
        document_version_binding_valid = (
            type(version_manifest_sha256) is str
            and re.fullmatch(r"[0-9a-f]{64}", version_manifest_sha256)
            is not None
            and all(
                isinstance(row.get("measurements"), Mapping)
                and all(
                    isinstance(row["measurements"].get(phase), Mapping)
                    and isinstance(
                        row["measurements"][phase].get(
                            "document_release_identity"
                        ),
                        Mapping,
                    )
                    and row["measurements"][phase][
                        "document_release_identity"
                    ].get("manifest_sha256")
                    == version_manifest_sha256
                    for phase in ("cold", "warm")
                )
                for row in rows
                if row.get("status") == "pass"
            )
        )
    return {
        "status": (
            "pass"
            if not failed_or_unsupported and document_version_binding_valid
            else "blocked"
        ),
        "profile": constrained_client_profile_receipt(profile),
        "target": _sanitized_resource_url(target_url),
        "release_identity": release_identity,
        "requested_browser_engines": engines,
        "engine_rows": rows,
        "limitations_by_engine": {
            str(row.get("browser_engine") or "unknown"): list(
                row.get("limitations") or []
            )
            for row in rows
            if row.get("status") != "pass"
        },
        "field_core_web_vitals_claimed": False,
        "physical_device_claimed": False,
    }


def _asset_text(client: TestClient, path: str) -> str:
    try:
        response = client.get(path, headers={"host": "propertyquarry.com", "accept-encoding": "identity"})
    except Exception:
        return ""
    if response.status_code != 200:
        return ""
    return response.text or ""


def _visible_text(body: str) -> str:
    without_hidden = re.sub(
        r"<script.*?</script>|<style.*?</style>|<template.*?</template>",
        " ",
        str(body or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    without_tags = re.sub(r"<[^>]+>", " ", without_hidden)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def _workbench_css_path_for_route(path: str, body: str) -> str:
    match = re.search(r'href="(?P<href>/app/assets/property-workbench\.css[^"]*)"', body)
    if match:
        return str(match.group("href") or "").strip()
    normalized_path = str(path or "").split("?", 1)[0]
    if normalized_path in CONTENT_FIRST_MOBILE_PATHS:
        return "/app/assets/property-workbench.css?surface=static"
    return "/app/assets/property-workbench.css"


def _has_css_min_height_at_least(css_body: str, minimum_px: int = 44) -> bool:
    for value in re.findall(r"min-height\s*:\s*(\d+)px", css_body, flags=re.IGNORECASE):
        try:
            if int(value) >= minimum_px:
                return True
        except ValueError:
            continue
    return False


def _mobile_surface_contract_checks(path: str, body: str, *, css_body: str = "") -> list[dict[str, object]]:
    normalized_path = str(path or "").split("?", 1)[0]
    surface_markup = f'data-pqx-surface="{normalized_path.rsplit("/", 1)[-1]}"'
    if normalized_path == "/sign-in":
        return [
            {
                "name": "mobile_viewport_meta",
                "ok": 'name="viewport"' in body and "width=device-width" in body,
            },
            {
                "name": "public_auth_surface",
                "ok": "data-property-public-page" in body or "PropertyQuarry" in body,
            },
        ]
    if not normalized_path.startswith("/app/"):
        return []
    nav_missing = [label for label in SHARED_TOP_NAV_LABELS if label not in body]
    checks = [
        {
            "name": "mobile_viewport_meta",
            "ok": 'name="viewport"' in body and "width=device-width" in body,
        },
        {
            "name": "shared_top_navigation",
            "ok": "data-property-research-topnav" in body and not nav_missing,
            "detail": ", ".join(nav_missing[:5]),
        },
        {
            "name": "property_app_shell",
            "ok": "data-property-app-shell" in body and "data-pq-greenfield-shell" in body,
        },
    ]
    if normalized_path in CONTENT_FIRST_MOBILE_PATHS:
        checks.extend(
            (
                {
                    "name": "mobile_content_first_surface",
                    "ok": 'data-pqx-mobile-panel="brief"' in body and "pqx-brief-drawer-panel" in body,
                },
                {
                    "name": "mobile_static_switch_suppressed",
                    "ok": ".pqx-shell[data-pqx-surface=\"account\"] .pqx-mobile-switch" in css_body
                    and ".pqx-shell[data-pqx-surface=\"billing\"] .pqx-mobile-switch" in css_body
                    and ".pqx-shell[data-pqx-surface=\"alerts\"] .pqx-mobile-switch" in css_body,
                },
            )
        )
    elif normalized_path in SETTINGS_MOBILE_PATHS:
        checks.append(
            {
                "name": "mobile_settings_surface",
                "ok": (
                    "data-property-research-topnav" in body
                    and (
                        "/app/settings/" in body
                        or "/app/account?settings_view=" in body
                        or surface_markup in body
                    )
                ),
            }
        )
    else:
        checks.extend(
            (
                {
                    "name": "mobile_top_navigation_only",
                    "ok": "data-property-mobile-dock" not in body and "class=\"pq-mobile-nav\"" not in body,
                },
                {
                    "name": "mobile_top_navigation_touch_targets",
                    "ok": (
                        "data-property-research-topnav" in body
                        and _has_css_min_height_at_least(css_body, 44)
                    ),
                },
            )
        )
    return checks


def _allowed_billing_handoff_hosts() -> set[str]:
    urls = [
        "https://billing.propertyquarry.test/",
        "https://billing.propertyquarry.com/",
        str(os.environ.get("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BILLING_URL") or ""),
        str(os.environ.get("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BASE_URL") or ""),
    ]
    urls.extend(
        part.strip()
        for part in str(os.environ.get("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BILLING_FALLBACK_URLS") or "").split(",")
        if part.strip()
    )
    hosts: set[str] = set()
    for raw_url in urls:
        parsed = urlparse(str(raw_url or "").strip())
        if parsed.scheme == "https" and parsed.netloc:
            hosts.add(parsed.netloc.lower())
    return hosts


def _billing_handoff_redirect_ok(*, path: str, status_code: int, location: str) -> tuple[bool, str]:
    if path != "/app/billing" or status_code not in {303, 307}:
        return False, ""
    parsed = urlparse(str(location or "").strip())
    host = parsed.netloc.lower()
    if parsed.scheme != "https" or not host:
        return False, host
    return host in _allowed_billing_handoff_hosts(), host


def _billing_internal_account_fallback_ok(*, path: str, status_code: int, location: str) -> bool:
    if path != "/app/billing" or status_code not in {303, 307}:
        return False
    parsed = urlparse(str(location or "").strip())
    return not parsed.scheme and (parsed.path or "").startswith("/app/account")


def _rybbit_surface_contract_checks(path: str, body: str) -> list[dict[str, object]]:
    normalized_path = str(path or "").split("?", 1)[0]
    if not normalized_path.startswith("/app/"):
        return []
    attr_matches = re.findall(r"(data-rybbit-[^=\s>]+)=([\"'])(.*?)\2", body, flags=re.IGNORECASE | re.DOTALL)
    attrs = [
        (str(name or "").strip().lower(), " ".join(str(value or "").split()).strip())
        for name, _quote, value in attr_matches
    ]
    attr_names = {name for name, _value in attrs}
    event_values = [value for name, value in attrs if name == "data-rybbit-event"]
    serialized_attrs = " ".join(f"{name}={value}" for name, value in attrs).lower()
    forbidden_hits = [token for token in FORBIDDEN_RYBBIT_PAYLOAD_TOKENS if token in serialized_attrs]
    unknown_events = [value for value in event_values if value not in ALLOWED_RYBBIT_APP_EVENTS]
    unknown_attrs = [name for name in sorted(attr_names) if name not in ALLOWED_RYBBIT_ATTRIBUTE_NAMES]
    return [
        {
            "name": "rybbit_no_identify",
            "ok": "rybbit.identify" not in body and "analytics_principal_id" not in body,
        },
        {
            "name": "rybbit_taxonomy_events_only",
            "ok": not unknown_events and 'data-rybbit-event="property_' not in body,
            "detail": ", ".join(unknown_events[:5]),
        },
        {
            "name": "rybbit_allowed_attributes_only",
            "ok": not unknown_attrs,
            "detail": ", ".join(unknown_attrs[:5]),
        },
        {
            "name": "rybbit_no_private_payload",
            "ok": not forbidden_hits,
            "detail": ", ".join(forbidden_hits[:5]),
        },
    ]


def _route_budget_for(path: str, *, route_budget_ms: int) -> int:
    normalized_path = str(path or "").split("?", 1)[0]
    default_budget = int(DEFAULT_ROUTE_BUDGET_MS.get(normalized_path, route_budget_ms))
    return min(default_budget, int(route_budget_ms))


def _search_compressed_max_bytes() -> int:
    raw_value = str(os.environ.get("PROPERTYQUARRY_SEARCH_COMPRESSED_MAX_BYTES") or "").strip()
    if not raw_value:
        return DEFAULT_SEARCH_COMPRESSED_MAX_BYTES
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_SEARCH_COMPRESSED_MAX_BYTES
    return value if value > 0 else DEFAULT_SEARCH_COMPRESSED_MAX_BYTES


def _response_content_length(response: object) -> int:
    headers = getattr(response, "headers", {})
    raw_value = str(headers.get("content-length") or "").strip()
    if raw_value:
        try:
            return int(raw_value)
        except ValueError:
            pass
    return len(getattr(response, "content", b"") or b"")


def _reset_authenticated_performance_smoke_env() -> None:
    for key in list(os.environ.keys()):
        if key in PROVIDER_FREE_ENV_NAMES or key.startswith(PROVIDER_FREE_ENV_PREFIXES):
            os.environ.pop(key, None)


def _scrub_performance_subprocess_environment() -> dict[str, str]:
    original = dict(os.environ)
    retained = {
        key: value
        for key, value in original.items()
        if key in PERFORMANCE_SUBPROCESS_ENV_ALLOWLIST
    }
    retained["PATH"] = "/usr/bin:/bin"
    os.environ.clear()
    os.environ.update(retained)
    return original


def _restore_process_environment(original: Mapping[str, str]) -> None:
    os.environ.clear()
    os.environ.update({str(key): str(value) for key, value in original.items()})


def _with_scrubbed_performance_environment(function: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(function)
    def wrapped(*args: object, **kwargs: object) -> Any:
        original_environment = _scrub_performance_subprocess_environment()
        try:
            return function(*args, **kwargs)
        finally:
            _restore_process_environment(original_environment)

    return wrapped


@contextlib.contextmanager
def _provider_free_landing_view_model_boundaries() -> Iterator[None]:
    from app.api.routes import landing_view_models

    boundary_record = landing_view_models._nominatim_boundary_record
    geocode_point = landing_view_models._forward_geocode_preview_point

    def _offline_boundary_record(_query: str) -> dict[str, object]:
        return {}

    def _offline_geocode_point(_query: str) -> tuple[float, float] | None:
        return None

    landing_view_models._nominatim_boundary_record = _offline_boundary_record
    landing_view_models._forward_geocode_preview_point = _offline_geocode_point
    try:
        yield
    finally:
        landing_view_models._nominatim_boundary_record = boundary_record
        landing_view_models._forward_geocode_preview_point = geocode_point


@contextlib.contextmanager
def _provider_free_public_tour_directory() -> Iterator[None]:
    environment_name = "EA_PUBLIC_TOUR_DIR"
    original_value = os.environ.get(environment_name)
    with tempfile.TemporaryDirectory(prefix="propertyquarry-performance-tours-") as directory:
        Path(directory).chmod(0o700)
        os.environ[environment_name] = directory
        try:
            yield
        finally:
            if original_value is None:
                os.environ.pop(environment_name, None)
            else:
                os.environ[environment_name] = original_value


def _seed_workspace(client: TestClient) -> None:
    response = client.post(
        "/v1/onboarding/start",
        json={
            "workspace_name": "PropertyQuarry Performance Smoke",
            "mode": "personal",
            "workspace_mode": "personal",
            "timezone": "Europe/Vienna",
            "region": "AT",
            "language": "en",
            "selected_channels": ["google"],
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"workspace_seed_failed:{response.status_code}:{response.text[:280]}")


def _property_preferences_payload(*, saved_candidates: list[dict[str, object]] | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "country_code": "AT",
        "region_code": "vienna",
        "language_code": "de",
        "listing_mode": "rent",
        "property_type": "apartment",
        "location_query": PERFORMANCE_SMOKE_LOCATION_QUERY,
        "min_area_m2": PERFORMANCE_SMOKE_MIN_AREA_M2,
        "max_price_eur": PERFORMANCE_SMOKE_MAX_PRICE_EUR,
        "selected_platforms": ["willhaben", "derstandard_at"],
        "active_search_agent_id": "perf-watch-1020",
        "search_agents": [
            {
                "agent_id": "perf-watch-1020",
                "name": "Leopoldstadt rent watch",
                "enabled": True,
                "country_code": "AT",
                "region_code": "vienna",
                "location_query": PERFORMANCE_SMOKE_LOCATION_QUERY,
                "listing_mode": "rent",
                "property_type": "apartment",
                "min_area_m2": PERFORMANCE_SMOKE_MIN_AREA_M2,
                "max_price_eur": PERFORMANCE_SMOKE_MAX_PRICE_EUR,
                "notification_limit": 3,
                "notification_period": "day",
                "preferences_json": {
                    "country_code": "AT",
                    "region_code": "vienna",
                    "location_query": PERFORMANCE_SMOKE_LOCATION_QUERY,
                    "listing_mode": "rent",
                    "property_type": "apartment",
                    "min_area_m2": PERFORMANCE_SMOKE_MIN_AREA_M2,
                    "max_price_eur": PERFORMANCE_SMOKE_MAX_PRICE_EUR,
                    "selected_platforms": ["willhaben", "derstandard_at"],
                },
            },
            {
                "agent_id": "perf-watch-1130",
                "name": "Hietzing buy watch",
                "enabled": False,
                "country_code": "AT",
                "region_code": "vienna",
                "location_query": "1130 Vienna",
                "listing_mode": "buy",
                "property_type": "apartment",
                "notification_limit": 5,
                "notification_period": "week",
                "preferences_json": {
                    "country_code": "AT",
                    "region_code": "vienna",
                    "location_query": "1130 Vienna",
                    "listing_mode": "buy",
                    "property_type": "apartment",
                    "selected_platforms": ["willhaben"],
                },
            },
        ],
    }
    if saved_candidates is not None:
        payload["saved_shortlist_candidates"] = saved_candidates
    return payload


def _seed_saved_agents(client: TestClient, *, saved_candidates: list[dict[str, object]] | None = None) -> None:
    response = client.post(
        "/v1/onboarding/property-search/preferences",
        json=_property_preferences_payload(saved_candidates=saved_candidates),
    )
    if response.status_code != 200:
        raise RuntimeError(f"saved_agents_seed_failed:{response.status_code}:{response.text[:280]}")


def _synthetic_candidate(*, saved_from_run_id: str = "") -> dict[str, object]:
    candidate = {
        "candidate_ref": "perf-candidate-1020",
        "rank": 1,
        "title": "Performance smoke apartment in 1020 Vienna",
        "source_label": "Willhaben | Austria | Rent | 1020 Vienna",
        "source_platform": "willhaben",
        "property_url": "https://example.invalid/propertyquarry/performance-smoke",
        # A bare availability flag is intentionally not enough to authorize a 3D build.
        "floorplan_url": "/assets/propertyquarry/performance-smoke-floorplan.svg",
        "packet_url": "/app/research/perf-candidate-1020",
        "review_url": "/app/research/perf-candidate-1020",
        "fit_score": 91,
        "score": 91,
        "fit_summary": "Transit, area, layout and budget fit the seeded brief.",
        "match_reasons": ["1020 Vienna matches the seeded search area.", "The synthetic listing keeps route and layout data compact."],
        "mismatch_reasons": ["Operating costs are still missing from the listing."],
        "property_facts": {
            "postal_code": "1020",
            "postal_name": "1020 Vienna",
            "district": "1020 Vienna",
            "street_address": "Nordbahnstrasse 32",
            "exact_address": "Nordbahnstrasse 32, 1020 Vienna, Austria",
            "map_lat": 48.22317,
            "map_lng": 16.39594,
            "map_location_precision": "address",
            "location_hint_research_attempted": True,
            "price_display": "EUR 1,290",
            "price_eur": 1290,
            "area_m2": 72,
            "area_sqm": 72,
            "rooms": 3,
            "has_floorplan": True,
            "has_balcony": True,
            "operating_costs_status": "missing",
            "nearest_playground_m": 310,
            "nearest_playground_name": "Rudolfspark Spielplatz",
            "nearest_playground_source": "OpenStreetMap",
            "nearest_supermarket_m": 280,
            "nearest_supermarket_name": "BILLA Praterstern",
            "nearest_supermarket_source": "OpenStreetMap",
            "nearest_pharmacy_m": 640,
            "nearest_pharmacy_name": "Apotheke Nordbahn",
            "nearest_pharmacy_source": "OpenStreetMap",
            "nearest_subway_m": 350,
            "nearest_subway_name": "Praterstern",
            "nearest_subway_source": "OpenStreetMap",
            "nearest_tram_bus_m": 180,
            "nearest_tram_bus_name": "Nordbahnstrasse",
            "nearest_tram_bus_source": "OpenStreetMap",
            "nearest_flowing_water_m": 890,
            "nearest_flowing_water_name": "Donaukanal",
            "nearest_flowing_water_kind": "canal",
            "nearest_flowing_water_source": "OpenStreetMap",
            "listing_fact_confirmation": {
                "status": "confirmed",
                "label": "Listing facts",
                "summary": "4 listing facts read automatically from the listing.",
                "fields": ["area", "location", "price", "rooms"],
                "sources": {
                    "area": "provider_structured_fact",
                    "location": "provider_structured_fact",
                    "price": "provider_structured_fact",
                    "rooms": "provider_structured_fact",
                },
                "requires_manual_confirmation": False,
            },
        },
        "route_evidence": [
            {"label": "Transit", "distance": "350 m", "icon": "U"},
            {"label": "School", "distance": "650 m", "icon": "S"},
        ],
    }
    if saved_from_run_id:
        candidate["saved_from_run_id"] = saved_from_run_id
    return candidate


def _synthetic_search_result(*args: object, **kwargs: object) -> dict[str, object]:
    progress_callback = kwargs.get("progress_callback")
    if callable(progress_callback):
        progress_callback(
            step="sources_resolved",
            message="Resolved synthetic performance smoke source.",
            status="in_progress",
            steps_delta=1,
            summary_updates={"sources_total": 1, "source_variant_total": 1, "provider_total": 1},
        )
    candidate = _synthetic_candidate()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "processed",
        "sources_total": 1,
        "source_variant_total": 1,
        "provider_total": 1,
        "listing_total": 1,
        "raw_listing_total": 1,
        "reviewed_listing_total": 1,
        "ranked_total": 1,
        "filtered_total": 0,
        "held_back_total": 0,
        "review_created_total": 1,
        "review_existing_total": 0,
        "notified_total": 0,
        "email_notified_total": 0,
        "tour_created_total": 0,
        "tour_existing_total": 0,
        "high_fit_total": 1,
        "watch_notified_total": 0,
        "ranked_candidates": [candidate],
        "top_candidates": [candidate],
        "sources": [
            {
                "source_label": "Willhaben | Austria | Rent | 1020 Vienna",
                "platform": "willhaben",
                "status": "completed",
                "listing_total": 1,
                "ranked_total": 1,
            }
        ],
    }


def _start_synthetic_run(client: TestClient) -> str:
    from app.product.service import ProductService

    original: Callable[..., dict[str, object]] = ProductService.sync_direct_property_scout
    ProductService.sync_direct_property_scout = _synthetic_search_result  # type: ignore[method-assign]
    try:
        response = client.post(
            "/app/api/property/search-runs",
            json={
                "selected_platforms": ["willhaben"],
                "property_preferences": {
                    "country_code": "AT",
                    "region_code": "vienna",
                    "listing_mode": "rent",
                    "property_type": ["apartment"],
                    "location_query": PERFORMANCE_SMOKE_LOCATION_QUERY,
                    "min_area_m2": PERFORMANCE_SMOKE_MIN_AREA_M2,
                    "max_price_eur": PERFORMANCE_SMOKE_MAX_PRICE_EUR,
                },
                "max_results_per_source": 1,
            },
        )
        if response.status_code != 202:
            raise RuntimeError(f"synthetic_run_start_failed:{response.status_code}:{response.text[:280]}")
        run_id = str(response.json().get("run_id") or "").strip()
        if not run_id:
            raise RuntimeError("synthetic_run_missing_run_id")
        for _ in range(160):
            status = client.get(f"/app/api/property/search-runs/{run_id}")
            if status.status_code == 200 and str(status.json().get("status") or "").lower() in {"processed", "completed"}:
                _seed_saved_agents(client, saved_candidates=[_synthetic_candidate(saved_from_run_id=run_id)])
                return run_id
            time.sleep(0.025)
        raise RuntimeError(f"synthetic_run_timeout:{run_id}")
    finally:
        ProductService.sync_direct_property_scout = original  # type: ignore[method-assign]


def _open_workspace_access_session(client: TestClient) -> None:
    response = client.post(
        "/app/api/access-sessions",
        json={
            "email": "performance-smoke@propertyquarry.test",
            "role": "principal",
            "display_name": "Performance Smoke",
            "expires_in_hours": 24,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(f"access_session_seed_failed:{response.status_code}:{response.text[:280]}")
    access_url = str(response.json().get("access_url") or "").strip()
    if not access_url:
        raise RuntimeError("access_session_seed_failed:missing_access_url")
    client.headers.pop("X-EA-Principal-ID", None)
    opened = client.get(access_url, follow_redirects=False)
    if opened.status_code != 303 or not client.cookies.get("ea_workspace_session"):
        raise RuntimeError(f"access_session_open_failed:{opened.status_code}:{opened.text[:280]}")


def _request_measured_route(client: TestClient, path: str) -> tuple[object, int]:
    request_headers = {
        "host": "propertyquarry.com",
        "accept-encoding": "gzip" if path == "/app/search" else "identity",
    }
    started = time.perf_counter()
    response = client.get(
        path,
        headers=request_headers,
        follow_redirects=not (path.startswith("/app/research/") or path == "/app/billing"),
    )
    duration_ms = round((time.perf_counter() - started) * 1000)
    return response, duration_ms


def _measure_route(
    client: TestClient,
    path: str,
    *,
    budget_ms: int,
    cold_budget_ms: int,
) -> dict[str, object]:
    cold_response, cold_duration_ms = _request_measured_route(client, path)
    response, duration_ms = _request_measured_route(client, path)
    attempt_durations_ms = [cold_duration_ms, duration_ms]
    attempt_count = 2
    body = response.text or ""
    lowered_body = body.lower()
    visible_body = _visible_text(body)
    lowered_visible_body = visible_body.lower()
    css_body = ""
    if path != "/app/billing":
        css_body = _asset_text(client, _workbench_css_path_for_route(path, body))
    billing_redirect_location = str(response.headers.get("location") or "").strip()
    billing_handoff_redirect_ok, billing_redirect_host = _billing_handoff_redirect_ok(
        path=path,
        status_code=response.status_code,
        location=billing_redirect_location,
    )
    billing_internal_account_fallback_ok = _billing_internal_account_fallback_ok(
        path=path,
        status_code=response.status_code,
        location=billing_redirect_location,
    )
    cold_billing_location = str(cold_response.headers.get("location") or "").strip()
    cold_billing_handoff_ok, _cold_billing_host = _billing_handoff_redirect_ok(
        path=path,
        status_code=cold_response.status_code,
        location=cold_billing_location,
    )
    cold_billing_internal_fallback_ok = _billing_internal_account_fallback_ok(
        path=path,
        status_code=cold_response.status_code,
        location=cold_billing_location,
    )
    cold_billing_fail_closed_ok = (
        path == "/app/billing" and cold_response.status_code == 503
    )
    noise_hits = [
        phrase
        for phrase in FORBIDDEN_CUSTOMER_NOISE
        if phrase in lowered_body
    ]
    visible_internal_hits = [
        phrase
        for phrase in FORBIDDEN_VISIBLE_INTERNAL_COPY
        if phrase in lowered_visible_body
    ]
    billing_fail_closed_ok = path == "/app/billing" and response.status_code == 503
    checks = [
        {
            "name": "status_ok",
            "ok": response.status_code == 200 or billing_handoff_redirect_ok or billing_internal_account_fallback_ok or billing_fail_closed_ok,
        },
        {"name": "under_budget", "ok": duration_ms <= budget_ms},
        {
            "name": "cold_first_request_status_ok",
            "ok": cold_response.status_code == 200
            or cold_billing_handoff_ok
            or cold_billing_internal_fallback_ok
            or cold_billing_fail_closed_ok,
        },
        {
            "name": "cold_first_request_under_budget",
            "ok": cold_duration_ms <= cold_budget_ms,
        },
        {"name": "contains_propertyquarry", "ok": "PropertyQuarry" in body or billing_handoff_redirect_ok or billing_internal_account_fallback_ok},
        {"name": "no_generic_ea_copy", "ok": "Executive Assistant" not in body and "Morning Memo" not in body},
        {"name": "no_customer_jargon", "ok": not noise_hits, "detail": ", ".join(noise_hits[:5])},
        {
            "name": "no_visible_internal_proof_copy",
            "ok": not visible_internal_hits,
            "detail": ", ".join(visible_internal_hits[:5]),
        },
    ]
    if billing_handoff_redirect_ok:
        checks.append(
            {
                "name": "billing_external_handoff_redirect",
                "ok": True,
                "location_host": billing_redirect_host,
            }
        )
    elif billing_internal_account_fallback_ok:
        checks.append(
            {
                "name": "billing_internal_account_fallback",
                "ok": True,
                "location": billing_redirect_location,
            }
        )
    elif not billing_fail_closed_ok:
        checks.extend(_mobile_surface_contract_checks(path, body, css_body=css_body))
        checks.extend(_rybbit_surface_contract_checks(path, body))
    if path == "/app/search":
        content_encoding = str(response.headers.get("content-encoding") or "").strip().lower()
        vary_header = str(response.headers.get("vary") or "").strip().lower()
        compressed_bytes = _response_content_length(response)
        compressed_max_bytes = _search_compressed_max_bytes()
        checks.extend(
            (
                {
                    "name": "search_gzip_delivery",
                    "ok": "gzip" in content_encoding,
                    "content_encoding": content_encoding or "missing",
                },
                {
                    "name": "search_gzip_vary_accept_encoding",
                    "ok": "accept-encoding" in vary_header,
                    "vary": vary_header or "missing",
                },
                {
                    "name": "search_compressed_payload_under_budget",
                    "ok": 0 < compressed_bytes <= compressed_max_bytes,
                    "compressed_bytes": compressed_bytes,
                    "max_bytes": compressed_max_bytes,
                },
                {
                    "name": "what_matters_distance_controls_compact",
                    "ok": (
                        "grid-template-columns: repeat(auto-fit, minmax(min(100%, 260px), 320px));" in css_body
                        and "justify-content: start;" in css_body
                        and "max-width: 150px;" in css_body
                        and "grid-template-columns: minmax(0, 1fr) minmax(104px, 110px) minmax(96px, 100px);" in css_body
                        and "grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));" not in css_body
                    ),
                },
                {
                    "name": "what_matters_school_distance_controls",
                    "ok": (
                        'name="school_distance__kindergarten"' in body
                        and 'name="school_distance__ganztags_volksschule"' in body
                        and 'name="school_distance__halbtags_volksschule"' in body
                        and 'data-distance-field="max_distance_to_kindergarten_m"' in body
                        and 'data-distance-field="max_distance_to_ganztags_volksschule_m"' in body
                        and 'data-distance-field="max_distance_to_halbtags_volksschule_m"' in body
                    ),
                },
            )
        )
    if path == "/app/agents":
        checks.extend(
            (
                {
                    "name": "agent_cards",
                    "ok": (
                        "Leopoldstadt rent watch" in body
                        and "Hietzing buy watch" in body
                        and "1 active" in visible_body
                        and "2 saved" in visible_body
                    ),
                },
                {"name": "map_only_thumbnails", "ok": "osm_district_overlay" in body and "Map preview unavailable" not in body},
            )
        )
    if path.startswith("/app/properties") or path.startswith("/app/shortlist"):
        compare_hits = [token for token in FORBIDDEN_COMPARE_CARD_TOKENS if token in body]
        has_ranked_results_shell = (
            ('data-workbench-results-table' in body and "pqx-rank" in body)
            or ('data-pqx-ranked-candidates' in body and "pqx-rank" in body)
            or ('data-pq-fast-ranked-run' in body and "ranked homes" in lowered_body)
        )
        checks.extend(
            (
                {
                    "name": "results_ranking_only_no_compare_cards",
                    "ok": has_ranked_results_shell and not compare_hits,
                    "detail": ", ".join(compare_hits[:5]),
                },
                {
                    "name": "results_ranked_not_compare_copy",
                    "ok": has_ranked_results_shell and (
                        "ranked homes" in lowered_body
                        or "ranked opportunities" in lowered_body
                        or "shortlisted homes" in lowered_body
                        or "matching homes" in lowered_body
                        or "matching opportunities" in lowered_body
                        or "saved homes" in lowered_body
                        or "best matches" in lowered_body
                        or " matches" in lowered_body
                    ),
                },
            )
        )
    if path.startswith("/app/research/"):
        unevidenced_visual_ready = (
            'data-prd-visual-card="tour"' in body
            and 'data-prd-visual-card="walkthrough"' in body
            and 'data-pw-visual-state="ready"' in body
            and ("Request 3D tour" in body or "Request walkthrough" in body)
        )
        research_css_anchor = body.find(".prd-topbar")
        mobile_css_start = body.find("@media (max-width: 760px)", research_css_anchor if research_css_anchor >= 0 else 0)
        mobile_css_end = body.find("</style>", mobile_css_start) if mobile_css_start >= 0 else -1
        mobile_detail_css = body[mobile_css_start:mobile_css_end] if mobile_css_start >= 0 and mobile_css_end > mobile_css_start else ""
        compare_hits = [token for token in FORBIDDEN_COMPARE_CARD_TOKENS if token in body]
        checks.extend(
            (
                {"name": "research_candidate", "ok": "Performance smoke apartment in 1020 Vienna" in body},
                {"name": "media_requests_explicit", "ok": "Request" in body and "tour" in body.lower()},
                {"name": "research_visual_cards_present", "ok": 'data-prd-visual-card="tour"' in body and 'data-prd-visual-card="walkthrough"' in body},
                {"name": "research_visual_requests_honest", "ok": 'data-pw-visual-request="tour"' in body and 'data-pw-visual-request="flythrough"' in body and 'data-pw-visual-state="idle"' in body},
                {"name": "research_no_fake_visual_ready", "ok": not unevidenced_visual_ready},
                {"name": "research_listing_facts", "ok": "Listing facts" in body and "read automatically from the listing" in body},
                {"name": "research_listed_price_signal", "ok": "Budget signal" in body and "EUR 1,290" in body},
                {
                    "name": "research_ranking_only_no_compare_cards",
                    "ok": "Performance smoke apartment in 1020 Vienna" in body and not compare_hits,
                    "detail": ", ".join(compare_hits[:5]),
                },
                {
                    "name": "research_mobile_open_property_compact_layout",
                    "ok": (
                        ".prd-hero {\n      grid-template-columns: minmax(0, 1fr);\n      gap: 6px;" in mobile_detail_css
                        and (
                            (
                                ".prd-current-read {\n      display: grid;" in mobile_detail_css
                                and ".prd-current-read .prd-summary-grid {\n      grid-template-columns: minmax(0, 1fr);" in mobile_detail_css
                                and ".prd-current-read .prd-summary-box {\n      min-height: 68px;" in mobile_detail_css
                            )
                            or ".prd-current-read {\n      display: none;" in mobile_detail_css
                        )
                        and (
                            ".prd-media-frame {\n      height: min(46vw, 176px);" in mobile_detail_css
                            or ".prd-media-frame {\n      height: min(42vw, 160px);" in mobile_detail_css
                        )
                    ),
                },
                {
                    "name": "research_mobile_visual_frame_compact",
                    "ok": (
                        ".prd-media-frame.prd-media-frame-live {\n      height: min(58vw, 224px);" in mobile_detail_css
                        and ".prd-media-gradient,\n    .prd-media-caption {\n      display: none;" in mobile_detail_css
                    ),
                },
            )
        )
    if path.startswith("/app/alerts"):
        checks.extend(
            (
                {"name": "alerts_heading", "ok": "Alerts" in body},
                {"name": "delivery_controls", "ok": "Delivery rules" in body or "Notifications" in body},
            )
        )
    if path == "/app/billing" and not billing_handoff_redirect_ok and not billing_internal_account_fallback_ok:
        billing_noise_hits = [token for token in FORBIDDEN_BILLING_SURFACE_TOKENS if token in lowered_body]
        checks.extend(
            (
                {
                    "name": "billing_fail_closed_recovery",
                    "ok": response.status_code == 503
                    and "billing portal unavailable" in lowered_body
                    and "propertyquarry access stays active" in lowered_body
                    and any(marker in lowered_body for marker in BILLING_FAIL_CLOSED_STATE_MARKERS),
                },
                {"name": "billing_local_board_deleted", "ok": not billing_noise_hits, "detail": ", ".join(billing_noise_hits[:5])},
            )
        )
    if path == "/app/account":
        checks.extend(
            (
                {"name": "account_direct_logout_strip", "ok": "pqx-account-logout-strip" in body and "Current session" in body},
                {"name": "account_single_logout_action", "ok": body.count('data-account-page-sign-out') == 1 and body.count(">Log out</button>") == 1},
                {"name": "account_no_top_dropdown_duplicate_logout", "ok": '<form class="pqx-account-menu-form"' not in body},
                {
                    "name": "account_logout_mobile_target",
                    "ok": ".pqx-account-logout-strip-form .pqx-link-button" in css_body
                    and (
                        "min-height: 46px;" in css_body
                        or "min-height: 48px;" in css_body
                        or "min-height: 52px;" in css_body
                        or "min-height: 56px;" in css_body
                    ),
                },
                {
                    "name": "notification_destination_controls",
                    "ok": all(token in body for token in ("Email", "Telegram", "WhatsApp"))
                    and (
                        "Destination mix" in body
                        or "Strong matches can land in more than one place." in body
                        or "Where matches arrive" in body
                    ),
                },
                {
                    "name": "notification_primary_channel_controls",
                    "ok": ("Primary response lane" in body or "Primary route" in body)
                    and "Save notifications" in body,
                },
                {
                    "name": "notification_opt_in_copy",
                    "ok": ("Strong matches and watch hits" in body or "Strong matches go to every selected channel." in body)
                    and ("Near-miss follow-up prompts" in body or "Near-miss follow-up stays Telegram-only when Telegram is primary." in body),
                },
                {"name": "notification_secret_safe", "ok": "telegram-secret-token" not in body and "raw_delivery_receipts" not in body},
                {"name": "account_notifications", "ok": "<h2>Notifications</h2>" in body},
                {"name": "account_notification_form", "ok": 'action="/app/api/property/account/notifications"' in body},
                {"name": "account_notification_email_channel", "ok": 'name="notification_channels" value="email"' in body},
                {"name": "account_notification_telegram_channel", "ok": 'name="notification_channels" value="telegram"' in body},
                {"name": "account_notification_whatsapp_channel", "ok": 'name="notification_channels" value="whatsapp"' in body},
                {"name": "account_notification_primary_route", "ok": 'name="preferred_channel"' in body},
                {"name": "account_notification_whatsapp_phone", "ok": 'name="whatsapp_ai_support_phone"' in body},
                {"name": "account_notification_save_action", "ok": "Save notifications" in body},
            )
        )
    if path == "/app/settings/google":
        checks.extend(
            (
                {"name": "google_settings_heading", "ok": "Google sign-in" in body or "PropertyQuarry Google connection" in body},
                {"name": "implicit_account_creation_copy", "ok": "Continue with Google" in body or "Google sign-in" in body},
            )
        )
    if path == "/app/settings/access":
        checks.extend(
            (
                {"name": "access_settings_heading", "ok": "Access" in body or "Identity and return access" in body},
                {"name": "account_access_controls", "ok": "Invite" in body or "access" in lowered_body},
            )
        )
    if path == "/app/settings/usage":
        checks.extend(
            (
                {"name": "usage_settings_heading", "ok": "Usage and activation" in body},
                {"name": "usage_metrics_visible", "ok": "Searches opened" in body or "activation" in lowered_body},
            )
        )
    if path == "/app/settings/support":
        checks.extend(
            (
                {"name": "support_settings_heading", "ok": "Support" in body or "Support and recovery" in body},
                {"name": "support_recovery_controls", "ok": "recovery" in lowered_body or "support" in lowered_body},
            )
        )
    if path == "/app/settings/trust":
        checks.extend(
            (
                {"name": "trust_settings_heading", "ok": "Search health" in body or "Reliability" in body or "Trust" in body},
                {"name": "trust_evidence_visible", "ok": "source health" in lowered_body or "list health" in lowered_body},
            )
        )
    if path == "/app/settings/invitations":
        checks.extend(
            (
                {"name": "invitations_settings_heading", "ok": "Invitations" in body},
                {"name": "invitation_controls_visible", "ok": "Invite" in body or "invitation" in lowered_body},
            )
        )
    if path == "/sign-in":
        secure_email_access_copy = "use a secure email link if your address already has access." in lowered_visible_body
        safe_provider_copy = "continue with an available provider." in lowered_visible_body
        checks.extend(
            (
                {
                    "name": "connected_identity_implicit_account_creation",
                    "ok": secure_email_access_copy and safe_provider_copy,
                },
                {
                    "name": "connected_identity_copy_is_customer_safe",
                    "ok": (
                        "oauth_config_missing" not in lowered_body
                        and "callback setup" not in lowered_body
                        and secure_email_access_copy
                        and safe_provider_copy
                    ),
                },
            )
        )
    return {
        "path": path,
        "status_code": response.status_code,
        "duration_ms": duration_ms,
        "first_duration_ms": cold_duration_ms,
        "attempt_durations_ms": attempt_durations_ms,
        "attempt_count": attempt_count,
        "budget_ms": budget_ms,
        "cold_budget_ms": cold_budget_ms,
        "measurements": {
            "cold": {
                "sequence": 1,
                "kind": "first_measured_request_after_fixture_setup",
                "cache_state": "server_cache_not_explicitly_prewarmed_or_cleared",
                "duration_ms": cold_duration_ms,
                "status_code": cold_response.status_code,
                "response_bytes": _response_content_length(cold_response),
                "budget_ms": cold_budget_ms,
                "ok": cold_duration_ms <= cold_budget_ms,
            },
            "warm": {
                "sequence": 2,
                "kind": "same_client_immediate_repeat_request",
                "cache_state": "same_process_and_client_repeat_eligible",
                "duration_ms": duration_ms,
                "status_code": response.status_code,
                "response_bytes": _response_content_length(response),
                "budget_ms": budget_ms,
                "ok": duration_ms <= budget_ms,
            },
        },
        "cold_to_warm": {
            "duration_delta_ms": cold_duration_ms - duration_ms,
            "response_bytes_delta": _response_content_length(cold_response)
            - _response_content_length(response),
        },
        "ok": all(bool(row["ok"]) for row in checks),
        "checks": checks,
    }


@_with_scrubbed_performance_environment
def build_authenticated_performance_receipt(
    *,
    route_budget_ms: int = 1200,
    cold_route_budget_ms: int = DEFAULT_COLD_ROUTE_BUDGET_MS,
    constrained_client_target_url: str = "",
    constrained_client_authentication_bootstrap_url: str = "",
    constrained_client_release_probe_secret: str = "",
    release_commit_sha: str = "",
    release_image_digest: str = "",
    release_deployment_id: str = "",
    release_manifest_sha256: str = "",
    expected_chromium_executable_path: str = "",
    expected_chromium_executable_sha256: str = "",
    constrained_client_browser_engines: Sequence[str] = ("chromium",),
    constrained_client_profile_config: Mapping[str, object]
    | ConstrainedClientProfile
    | None = None,
    constrained_browser_collector: Callable[..., dict[str, object]] = collect_constrained_client_browser_evidence,
    release_identity_probe: Callable[..., dict[str, object]] = probe_live_release_identity,
) -> dict[str, object]:
    warm_budget, cold_budget = _validate_route_budgets(
        warm_route_budget_ms=route_budget_ms,
        cold_route_budget_ms=cold_route_budget_ms,
    )
    profile = constrained_client_profile_from_config(
        constrained_client_profile_config
    )
    target_url = str(constrained_client_target_url or "").strip()
    bootstrap_url = str(
        constrained_client_authentication_bootstrap_url or ""
    ).strip()
    probe_secret = str(constrained_client_release_probe_secret or "").strip()
    if target_url and bool(bootstrap_url) == bool(probe_secret):
        raise PerformanceConfigError(
            "constrained_client_target_requires_exactly_one_authentication_method"
        )
    if not target_url and (bootstrap_url or probe_secret):
        raise PerformanceConfigError(
            "constrained_client_authentication_requires_target"
        )
    expected_identity, release_identity_errors = _expected_release_identity(
        commit_sha=release_commit_sha,
        image_digest=release_image_digest,
        deployment_id=release_deployment_id,
        manifest_sha256=release_manifest_sha256,
    )
    _reset_authenticated_performance_smoke_env()
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ.pop("DATABASE_URL", None)
    # Keep prod-mode startup valid even when this smoke runs outside the live container.
    os.environ["EA_RUNTIME_MODE"] = "dev"
    os.environ["EA_API_TOKEN"] = "performance-smoke-local-token"
    os.environ["PROPERTYQUARRY_MAP_TILE_NETWORK_ENABLED"] = "0"
    os.environ["PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES"] = "1"
    os.environ["EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER"] = "1"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_ENABLED"] = "0"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_ENABLED"] = "0"
    os.environ["PROPERTYQUARRY_BRILLIANT_DIRECTORIES_DISABLED"] = "1"
    for name in (
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BASE_URL",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_ALLOWED_HOSTS",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_BILLING_URL",
        "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY",
    ):
        os.environ.pop(name, None)
    api_token = str(os.environ.get("EA_API_TOKEN") or "").strip()
    principal_id = "pq-auth-performance-smoke"
    from app.api.app import create_app

    with (
        _provider_free_landing_view_model_boundaries(),
        _provider_free_public_tour_directory(),
    ):
        app = create_app()
        with TestClient(app, base_url="https://propertyquarry.com") as client:
            client.headers.update(
                {
                    "X-EA-Principal-ID": principal_id,
                    "X-EA-API-Token": api_token,
                    "Authorization": f"Bearer {api_token}",
                    "host": "propertyquarry.com",
                }
            )
            _seed_workspace(client)
            _seed_saved_agents(client)
            run_id = _start_synthetic_run(client)
            _open_workspace_access_session(client)
            routes = [
                "/sign-in",
                "/app/search",
                "/app/agents",
                f"/app/properties?run_id={run_id}",
                f"/app/shortlist?run_id={run_id}",
                f"/app/research/perf-candidate-1020?run_id={run_id}",
                f"/app/alerts?run_id={run_id}",
                "/app/account",
                "/app/billing",
                "/app/settings/google",
                "/app/settings/access",
                "/app/settings/usage",
                "/app/settings/support",
                "/app/settings/trust",
                "/app/settings/invitations",
            ]
            rows = [
                _measure_route(
                    client,
                    route,
                    budget_ms=_route_budget_for(
                        route,
                        route_budget_ms=warm_budget,
                    ),
                    cold_budget_ms=cold_budget,
                )
                for route in routes
            ]
    failed = [row for row in rows if not row.get("ok")]
    if target_url:
        browser_parent_environment = _scrub_performance_subprocess_environment()
        try:
            constrained_client = collect_constrained_client_evidence(
                target_url=target_url,
                authentication_bootstrap_url=bootstrap_url,
                release_probe_secret=probe_secret,
                profile=profile,
                expected_release_identity=(
                    expected_identity if not release_identity_errors else None
                ),
                expected_chromium_executable_path=(
                    expected_chromium_executable_path
                ),
                expected_chromium_executable_sha256=(
                    expected_chromium_executable_sha256
                ),
                browser_engines=constrained_client_browser_engines,
                collector=constrained_browser_collector,
                release_identity_probe=release_identity_probe,
            )
        finally:
            _restore_process_environment(browser_parent_environment)
    else:
        constrained_client = {
            "status": "not_run",
            "profile": constrained_client_profile_receipt(profile),
            "target": "",
            "release_identity": {
                "status": "not_run",
                "version_url": "",
                "status_code": 0,
                "tls_verified": False,
                "expected": {},
                "observed": {},
                "matches_expected": False,
                "error": "constrained_client_target_missing",
                "credential_persisted": False,
            },
            "requested_browser_engines": [],
            "engine_rows": [],
            "limitations_by_engine": {},
            "limitations": [
                "No authenticated browser target and protected browser authentication method were supplied; this receipt does not contain constrained-client evidence."
            ],
            "field_core_web_vitals_claimed": False,
            "physical_device_claimed": False,
        }
    constrained_failed = (
        bool(target_url) and constrained_client.get("status") != "pass"
    )
    engine_rows = list(constrained_client.get("engine_rows") or [])
    signed_release_probe_ready = (
        len(engine_rows) == 1
        and isinstance(engine_rows[0], Mapping)
        and isinstance(engine_rows[0].get("authentication"), Mapping)
        and engine_rows[0]["authentication"].get("method")
        == "signed_release_probe_per_navigation"
    )
    release_identity_ready = (
        not release_identity_errors
        and isinstance(constrained_client.get("release_identity"), Mapping)
        and constrained_client["release_identity"].get("status") == "pass"
        and constrained_client["release_identity"].get("matches_expected") is True
    )
    fixed_flagship_server_thresholds = warm_budget == 1200 and cold_budget == 2400
    flagship_ready = (
        not failed
        and constrained_client.get("status") == "pass"
        and signed_release_probe_ready
        and release_identity_ready
        and fixed_flagship_server_thresholds
    )
    return {
        "schema": AUTHENTICATED_PERFORMANCE_SCHEMA,
        "status": "pass" if not failed and not constrained_failed else "fail",
        "status_scope": "legacy_authenticated_route_smoke_plus_any_explicitly_requested_constrained_probe",
        "flagship_status": "pass" if flagship_ready else "blocked",
        "flagship_blockers": (
            []
            if flagship_ready
            else [
                *(
                    ["cold_or_warm_authenticated_server_route_failed"]
                    if failed
                    else []
                ),
                *(
                    ["constrained_authenticated_browser_evidence_missing_or_blocked"]
                    if constrained_client.get("status") != "pass"
                    else []
                ),
                *(
                    ["signed_release_probe_authentication_missing_or_blocked"]
                    if not signed_release_probe_ready
                    else []
                ),
                *(
                    ["exact_live_release_identity_missing_or_mismatched"]
                    if not release_identity_ready
                    else []
                ),
                *(
                    ["flagship_server_thresholds_not_fixed"]
                    if not fixed_flagship_server_thresholds
                    else []
                ),
            ]
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "release_identity": expected_identity,
        "principal_id": principal_id,
        "run_id": run_id,
        "route_count": len(rows),
        "failed_count": len(failed),
        "thresholds": {
            "warm_route_budget_ms": warm_budget,
            "cold_route_budget_ms": cold_budget,
        },
        "server_request_evidence": {
            "status": "pass" if not failed else "fail",
            "cold_definition": "first measured route request after authenticated fixture setup; server caches are neither claimed empty nor explicitly prewarmed",
            "warm_definition": "immediate same-process and same-client repeat request",
            "cold_route_count": len(rows),
            "warm_route_count": len(rows),
        },
        "routes": rows,
        "constrained_client_evidence": constrained_client,
        "claims": {
            "cold_and_warm_server_request_lab_evidence": not failed,
            "constrained_browser_lab_evidence": constrained_client.get("status")
            == "pass",
            "signed_release_probe_authentication": signed_release_probe_ready,
            "exact_live_release_identity_observed": release_identity_ready,
            "field_core_web_vitals": False,
            "physical_device_performance": False,
        },
        "notes": [
            "The server-request lane is local, authenticated, provider-free and non-networked.",
            "It records both the first measured request and an immediate warm repeat for sign-in, search, agents, results, research, alerts, account, billing, and settings surfaces; legacy duration_ms and under_budget fields remain the warm measurement.",
            "The constrained-client lane is browser lab emulation only and requires an explicit authenticated same-origin target; launch qualification uses fresh nonce-bound signed release-probe navigation and exact /version identity observation, not a reusable access link.",
            "Legacy status remains compatible with the local authenticated route smoke; flagship_status stays blocked until the constrained browser lane, signed probe authentication, fixed server thresholds, and exact release identity all pass.",
            "It also asserts shared top navigation, viewport metadata, app shell, no legacy mobile bottom dock, and content-first mobile layouts for static account/billing/settings surfaces.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run authenticated PropertyQuarry route performance smoke.")
    parser.add_argument("--route-budget-ms", type=int, default=1200)
    parser.add_argument(
        "--cold-route-budget-ms",
        type=int,
        default=DEFAULT_COLD_ROUTE_BUDGET_MS,
    )
    parser.add_argument(
        "--constrained-client-target-url",
        default=os.environ.get("PROPERTYQUARRY_PERFORMANCE_TARGET_URL", ""),
        help="Authenticated target URL; defaults to PROPERTYQUARRY_PERFORMANCE_TARGET_URL.",
    )
    parser.add_argument(
        "--constrained-client-authentication-bootstrap-url",
        default=os.environ.get(
            "PROPERTYQUARRY_PERFORMANCE_AUTH_BOOTSTRAP_URL", ""
        ),
        help="Optional local/developer workspace-access bootstrap URL; this compatibility mode never qualifies flagship Gold and is never serialized into the receipt.",
    )
    parser.add_argument(
        "--constrained-client-browser-engine",
        action="append",
        default=[],
        help="Repeat for multiple engines. Only Chromium currently supports the full constrained profile.",
    )
    parser.add_argument(
        "--release-probe-secret-stdin",
        action="store_true",
        help="Read the protected release-probe credential once from stdin before app imports or browser launch.",
    )
    parser.add_argument("--write", default="", help="Optional JSON receipt output path.")
    parser.add_argument(
        "--release-commit-sha",
        default=(
            os.environ.get("PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA", "")
            or os.environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA", "")
        ),
    )
    parser.add_argument(
        "--release-image-digest",
        default=(
            os.environ.get("PROPERTYQUARRY_EXPECTED_RELEASE_IMAGE_DIGEST", "")
            or os.environ.get("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST", "")
        ),
    )
    parser.add_argument(
        "--release-deployment-id",
        default=os.environ.get("PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID", ""),
    )
    parser.add_argument(
        "--expected-release-manifest-sha256",
        default=os.environ.get(
            "PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256", ""
        ),
        help="Independent controller-provided lowercase SHA-256 of the canonical runtime manifest.",
    )
    parser.add_argument(
        "--expected-chromium-executable-path",
        default=os.environ.get(
            "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_PATH", ""
        ),
    )
    parser.add_argument(
        "--expected-chromium-executable-sha256",
        default=os.environ.get(
            "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_SHA256", ""
        ),
    )
    args = parser.parse_args()
    constrained_target_url = str(args.constrained_client_target_url or "")
    constrained_bootstrap_url = str(
        args.constrained_client_authentication_bootstrap_url or ""
    )
    if os.environ.get("PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET") or os.environ.get(
        "PROPERTYQUARRY_LIVE_PROBE_SECRET"
    ):
        parser.error(
            "release-probe credentials must not be supplied in the performance process environment; use --release-probe-secret-stdin"
        )
    release_probe_secret = ""
    if args.release_probe_secret_stdin:
        raw_secret = sys.stdin.buffer.read(4_097)
        if len(raw_secret) > 4_096:
            parser.error("release-probe credential stdin exceeds 4096 bytes")
        try:
            decoded_secret = raw_secret.decode("utf-8")
        except UnicodeDecodeError:
            parser.error("release-probe credential stdin must be UTF-8")
        release_probe_secret = decoded_secret.rstrip("\r\n")
        if (
            not release_probe_secret
            or "\x00" in release_probe_secret
            or "\r" in release_probe_secret
            or "\n" in release_probe_secret
        ):
            parser.error("release-probe credential stdin is malformed")
    # Retain only explicit non-secret runtime plumbing before importing the app,
    # starting Playwright's driver, or launching the browser. Protected release,
    # database, provider, analytics, notification, and bootstrap inputs remain
    # local Python values and are not inherited by subprocesses.
    original_environment = _scrub_performance_subprocess_environment()
    try:
        try:
            receipt = build_authenticated_performance_receipt(
                route_budget_ms=args.route_budget_ms,
                cold_route_budget_ms=args.cold_route_budget_ms,
                constrained_client_target_url=constrained_target_url,
                constrained_client_authentication_bootstrap_url=constrained_bootstrap_url,
                constrained_client_release_probe_secret=release_probe_secret,
                release_commit_sha=args.release_commit_sha,
                release_image_digest=args.release_image_digest,
                release_deployment_id=args.release_deployment_id,
                release_manifest_sha256=args.expected_release_manifest_sha256,
                expected_chromium_executable_path=(
                    args.expected_chromium_executable_path
                ),
                expected_chromium_executable_sha256=(
                    args.expected_chromium_executable_sha256
                ),
                constrained_client_browser_engines=(
                    tuple(args.constrained_client_browser_engine)
                    if args.constrained_client_browser_engine
                    else ("chromium",)
                ),
            )
        except PerformanceConfigError as exc:
            parser.error(str(exc))
    finally:
        _restore_process_environment(original_environment)
    body = json.dumps(receipt, indent=2, sort_keys=True)
    if str(args.write or "").strip():
        out_path = Path(str(args.write)).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(body + "\n", encoding="utf-8")
    print(body)
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    exit_code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # The smoke boots the full app and can leave non-daemon provider/testclient
    # helper threads alive during interpreter shutdown. The receipt is complete
    # once flushed, so fail/exit deterministically instead of hanging CI.
    os._exit(exit_code)
