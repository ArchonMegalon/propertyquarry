#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlsplit

if __package__:
    from scripts import propertyquarry_evidence_contract as evidence_contract
    from scripts.propertyquarry_advanced_visual_gold_binding import (
        UNBOUND_PRODUCER_STATE,
        verify_advanced_visual_binding_receipt,
    )
    from scripts.propertyquarry_continuous_ux_gate import (
        validate_visual_baseline_receipt,
        visual_baseline_payload_sha256,
    )
    from scripts.property_evidence_overlay_read_model import (
        verify_receipt as verify_evidence_overlay_read_model_receipt,
    )
    from scripts.propertyquarry_observability_receipts import (
        OPERATIONS_SHARED_INPUT_NAMES,
        OPERATIONS_VERIFICATION_PRODUCER,
        OPERATIONS_VERIFICATION_SCHEMA,
        REPLICA_ID_RE,
        ReceiptValidationError as ObservabilityReceiptValidationError,
        atomic_write_json as write_observability_verification,
        verify_receipt_bundle,
    )
    from scripts.propertyquarry_slo_evidence import (
        DEFAULT_ALERTMANAGER_CONFIG_PATH,
        DEFAULT_PROMETHEUS_CONFIG_PATH,
        DEFAULT_RULES_PATH,
        DEFAULT_RULE_TESTS_PATH,
        DEFAULT_SLO_PATH,
        EvidenceConfig,
        RANGE_RECEIPT_SCHEMA,
        run_evidence_gate,
    )
    from scripts.propertyquarry_rybbit_evidence import (
        verify_receipt as verify_rybbit_delivery_receipt,
    )
    from scripts.propertyquarry_strict_json import (
        StrictJsonError,
        load_strict_json_object_snapshot,
        loads_strict_json_object,
    )
else:
    import propertyquarry_evidence_contract as evidence_contract
    from propertyquarry_advanced_visual_gold_binding import (
        UNBOUND_PRODUCER_STATE,
        verify_advanced_visual_binding_receipt,
    )
    from propertyquarry_continuous_ux_gate import (
        validate_visual_baseline_receipt,
        visual_baseline_payload_sha256,
    )
    from property_evidence_overlay_read_model import (
        verify_receipt as verify_evidence_overlay_read_model_receipt,
    )
    from propertyquarry_observability_receipts import (
        OPERATIONS_SHARED_INPUT_NAMES,
        OPERATIONS_VERIFICATION_PRODUCER,
        OPERATIONS_VERIFICATION_SCHEMA,
        REPLICA_ID_RE,
        ReceiptValidationError as ObservabilityReceiptValidationError,
        atomic_write_json as write_observability_verification,
        verify_receipt_bundle,
    )
    from propertyquarry_slo_evidence import (
        DEFAULT_ALERTMANAGER_CONFIG_PATH,
        DEFAULT_PROMETHEUS_CONFIG_PATH,
        DEFAULT_RULES_PATH,
        DEFAULT_RULE_TESTS_PATH,
        DEFAULT_SLO_PATH,
        EvidenceConfig,
        RANGE_RECEIPT_SCHEMA,
        run_evidence_gate,
    )
    from propertyquarry_rybbit_evidence import (
        verify_receipt as verify_rybbit_delivery_receipt,
    )
    from propertyquarry_strict_json import (  # type: ignore[no-redef]
        StrictJsonError,
        load_strict_json_object_snapshot,
        loads_strict_json_object,
    )


GOLD_STATUS_SCHEMA = "propertyquarry.gold_status.v1"
CORE_REQUIRED_TOUR_PROVIDER_MODES = ("matterport",)
ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES = ("magicfit", "magic", "omagic")
# Legacy combined/operator vocabulary. New release decisions must use the
# explicit core/advanced fields below rather than inferring policy from it.
REQUIRED_TOUR_PROVIDER_MODES = ("matterport", "3dvista", "magicfit")
OPTIONAL_TOUR_PROVIDER_MODES = ("pano2vr", "krpano")
REQUIRED_SCENE_VIDEO_PARITY_PROVIDERS = ("magicfit", "magic", "omagic")
ADVANCED_VISUAL_BLOCKER_AREAS = frozenset(
    {
        "advanced_visual_provider_modes",
        "magicfit_walkthrough_playback",
        "walkthrough_quality",
        "walkthrough_provider_proof",
        "scene_video_readiness",
        "scene_video_provider_refresh_packet",
        "omagic_model_upload_adapter_deploy",
        "scene_video_provider_runtime",
        "advanced_visual_candidate_binding",
    }
)
ACTIVE_PROVIDER_MATRIX_COUNTRY_CODES = ("AT", "DE", "CR")
REQUIRED_RESEARCH_PERFORMANCE_CHECKS = (
    "research_candidate",
    "research_visual_cards_present",
    "research_visual_requests_honest",
    "research_no_fake_visual_ready",
    "research_listing_facts",
    "research_listed_price_signal",
    "research_ranking_only_no_compare_cards",
    "research_mobile_open_property_compact_layout",
    "research_mobile_visual_frame_compact",
)
REQUIRED_SEARCH_PERFORMANCE_CHECKS = (
    "search_gzip_delivery",
    "search_gzip_vary_accept_encoding",
    "search_compressed_payload_under_budget",
    "what_matters_distance_controls_compact",
    "what_matters_school_distance_controls",
)
REQUIRED_RYBBIT_PERFORMANCE_CHECKS = (
    "rybbit_no_identify",
    "rybbit_taxonomy_events_only",
    "rybbit_allowed_attributes_only",
    "rybbit_no_private_payload",
)
AUTHENTICATED_PERFORMANCE_FLAGSHIP_SCHEMA = (
    "propertyquarry.authenticated_performance.v2"
)
AUTHENTICATED_PERFORMANCE_FIXED_SERVER_THRESHOLDS = {
    "warm_route_budget_ms": 1_200,
    "cold_route_budget_ms": 2_400,
}
AUTHENTICATED_PERFORMANCE_RELEASE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
AUTHENTICATED_PERFORMANCE_RELEASE_IMAGE_RE = re.compile(
    r"^sha256:[0-9a-f]{64}$"
)
AUTHENTICATED_PERFORMANCE_DEPLOYMENT_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
)
AUTHENTICATED_PERFORMANCE_REPLICA_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
)
AUTHENTICATED_PERFORMANCE_PLACEHOLDER_HOST_RE = re.compile(
    r"(?:^|[.-])(?:example|placeholder|invalid|localhost|local|test|demo|dummy)(?:[.-]|$)",
    re.IGNORECASE,
)
AUTHENTICATED_PERFORMANCE_MAX_EXECUTABLE_BYTES = 1_073_741_824
AUTHENTICATED_PERFORMANCE_MIN_EXECUTABLE_BYTES = 1_048_576
AUTHENTICATED_PERFORMANCE_RESOURCE_TYPES = frozenset(
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
AUTHENTICATED_PERFORMANCE_FLAGSHIP_PROFILE = {
    "name": "low_end_mobile_lab_v1",
    "cpu": {
        "slowdown_rate": 4,
        "claim": "browser_lab_emulation_only",
    },
    "network": {
        "latency_ms": 150,
        "download_kbps": 1600,
        "upload_kbps": 750,
        "offline": False,
        "claim": "browser_lab_emulation_only",
    },
    "viewport": {
        "width": 390,
        "height": 844,
        "device_scale_factor": 1,
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
        "cold_navigation_budget_ms": 15_000,
        "warm_navigation_budget_ms": 8_000,
        "max_request_count": 120,
        "max_transferred_bytes": 3_000_000,
        "max_failed_requests": 0,
        "slowest_resource_limit": 5,
        "navigation_timeout_ms": 30_000,
    },
}
REQUIRED_AUTHENTICATED_PERFORMANCE_SERVER_ROUTES = (
    "/sign-in",
    "/app/search",
    "/app/agents",
    "/app/properties",
    "/app/shortlist",
    "/app/research/",
    "/app/alerts",
    "/app/account",
    "/app/billing",
    "/app/settings/google",
    "/app/settings/access",
    "/app/settings/usage",
    "/app/settings/support",
    "/app/settings/trust",
    "/app/settings/invitations",
)
AUTHENTICATED_PERFORMANCE_BROWSER_MEASUREMENT_FIELDS = frozenset(
    {
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
    }
)
AUTHENTICATED_PERFORMANCE_DOCUMENT_RELEASE_IDENTITY_FIELDS = frozenset(
    {
        "commit_sha",
        "image_digest",
        "deployment_id",
        "manifest_status",
        "manifest_sha256",
        "replica_id",
    }
)
AUTHENTICATED_PERFORMANCE_DOCUMENT_AUTHENTICATION_BINDING_FIELDS = frozenset(
    {
        "cache_control",
        "expected_nonce_sha256",
        "acknowledged_nonce_sha256",
    }
)
AUTHENTICATED_PERFORMANCE_NAVIGATION_TIMING_FIELDS = frozenset(
    {
        "responseStartMs",
        "responseEndMs",
        "domContentLoadedMs",
        "loadEventMs",
        "transferSize",
        "encodedBodySize",
        "decodedBodySize",
    }
)
REQUIRED_LIVE_MOBILE_ROUTES = (
    "/app/properties",
    "/app/search",
    "/app/shortlist",
    "/app/agents",
    "/app/alerts",
    "/app/account",
    "/app/billing",
    "/app/settings/google",
    "/app/settings/access",
    "/app/settings/usage",
    "/app/settings/support",
    "/app/settings/trust",
    "/app/settings/invitations",
    "/app/settings/outcomes",
    "/app/settings/plan",
    "/app/research",
    "/app/properties/packets",
    "/app/properties/notifications/preview",
    "/app/support",
)
REQUIRED_LIVE_MOBILE_DETAIL_PREFIXES = (
    "/app/research/",
    "/app/shortlist/run/",
    "/tours/",
)
REQUIRED_LIVE_MOBILE_COVERAGE_CHECKS = (
    "research_detail_route_configured",
    "shortlist_run_route_configured",
    "public_tour_route_configured",
    "registry_mobile_customer_surfaces_covered",
)
REQUIRED_FLAGSHIP_MOBILE_VIEWPORTS = ((390, 844), (412, 915))
SUPPORTED_FLAGSHIP_BROWSER_ENGINES = ("chromium", "firefox", "webkit")
DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES = SUPPORTED_FLAGSHIP_BROWSER_ENGINES
REQUIRED_FLAGSHIP_BROWSER_CHECKS = (
    "no_horizontal_overflow",
    "primary_touch_targets",
    "browser_navigation_committed",
    "browser_touch_context",
    "browser_focus_navigation",
)
REQUIRED_PUBLIC_AUTH_CHECKS = (
    "sign_in_minimal_copy",
    "sign_in_connected_identity_creates_account",
    "sign_in_no_unavailable_auth_copy",
    "sign_in_google_state",
    "sign_in_google_feedback",
)
REQUIRED_PUBLIC_INFORMATION_ROUTES = (
    "/",
    "/pricing",
    "/security",
    "/privacy",
    "/terms",
    "/support",
    "/imprint",
    "/cookies",
    "/subprocessors",
    "/refunds",
    "/disclaimers",
    "/integrations",
    "/docs",
    "/guides/wohnung-kaufen-wien-checkliste",
    "/markets/vienna",
    "/register",
    "/sign-in",
)
REQUIRED_BILLING_SURFACE_CHECKS = (
    "billing_local_board_deleted",
)
REQUIRED_FLAGSHIP_BILLING_HANDOFF_CHECKS = (
    "billing_external_handoff",
    "billing_external_handoff_resolves",
    "billing_external_handoff_usable",
    "billing_no_second_login",
)
REQUIRED_ACCOUNT_NOTIFICATION_CHECKS = (
    "account_notifications",
    "account_notification_form",
    "account_notification_email_channel",
    "account_notification_telegram_channel",
    "account_notification_whatsapp_channel",
    "account_notification_primary_route",
    "account_notification_whatsapp_phone",
    "account_notification_save_action",
)
FLAGSHIP_CUSTOMER_UX_RECEIPT_AREAS = (
    "continuous_ux",
    "public_auth_surfaces",
    "authenticated_customer_surfaces",
    "live_mobile_surfaces",
    "accessibility",
    "failure_states",
    "activation_to_value",
    "billing_handoff",
    "browser_rendered_3d",
    "map_preview_flagship",
    "walkthrough_quality",
)
DEFAULT_FLAGSHIP_MAX_RECEIPT_AGE_HOURS = 24.0
DEFAULT_RECEIPT_FUTURE_TOLERANCE_SECONDS = 300.0
DEFAULT_EVIDENCE_OVERLAY_MAX_AGE_HOURS = 48.0
DEFAULT_RYBBIT_EVIDENCE_MAX_AGE_MINUTES = 15.0
DEFAULT_SLO_EVIDENCE_MAX_AGE_SECONDS = 900
SLO_EVIDENCE_RECEIPT_SCHEMA = "propertyquarry.slo_evidence_receipt.v2"
GLOBAL_MARKET_ENVELOPE_RECEIPT_SCHEMA = (
    "propertyquarry.global_market_envelope_receipt.v1"
)
INCIDENT_SUPPORT_GATE_RECEIPT_SCHEMA = "propertyquarry.incident_support_gate.v1"
GLOBAL_EXPERIENCE_GATE_RECEIPT_SCHEMA = "propertyquarry.global_experience_gate.v1"
JURISDICTION_PRIVACY_RIGHTS_GATE_RECEIPT_SCHEMA = (
    "propertyquarry.jurisdiction_privacy_rights_gate.v1"
)
JURISDICTION_PRIVACY_RIGHTS_CONTRACT_PATH = Path(
    "config/compliance/propertyquarry_jurisdiction_privacy_rights.v1.json"
)
JURISDICTION_PRIVACY_RIGHTS_MARKET_ENVELOPE_PATH = Path(
    "docs/propertyquarry_global_market_envelope.v1.json"
)
REQUIRED_FLAGSHIP_ACCESSIBILITY_ROUTES = (
    *REQUIRED_PUBLIC_INFORMATION_ROUTES,
    *REQUIRED_LIVE_MOBILE_ROUTES,
)
REQUIRED_FLAGSHIP_ACCESSIBILITY_CHECKS = (
    "route_document_loaded",
    "axe_core_version_pinned",
    "axe_no_moderate_or_higher_wcag_violations",
    "keyboard_only_navigation",
    "visible_keyboard_focus",
    "focus_not_obscured",
    "target_size_24_css_px_or_spacing",
    "dialog_focus_contract",
    "semantic_error_states",
    "semantic_live_progress_states",
    "zoom_200_reflow",
    "zoom_400_reflow",
    "contrast_signals_clear",
    "reduced_motion_honored",
)
REQUIRED_CONTINUOUS_UX_SCHEMA = "propertyquarry.continuous_ux_receipt.v2"
REQUIRED_CONTINUOUS_UX_PROOF_SCOPE = "isolated_loopback_memory_app"
REQUIRED_CONTINUOUS_UX_PROOF_MODE = "playwright_browser_all_isolated"
REQUIRED_CONTINUOUS_UX_ROUTES = (
    "/",
    "/app/search",
    "/app/search?continuous_ux_state=offline",
)
REQUIRED_CONTINUOUS_UX_TOP_CHECKS = (
    "isolated_loopback_origin",
    "memory_storage_backend",
    "candidate_sha_bound",
    "api_token_present_but_not_persisted",
    "production_claim_false",
    "real_playwright_browser_evidence",
    "browser_engine_route_matrix_complete",
    "loading_error_state_matrix_complete",
    "structural_visual_matrix_complete",
    "zoom_400_matrix_complete",
    "first_value_budget_matrix_complete",
    "provider_response_mocking_forbidden",
    "screenshot_pixel_comparison_complete",
)
REQUIRED_CONTINUOUS_UX_ROW_CHECKS = (
    "route_document_loaded",
    "structural_visual_contract",
    "zoom_400_reflow",
    "first_value_under_budget",
    "provider_response_not_mocked",
)
REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BUDGET_MS = 3_200.0
REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BASIS = "median_three_warm_dom_content_loaded_visible_structure"
REQUIRED_CONTINUOUS_UX_FIRST_VALUE_ENGINE = "chromium"
REQUIRED_CONTINUOUS_UX_FIRST_VALUE_SAMPLE_COUNT = 3
REQUIRED_CONTINUOUS_UX_FIRST_VALUE_MAX_ATTEMPTS = 2
REQUIRED_AXE_CORE_VERSION = "4.10.2"
REQUIRED_FLAGSHIP_FAILURE_STATES = (
    "not_found",
    "internal_error",
    "offline",
    "expired_session",
    "delayed",
    "quota_blocked",
    "payment_failed",
    "empty",
    "partial",
    "provider_blocked",
    "stale",
    "missing_packet",
    "missing_media",
)
REQUIRED_FLAGSHIP_FAILURE_PRESERVATION_ROUTE = "/v1/onboarding/property-search/preferences"
REQUIRED_FLAGSHIP_FAILURE_STATE_CHECKS = (
    "state_marker_visible",
    "calm_customer_copy",
    "useful_next_action",
    "semantic_status_contract",
    "raw_diagnostics_hidden",
    "scenario_transition_proven",
    "customer_data_preserved",
)
REQUIRED_ACTIVATION_TO_VALUE_STEPS = (
    "landing",
    "real_authentication",
    "account_create_or_reopen",
    "first_real_search",
    "real_provider_results",
    "shortlist",
    "research",
    "walkthrough_request_or_reuse",
    "walkthrough_ready",
    "logout",
    "relogin",
    "safe_cleanup",
)
BILLING_MEMBER_TOKEN_REQUIRED_ENV = (
    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY",
    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY_HEADER",
    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_MEMBER_LOGIN_TOKEN_ENABLED",
    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_MEMBER_LOGIN_TOKEN_SECRET",
)
BILLING_MEMBER_TOKEN_ADMIN_ACTION = (
    "generate a Brilliant Directories API key in the admin backend, confirm the member-login token account lane, "
    "then set PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY, PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY_HEADER, "
    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_MEMBER_LOGIN_TOKEN_ENABLED=1, and "
    "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_MEMBER_LOGIN_TOKEN_SECRET so /app/billing opens without a second login"
)
COMMON_OPERATOR_DROP_README_TOKENS = (
    ("title", "PropertyQuarry provider export drop folder"),
    ("no_placeholders", "Do not copy placeholder HTML"),
    ("dry_import", "Single-provider dry import example:"),
    ("gold_gate", "Public gold only passes when verify_property_tour_controls reports ready provider modes"),
)
PROVIDER_OPERATOR_DROP_README_TOKENS = {
    "3dvista": (
        ("complete_export", "Copy the complete 3DVista export folder"),
        ("runtime_marker", "tdvplayer"),
        ("importer", "import_3dvista_export.py"),
    ),
    "pano2vr": (
        ("complete_output", "Copy the complete Pano2VR output folder"),
        ("runtime_marker", "tour.js"),
        ("importer", "import_pano2vr_export.py"),
    ),
    "krpano": (
        ("cube_faces", "cube-face-1"),
        ("license_domain", "KRPANO_LICENSE_DOMAIN=propertyquarry.com"),
        ("importer", "import_krpano_walkable_scene.py"),
    ),
    "magicfit": (
        ("walkthrough_video", "magicfit-walkthrough.mp4"),
        ("render_receipt", "magicfit-receipt.json"),
        ("importer", "import_magicfit_walkthrough.py"),
    ),
}


DEFAULT_RECEIPT_PATTERNS = {
    "performance": ("_completion/smoke/property-auth-performance-*.json",),
    "continuous_ux": ("_completion/smoke/propertyquarry-continuous-ux-*.json",),
    "live_mobile": (
        "state/receipts/propertyquarry_live_mobile*.json",
        "state/receipts/property_live_mobile*.json",
        "_completion/smoke/property-live-mobile*.json",
    ),
    "public_smoke": (
        "state/receipts/propertyquarry_live_public*.json",
        "state/receipts/property_live_public*.json",
        "_completion/smoke/property-live-public*.json",
    ),
    "authenticated_smoke": (
        "state/receipts/propertyquarry_live_authenticated*.json",
        "state/receipts/property_live_authenticated*.json",
        "_completion/smoke/property-live-authenticated*.json",
    ),
    "accessibility": ("_completion/smoke/property-live-accessibility*.json",),
    "failure_states": ("_completion/smoke/property-live-failure-states*.json",),
    "activation_to_value": ("_completion/smoke/property-live-activation-to-value*.json",),
    "tour_control": (
        "_completion/property_tour_controls/*.json",
        "_completion/tours/property-tour-controls*.json",
    ),
    "export_discovery": ("_completion/tours/property-tour-export-discovery*.json",),
    "import_manifest": ("_completion/property_tour_exports/import-manifest*.json",),
    "billing": ("_completion/brilliant_directories/BRILLIANT_DIRECTORIES_PROVIDER_VERIFICATION*.json",),
    "tour_provider_ownership": ("_completion/property_tour_ownership/*.json",),
    "vendor_tooling": ("_completion/tours/property-tour-vendor-tooling*.json",),
    "repair_canary": ("_completion/repair/propertyquarry-repair-canary*.json",),
    "provider_catalog": ("_completion/smoke/property-live-provider-catalog*.json",),
    "provider_matrix": (
        "state/receipts/property_provider_stage*.json",
        "state/receipts/property_live_provider_smoke*.json",
        "state/receipts/property-provider-e2e*.json",
        "_completion/smoke/property-live-provider*.json",
        "_completion/provider_smoke/*provider-matrix*.json",
        "_completion/provider_smoke/all-search-ready*.json",
        "_completion/smoke/property-provider-e2e*.json",
    ),
    "whole_project_scope": ("_completion/whole_project_scope/property-whole-project-scope*.json",),
    "security_posture": ("_completion/security/property-security-posture*.json",),
    "release_hygiene": ("_completion/release_hygiene/property-release-hygiene*.json",),
    "furniture_style_contract": ("_completion/furniture_styles/property-furniture-style-contract*.json",),
    "bts_methodology_contract": ("_completion/bts_methodology/property-bts-methodology-contract*.json",),
    "tour_delivery_contract": ("_completion/tour_delivery/property-tour-delivery-contract*.json",),
    "map_preview_flagship": ("_completion/smoke/property-live-map-preview-flagship*.json",),
    "browser_3d_gate": ("_completion/smoke/property-live-3d-browser-gate*.json",),
    "runtime_reconstruction": ("_completion/tours/property-runtime-reconstruction*.json",),
    "service_generated_reconstruction": ("_completion/tours/property-service-generated-reconstruction*.json",),
    "walkthrough_quality": ("_completion/smoke/property-live-walkthrough-quality*.json",),
    "walkthrough_provider_proof": ("_completion/smoke/property-live-walkthrough-provider-proof*.json",),
    "scene_video_readiness": (
        "_completion/scene_video_readiness/release-gate.json",
        "_completion/scene_video_readiness/PROPERTY_SCENE_VIDEO_READINESS.generated.json",
        "_completion/scene_video_readiness/property-scene-video-readiness*.json",
    ),
    "scene_video_readiness_verifier": (
        "_completion/scene_video_readiness/release-gate-verifier.json",
        "_completion/scene_video_readiness/*readiness-verifier*.json",
    ),
    "scene_video_runtime_status": (
        "_completion/scene_video_readiness/runtime-status*.json",
        "_completion/scene_video_readiness/property-scene-video-runtime-status*.json",
    ),
    "scene_video_provider_refresh_packet": (
        "_completion/scene_video_readiness/provider-refresh-packet.json",
        "_completion/scene_video_readiness/property-scene-video-provider-refresh-packet*.json",
    ),
    "scene_video_provider_refresh_packet_verifier": (
        "_completion/scene_video_readiness/provider-refresh-packet-verifier.json",
        "_completion/scene_video_readiness/*provider-refresh-packet-verifier*.json",
    ),
    "id_austria": ("_completion/id_austria/ID_AUSTRIA_PROVIDER_VERIFICATION*.json",),
    "global_market_envelope": (
        "state/receipts/propertyquarry_global_market_envelope_receipt*.json",
        "state/receipts/propertyquarry-global-market-envelope-receipt*.json",
        "_completion/global_market_envelope/propertyquarry-global-market-envelope-receipt*.json",
        "_completion/property_gold_status/global-market-envelope-receipt*.json",
    ),
    "incident_support": (
        "state/receipts/propertyquarry_incident_support_gate*.json",
        "state/receipts/propertyquarry-incident-support-gate*.json",
        "_completion/incident_support/propertyquarry-incident-support-gate*.json",
        "_completion/property_gold_status/incident-support-gate*.json",
    ),
    "global_experience": (
        "state/receipts/propertyquarry_global_experience_gate*.json",
        "state/receipts/propertyquarry-global-experience-gate*.json",
        "_completion/global_experience/propertyquarry-global-experience-gate*.json",
        "_completion/property_gold_status/global-experience-gate*.json",
    ),
    "jurisdiction_privacy_rights": (
        "state/receipts/propertyquarry_jurisdiction_privacy_rights_gate*.json",
        "state/receipts/propertyquarry-jurisdiction-privacy-rights-gate*.json",
        "_completion/jurisdiction_privacy_rights/propertyquarry-jurisdiction-privacy-rights-gate*.json",
        "_completion/property_gold_status/jurisdiction-privacy-rights-gate*.json",
    ),
}
DEFAULT_RECEIPT_FALLBACKS = {
    "performance": "_completion/smoke/property-auth-performance-latest.json",
    "continuous_ux": "_completion/smoke/propertyquarry-continuous-ux-latest.json",
    "live_mobile": "_completion/smoke/property-live-mobile-surface-latest.json",
    "public_smoke": "_completion/smoke/property-live-public-latest.json",
    "authenticated_smoke": "_completion/smoke/property-live-authenticated-latest.json",
    "accessibility": "_completion/smoke/property-live-accessibility-latest.json",
    "failure_states": "_completion/smoke/property-live-failure-states-latest.json",
    "activation_to_value": "_completion/smoke/property-live-activation-to-value-latest.json",
    "tour_control": "_completion/tours/property-tour-controls-live-container-current.json",
    "export_discovery": "_completion/tours/property-tour-export-discovery-full-current.json",
    "import_manifest": "_completion/property_tour_exports/import-manifest-current.json",
    "billing": "_completion/brilliant_directories/BRILLIANT_DIRECTORIES_PROVIDER_VERIFICATION.generated.json",
    "tour_provider_ownership": "_completion/property_tour_ownership/release-gate.json",
    "vendor_tooling": "_completion/tours/property-tour-vendor-tooling-current.json",
    "repair_canary": "_completion/repair/propertyquarry-repair-canary-latest.json",
    "provider_catalog": "_completion/smoke/property-live-provider-catalog-latest.json",
    "provider_matrix": "_completion/smoke/property-live-provider-latest.json",
    "whole_project_scope": "_completion/whole_project_scope/property-whole-project-scope-latest.json",
    "security_posture": "_completion/security/property-security-posture-latest.json",
    "release_hygiene": "_completion/release_hygiene/property-release-hygiene-latest.json",
    "furniture_style_contract": "_completion/furniture_styles/property-furniture-style-contract-latest.json",
    "bts_methodology_contract": "_completion/bts_methodology/property-bts-methodology-contract-latest.json",
    "tour_delivery_contract": "_completion/tour_delivery/property-tour-delivery-contract-latest.json",
    "map_preview_flagship": "_completion/smoke/property-live-map-preview-flagship-latest.json",
    "browser_3d_gate": "_completion/smoke/property-live-3d-browser-gate-latest.json",
    "runtime_reconstruction": "_completion/tours/property-runtime-reconstruction-release-gate.json",
    "service_generated_reconstruction": "_completion/tours/property-service-generated-reconstruction-current.json",
    "walkthrough_quality": "_completion/smoke/property-live-walkthrough-quality-latest.json",
    "walkthrough_provider_proof": "_completion/smoke/property-live-walkthrough-provider-proof-latest.json",
    "scene_video_readiness": "_completion/scene_video_readiness/release-gate.json",
    "scene_video_readiness_verifier": "_completion/scene_video_readiness/release-gate-verifier.json",
    "scene_video_runtime_status": "_completion/scene_video_readiness/runtime-status.json",
    "scene_video_provider_refresh_packet": "_completion/scene_video_readiness/provider-refresh-packet.json",
    "scene_video_provider_refresh_packet_verifier": "_completion/scene_video_readiness/provider-refresh-packet-verifier.json",
    "id_austria": "_completion/id_austria/ID_AUSTRIA_PROVIDER_VERIFICATION.generated.json",
    "global_market_envelope": "_completion/global_market_envelope/propertyquarry-global-market-envelope-receipt-latest.json",
    "incident_support": "_completion/incident_support/propertyquarry-incident-support-gate-latest.json",
    "global_experience": "_completion/global_experience/propertyquarry-global-experience-gate-latest.json",
    "jurisdiction_privacy_rights": "_completion/jurisdiction_privacy_rights/propertyquarry-jurisdiction-privacy-rights-gate-latest.json",
}

_CANONICAL_GOLD_STATUS_LATEST_PATHS = (
    "_completion/property_gold_status/latest.json",
    "_completion/propertyquarry-gold-status-latest.json",
)
_CANONICAL_GOLD_STATUS_RELEASE_GATE_PATH = "_completion/property_gold_status/release-gate.json"
_MAX_GOLD_RECEIPT_BYTES = 64 * 1024 * 1024


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "missing", "path": str(path)}
    try:
        payload, _raw, _digest = load_strict_json_object_snapshot(
            path,
            field="Gold receipt",
            maximum_bytes=_MAX_GOLD_RECEIPT_BYTES,
        )
    except Exception as exc:
        return {"status": "invalid", "path": str(path), "error": f"{type(exc).__name__}: {exc}"}
    return payload


def _load_json_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {"status": "missing", "path": str(path)}, ""
    try:
        payload, _raw, digest = load_strict_json_object_snapshot(
            path,
            field="Gold receipt",
            maximum_bytes=_MAX_GOLD_RECEIPT_BYTES,
        )
    except Exception as exc:
        return {
            "status": "invalid",
            "path": str(path),
            "error": f"{type(exc).__name__}: {exc}",
        }, ""
    return payload, digest


def _scene_video_runtime_status_summary(payload: dict[str, Any]) -> dict[str, Any]:
    summary = payload.get("summary")
    return dict(summary) if isinstance(summary, dict) else {}


def _scene_video_runtime_status_provider_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in list(payload.get("providers") or []) if isinstance(row, dict)]


def _scene_video_runtime_status_next_actions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in _scene_video_runtime_status_provider_rows(payload):
        provider = str(row.get("provider") or row.get("provider_key") or "").strip()
        action = str(row.get("next_action") or "").strip()
        reason = str(row.get("next_action_reason") or "").strip()
        severity = str(row.get("next_action_severity") or "").strip()
        if not provider or not (action or reason or bool(row.get("attention_required"))):
            continue
        normalized: dict[str, Any] = {
            "provider": provider,
        }
        if action:
            normalized["action"] = action
        if reason:
            normalized["reason"] = reason
        if severity:
            normalized["severity"] = severity
        status = str(row.get("status") or "").strip()
        if status:
            normalized["status"] = status
        execution_lane = str(row.get("execution_lane") or "").strip()
        if execution_lane:
            normalized["execution_lane"] = execution_lane
        provider_backend_key = str(row.get("provider_backend_key") or "").strip()
        if provider_backend_key:
            normalized["provider_backend_key"] = provider_backend_key
        blocking_reason = str(row.get("blocking_reason") or "").strip()
        if blocking_reason:
            normalized["blocking_reason"] = blocking_reason
        blockers = [str(value or "").strip() for value in list(row.get("blockers") or []) if str(value or "").strip()]
        if blockers:
            normalized["blockers"] = blockers
        for key in (
            "runtime_account_count",
            "expected_account_count",
            "tracked_account_count",
            "unavailable_account_count",
            "visible_account_gap",
            "credit_state",
        ):
            value = row.get(key)
            if value not in (None, ""):
                normalized[key] = value
        actions.append(normalized)
    return actions


def _receipt_is_complete_enough(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    status = str(payload.get("status") or "").strip().lower()
    if status in {"running", "in_progress", "verifying"}:
        return False
    if payload.get("checkpoint") is True:
        return False
    if payload.get("complete") is False:
        return False
    return True


def _provider_matrix_proof_rank(payload: dict[str, Any]) -> int:
    if not isinstance(payload, dict):
        return 0
    if "targeted_search_matrix_status" not in payload and "targeted_search_matrix_summary" not in payload:
        return 0

    summary = dict(payload.get("targeted_search_matrix_summary") or {})
    status = str(payload.get("status") or "").strip().lower()
    targeted_status = str(payload.get("targeted_search_matrix_status") or "").strip().lower()
    executed = payload.get("targeted_search_matrix_executed") is True
    summary_executed = summary.get("executed") is True
    providers_covered = summary.get("all_search_ready_providers_covered") is True

    if status == "pass" and targeted_status == "pass" and executed and summary_executed and providers_covered:
        return 3
    if executed and summary_executed:
        return 2
    if executed or status == "pass":
        return 1
    return 0


def _provider_matrix_proof_coverage(payload: dict[str, Any]) -> int:
    if not isinstance(payload, dict):
        return 0
    summary = dict(payload.get("targeted_search_matrix_summary") or {})
    counts: list[int] = []
    for raw_value in (
        payload.get("targeted_search_matrix_count"),
        summary.get("case_count"),
        summary.get("executed_case_count"),
        summary.get("passed_case_count"),
    ):
        try:
            parsed = int(raw_value or 0)
        except Exception:
            parsed = 0
        if parsed > 0:
            counts.append(parsed)
    try:
        strict_case_count = int(summary.get("strict_case_count") or 0)
    except Exception:
        strict_case_count = 0
    try:
        soft_case_count = int(summary.get("soft_filter_case_count") or 0)
    except Exception:
        soft_case_count = 0
    if strict_case_count > 0 or soft_case_count > 0:
        counts.append(strict_case_count + soft_case_count)
    targeted_rows = [
        row for row in list(payload.get("targeted_search_matrix") or [])
        if isinstance(row, dict)
    ]
    if targeted_rows:
        counts.append(len(targeted_rows))
    return max(counts, default=0)


def _provider_matrix_scope_rank(payload: dict[str, Any]) -> int:
    if not isinstance(payload, dict):
        return 0
    summary = dict(payload.get("targeted_search_matrix_summary") or {})
    raw_country_codes = summary.get("country_codes")
    if not isinstance(raw_country_codes, list):
        raw_country_codes = payload.get("country_codes")
    country_codes = tuple(
        dict.fromkeys(
            str(country or "").strip().upper()
            for country in list(raw_country_codes or [])
            if str(country or "").strip()
        )
    )
    if not country_codes:
        return 0
    if tuple(sorted(country_codes)) != tuple(sorted(ACTIVE_PROVIDER_MATRIX_COUNTRY_CODES)):
        return 0
    country_scope = str(payload.get("country_scope") or "").strip().lower()
    if country_scope in {"explicit", "all_search_ready"}:
        return 2
    return 1


def _provider_scope_pairs(payload: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (
            str(row.get("country_code") or "").strip().upper(),
            str(row.get("provider") or "").strip(),
        )
        for row in list(payload.get("targeted_search_matrix") or [])
        if isinstance(row, dict)
        and str(row.get("country_code") or "").strip()
        and str(row.get("provider") or "").strip()
    }


def _provider_matrix_catalog_scope_details(
    provider_matrix: dict[str, Any],
    provider_catalog: dict[str, Any],
) -> dict[str, Any]:
    expected_pairs = _provider_scope_pairs(provider_catalog)
    matrix_pairs = _provider_scope_pairs(provider_matrix)
    if expected_pairs:
        expected_countries = {country for country, _provider in expected_pairs}
        relevant_matrix_pairs = {
            pair for pair in matrix_pairs if pair[0] in expected_countries
        }
        missing_pairs = sorted(expected_pairs - relevant_matrix_pairs)
        unexpected_pairs = sorted(relevant_matrix_pairs - expected_pairs)
        return {
            "checked": True,
            "ok": not missing_pairs and not unexpected_pairs,
            "comparison": "provider_keys",
            "missing_providers": [
                {"country_code": country, "provider": provider}
                for country, provider in missing_pairs
            ],
            "unexpected_providers": [
                {"country_code": country, "provider": provider}
                for country, provider in unexpected_pairs
            ],
        }

    catalog_summary = dict(provider_catalog.get("targeted_search_matrix_summary") or {})
    matrix_summary = dict(provider_matrix.get("targeted_search_matrix_summary") or {})
    expected_counts = {
        str(country or "").strip().upper(): int(count or 0)
        for country, count in dict(
            catalog_summary.get("full_search_ready_provider_count_by_country") or {}
        ).items()
        if str(country or "").strip()
    }
    if not expected_counts:
        return {
            "checked": False,
            "ok": True,
            "comparison": "unavailable",
            "missing_providers": [],
            "unexpected_providers": [],
        }
    matrix_counts = {
        str(country or "").strip().upper(): int(count or 0)
        for country, count in dict(
            matrix_summary.get("full_search_ready_provider_count_by_country") or {}
        ).items()
        if str(country or "").strip().upper() in expected_counts
    }
    mismatches = [
        {
            "country_code": country,
            "expected_count": expected_counts[country],
            "matrix_count": matrix_counts.get(country, 0),
        }
        for country in sorted(expected_counts)
        if matrix_counts.get(country, 0) != expected_counts[country]
    ]
    return {
        "checked": True,
        "ok": not mismatches,
        "comparison": "provider_counts",
        "count_mismatches": mismatches,
        "missing_providers": [],
        "unexpected_providers": [],
    }


def _provider_catalog_smoke_summary(payload: dict[str, Any], receipt_path: Path | None) -> dict[str, Any]:
    if receipt_path is None:
        return {
            "status": "not_configured",
            "raw_status": "",
            "check_count": 0,
            "failed_checks": [],
            "targeted_search_matrix_executed": None,
            "targeted_search_matrix_status": "",
            "receipt_path": "",
            "note": "No separate provider-catalog smoke receipt was supplied.",
        }

    checks = [row for row in list(payload.get("checks") or []) if isinstance(row, dict)]
    failed_checks = [
        {
            "country_code": row.get("country_code"),
            "status": row.get("status") or "unknown",
            "runtime_provider_count_ok": row.get("runtime_provider_count_ok"),
            "runtime_provider_set_ok": row.get("runtime_provider_set_ok"),
            "runtime_missing_providers": list(row.get("runtime_missing_providers") or []),
            "runtime_unexpected_providers": list(row.get("runtime_unexpected_providers") or []),
            "runtime_defaults_present_ok": row.get("runtime_defaults_present_ok"),
            "runtime_provider_country_scope_ok": row.get("runtime_provider_country_scope_ok"),
        }
        for row in checks
        if row.get("status") != "pass"
        or row.get("runtime_provider_count_ok") is False
        or row.get("runtime_provider_set_ok") is False
        or row.get("runtime_defaults_present_ok") is False
        or row.get("runtime_provider_country_scope_ok") is False
    ]
    cross_country_summary = dict(payload.get("cross_country_sanitization_summary") or {})
    cross_country_failed = (
        bool(cross_country_summary)
        and (
            cross_country_summary.get("sanitization_ok") is False
            or int(dict(cross_country_summary.get("status_counts") or {}).get("fail") or 0) > 0
        )
    )
    if cross_country_failed:
        failed_checks.append(
            {
                "country_code": "cross_country_sanitization",
                "status": "fail",
                "runtime_provider_count_ok": None,
                "runtime_defaults_present_ok": None,
                "runtime_provider_country_scope_ok": cross_country_summary.get("sanitization_ok"),
            }
        )

    raw_status = str(payload.get("status") or "unknown")
    missing_or_invalid = raw_status in {"missing", "invalid", "unknown"}
    checks_ok = bool(checks) and not failed_checks and not missing_or_invalid
    return {
        "status": "pass" if checks_ok else "blocked",
        "raw_status": raw_status,
        "check_count": len(checks),
        "failed_checks": failed_checks[:12],
        "targeted_search_matrix_executed": payload.get("targeted_search_matrix_executed"),
        "targeted_search_matrix_status": payload.get("targeted_search_matrix_status"),
        "receipt_path": str(receipt_path),
        "note": "Catalog smoke checks provider/default-provider runtime parity. The strict/soft targeted search matrix is reported under provider_matrix.",
    }


def _latest_receipt_path(patterns: tuple[str, ...], *, fallback: str) -> Path:
    candidates: list[Path] = []
    for pattern in patterns:
        candidates.extend(path for path in Path().glob(pattern) if path.is_file())
    if not candidates:
        return Path(fallback).expanduser().resolve()

    def sort_key(path: Path) -> tuple[int, int, float, float, str]:
        payload = _load_json(path)
        completeness_rank = 1 if _receipt_is_complete_enough(payload) else 0
        provider_matrix_rank = _provider_matrix_proof_rank(payload)
        provider_matrix_scope_rank = _provider_matrix_scope_rank(payload)
        provider_matrix_coverage = _provider_matrix_proof_coverage(payload)
        generated_at, _timestamp_source, _raw_generated_at = _receipt_generated_at(payload)
        generated_timestamp = generated_at.timestamp() if generated_at is not None else 0.0
        try:
            modified_timestamp = path.stat().st_mtime
        except OSError:
            modified_timestamp = 0.0
        return (
            completeness_rank,
            provider_matrix_rank,
            provider_matrix_scope_rank,
            provider_matrix_coverage,
            generated_timestamp,
            modified_timestamp,
            path.as_posix(),
        )

    return max(candidates, key=sort_key).resolve()


def _default_receipt_path(name: str) -> Path:
    if name == "tour_control":
        canonical_path = Path(DEFAULT_RECEIPT_FALLBACKS[name]).expanduser().resolve()
        if canonical_path.is_file() and _receipt_is_complete_enough(_load_json(canonical_path)):
            return canonical_path
    return _latest_receipt_path(
        DEFAULT_RECEIPT_PATTERNS[name],
        fallback=DEFAULT_RECEIPT_FALLBACKS[name],
    )


def _default_receipt_path_if_exists(name: str) -> Path | None:
    path = _default_receipt_path(name)
    return path if path.is_file() else None


_GoldStatusDirectoryChain = list[
    tuple[int, int | None, str | None, tuple[int, int]]
]


def _lexical_gold_status_output_path(path: Path) -> Path:
    """Return an absolute path without dereferencing any path component."""

    output_path = Path(os.path.abspath(os.fspath(path.expanduser())))
    if not output_path.name:
        raise ValueError("Gold status output path must name a file")
    return output_path


def _gold_status_node_identity(metadata: os.stat_result) -> tuple[int, int]:
    return (metadata.st_dev, metadata.st_ino)


def _validate_gold_status_directory(
    metadata: os.stat_result,
    *,
    path: Path,
    final: bool,
) -> None:
    peer_writable = bool(
        metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    )
    sticky = bool(metadata.st_mode & stat.S_ISVTX)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid not in {0, os.geteuid()}
        or (peer_writable and (final or not sticky))
    ):
        raise ValueError(
            "Unsafe Gold status output directory chain; only trusted-owner "
            f"non-final sticky ancestors may be peer-writable: {path}"
        )


def _close_gold_status_directory_chain(chain: _GoldStatusDirectoryChain) -> None:
    for directory_fd, _parent_fd, _name, _identity in reversed(chain):
        with contextlib.suppress(OSError):
            os.close(directory_fd)


def _open_gold_status_output_directory(output_path: Path) -> _GoldStatusDirectoryChain:
    if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("Secure Gold status publication requires O_DIRECTORY and O_NOFOLLOW")

    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    chain: _GoldStatusDirectoryChain = []
    components = output_path.parent.parts[1:]
    try:
        root_fd = os.open(os.sep, directory_flags)
        try:
            root_metadata = os.fstat(root_fd)
            _validate_gold_status_directory(
                root_metadata,
                path=Path(os.sep),
                final=not components,
            )
        except BaseException:
            os.close(root_fd)
            raise
        chain.append((root_fd, None, None, _gold_status_node_identity(root_metadata)))

        current_path = Path(os.sep)
        for component_index, component in enumerate(components):
            current_path /= component
            parent_fd = chain[-1][0]
            created = False
            try:
                directory_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                    created = True
                except FileExistsError:
                    pass
                try:
                    directory_fd = os.open(component, directory_flags, dir_fd=parent_fd)
                except OSError as exc:
                    raise ValueError(
                        f"Unsafe Gold status output parent: {current_path}"
                    ) from exc
            except OSError as exc:
                raise ValueError(
                    f"Unsafe Gold status output parent: {current_path}"
                ) from exc

            try:
                metadata = os.fstat(directory_fd)
                _validate_gold_status_directory(
                    metadata,
                    path=current_path,
                    final=component_index == len(components) - 1,
                )
                named_metadata = os.stat(
                    component,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                identity = _gold_status_node_identity(metadata)
                if _gold_status_node_identity(named_metadata) != identity:
                    raise RuntimeError(
                        f"Gold status output parent changed while opening: {current_path}"
                    )
            except BaseException:
                os.close(directory_fd)
                raise
            chain.append((directory_fd, parent_fd, component, identity))
            if created:
                os.fsync(parent_fd)
        return chain
    except BaseException:
        _close_gold_status_directory_chain(chain)
        raise


def _revalidate_gold_status_directory_chain(
    chain: _GoldStatusDirectoryChain,
    *,
    output_path: Path,
) -> None:
    for index, (
        directory_fd,
        parent_fd,
        component,
        expected_identity,
    ) in enumerate(chain):
        metadata = os.fstat(directory_fd)
        _validate_gold_status_directory(
            metadata,
            path=output_path.parent,
            final=index == len(chain) - 1,
        )
        if _gold_status_node_identity(metadata) != expected_identity:
            raise RuntimeError(
                f"Gold status output parent changed during publication: {output_path.parent}"
            )
        if parent_fd is None or component is None:
            continue
        try:
            named_metadata = os.stat(
                component,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise RuntimeError(
                f"Gold status output parent changed during publication: {output_path.parent}"
            ) from exc
        if (
            not stat.S_ISDIR(named_metadata.st_mode)
            or _gold_status_node_identity(named_metadata) != expected_identity
        ):
            raise RuntimeError(
                f"Gold status output parent changed during publication: {output_path.parent}"
            )


def _validate_gold_status_destination(parent_fd: int, output_path: Path) -> None:
    try:
        metadata = os.stat(
            output_path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
        raise ValueError(f"Unsafe Gold status output path: {output_path}")


def _unlink_gold_status_destination(
    parent_fd: int,
    output_path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    try:
        metadata = os.stat(
            output_path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    if expected_identity is not None and (
        _gold_status_node_identity(metadata) != expected_identity
    ):
        return
    if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        os.unlink(output_path.name, dir_fd=parent_fd)


def _write_all(file_descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(file_descriptor, remaining)
        if written <= 0:
            raise OSError("Gold status output write made no progress")
        remaining = remaining[written:]


def _publish_private_text_at(
    output_path: Path,
    rendered: str,
    directory_chain: _GoldStatusDirectoryChain,
) -> tuple[int, int]:
    parent_fd = directory_chain[-1][0]
    _revalidate_gold_status_directory_chain(
        directory_chain,
        output_path=output_path,
    )
    _validate_gold_status_destination(parent_fd, output_path)

    temporary_name = ""
    file_descriptor = -1
    for _attempt in range(128):
        temporary_name = f".propertyquarry-gold-{secrets.token_hex(16)}.tmp"
        try:
            file_descriptor = os.open(
                temporary_name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                stat.S_IRUSR | stat.S_IWUSR,
                dir_fd=parent_fd,
            )
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError("Unable to allocate a private Gold status staging file")

    published = False
    published_identity: tuple[int, int] | None = None
    try:
        os.fchmod(file_descriptor, stat.S_IRUSR | stat.S_IWUSR)
        _write_all(file_descriptor, rendered.encode("utf-8"))
        os.fsync(file_descriptor)

        staged_metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(staged_metadata.st_mode) or staged_metadata.st_nlink != 1:
            raise RuntimeError("Unsafe Gold status staging file")
        published_identity = _gold_status_node_identity(staged_metadata)
        named_staged_metadata = os.stat(
            temporary_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if _gold_status_node_identity(named_staged_metadata) != published_identity:
            raise RuntimeError("Gold status staging file changed during publication")

        _revalidate_gold_status_directory_chain(
            directory_chain,
            output_path=output_path,
        )
        _validate_gold_status_destination(parent_fd, output_path)
        named_staged_metadata = os.stat(
            temporary_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if _gold_status_node_identity(named_staged_metadata) != published_identity:
            raise RuntimeError("Gold status staging file changed during publication")

        os.replace(
            temporary_name,
            output_path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        published = True
        published_metadata = os.stat(
            output_path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(published_metadata.st_mode)
            or _gold_status_node_identity(published_metadata) != published_identity
        ):
            raise RuntimeError("Gold status output changed during publication")
        _revalidate_gold_status_directory_chain(
            directory_chain,
            output_path=output_path,
        )
        os.fsync(parent_fd)
        return published_identity
    except BaseException:
        if published and published_identity is not None:
            with contextlib.suppress(OSError):
                _unlink_gold_status_destination(
                    parent_fd,
                    output_path,
                    expected_identity=published_identity,
                )
            with contextlib.suppress(OSError):
                os.fsync(parent_fd)
        raise
    finally:
        if file_descriptor >= 0:
            with contextlib.suppress(OSError):
                os.close(file_descriptor)
        if temporary_name:
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=parent_fd)


def _canonical_gold_status_alias_targets(output_path: Path) -> list[Path]:
    lexical_output = _lexical_gold_status_output_path(output_path)
    latest_targets = [
        _lexical_gold_status_output_path(Path(path))
        for path in _CANONICAL_GOLD_STATUS_LATEST_PATHS
    ]
    release_gate_target = _lexical_gold_status_output_path(
        Path(_CANONICAL_GOLD_STATUS_RELEASE_GATE_PATH)
    )

    if lexical_output == release_gate_target:
        return [path for path in latest_targets if path != lexical_output]
    if lexical_output in latest_targets:
        return [path for path in latest_targets if path != lexical_output]
    return []


def _clear_gold_status_publication(
    opened_targets: list[tuple[Path, _GoldStatusDirectoryChain]],
) -> None:
    for output_path, directory_chain in opened_targets:
        parent_fd = directory_chain[-1][0]
        with contextlib.suppress(OSError):
            _unlink_gold_status_destination(parent_fd, output_path)
        with contextlib.suppress(OSError):
            os.fsync(parent_fd)


def _write_gold_status_output(output_path: Path, output: str) -> list[str]:
    lexical_output = _lexical_gold_status_output_path(output_path)
    alias_targets = _canonical_gold_status_alias_targets(lexical_output)
    targets = [lexical_output, *alias_targets]
    rendered = output if output.endswith("\n") else f"{output}\n"

    opened_targets: list[tuple[Path, _GoldStatusDirectoryChain]] = []
    published_identities: list[tuple[int, int]] = []
    publication_started = False
    try:
        for target in targets:
            opened_targets.append(
                (target, _open_gold_status_output_directory(target))
            )
        for target, directory_chain in opened_targets:
            _revalidate_gold_status_directory_chain(
                directory_chain,
                output_path=target,
            )
            _validate_gold_status_destination(directory_chain[-1][0], target)

        for target, directory_chain in opened_targets:
            publication_started = True
            published_identities.append(
                _publish_private_text_at(target, rendered, directory_chain)
            )

        for (target, directory_chain), expected_identity in zip(
            opened_targets,
            published_identities,
            strict=True,
        ):
            _revalidate_gold_status_directory_chain(
                directory_chain,
                output_path=target,
            )
            published_metadata = os.stat(
                target.name,
                dir_fd=directory_chain[-1][0],
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(published_metadata.st_mode)
                or _gold_status_node_identity(published_metadata)
                != expected_identity
            ):
                raise RuntimeError(
                    f"Gold status output changed during canonical publication: {target}"
                )
        return [str(path) for path in alias_targets]
    except BaseException:
        if publication_started and len(opened_targets) > 1:
            _clear_gold_status_publication(opened_targets)
        raise
    finally:
        for _target, directory_chain in reversed(opened_targets):
            _close_gold_status_directory_chain(directory_chain)


def _atomic_write_private_text(path: Path, rendered: str) -> None:
    output_path = _lexical_gold_status_output_path(path)
    directory_chain = _open_gold_status_output_directory(output_path)
    try:
        _publish_private_text_at(output_path, rendered, directory_chain)
    finally:
        _close_gold_status_directory_chain(directory_chain)


def _parse_receipt_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _exact_json_equal(actual: object, expected: object) -> bool:
    """Compare JSON-like values without Python's bool/int equality aliasing."""
    if type(actual) is not type(expected):
        return False
    if type(expected) is dict:
        actual_dict = actual
        expected_dict = expected
        return set(actual_dict) == set(expected_dict) and all(
            _exact_json_equal(actual_dict[key], expected_dict[key])
            for key in expected_dict
        )
    if type(expected) is list:
        actual_list = actual
        expected_list = expected
        return len(actual_list) == len(expected_list) and all(
            _exact_json_equal(actual_item, expected_item)
            for actual_item, expected_item in zip(actual_list, expected_list)
        )
    return actual == expected


def _open_verified_browser_parent(path: Path) -> tuple[int, str]:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, directory_flags)
        for component in (None, *path.parent.parts[1:]):
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
                os.close(descriptor)
                return -1, "browser_executable_directory_chain_unsafe"
        return descriptor, ""
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        return -1, f"browser_executable_directory_chain_failed:{type(exc).__name__}"


def _verified_regular_file_identity(path_value: object) -> tuple[dict[str, object], str]:
    """Hash an immutable snapshot of a regular file without following its final link."""
    raw_path = str(path_value or "").strip()
    path = Path(raw_path)
    if not raw_path or not path.is_absolute():
        return {}, "browser_executable_path_not_absolute"
    if os.path.normpath(raw_path) != raw_path or os.path.realpath(raw_path) != raw_path:
        return {}, "browser_executable_path_not_canonical"
    parent_descriptor, parent_error = _open_verified_browser_parent(path)
    if parent_error:
        return {}, parent_error
    descriptor = -1
    try:
        before = os.stat(
            path.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        os.close(parent_descriptor)
        return {}, f"browser_executable_lstat_failed:{type(exc).__name__}"
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        os.close(parent_descriptor)
        return {}, "browser_executable_not_regular_nonsymlink"
    if stat.S_IMODE(before.st_mode) & 0o111 == 0:
        os.close(parent_descriptor)
        return {}, "browser_executable_not_executable"
    if (
        before.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(before.st_mode) & 0o022
        or before.st_nlink != 1
    ):
        os.close(parent_descriptor)
        return {}, "browser_executable_ownership_or_mode_unsafe"
    if (
        before.st_size < AUTHENTICATED_PERFORMANCE_MIN_EXECUTABLE_BYTES
        or before.st_size > AUTHENTICATED_PERFORMANCE_MAX_EXECUTABLE_BYTES
    ):
        os.close(parent_descriptor)
        return {}, "browser_executable_size_out_of_range"
    if path.name.lower() not in {
        "chrome",
        "chromium",
        "headless_shell",
        "chrome-headless-shell",
    } or not any(
        "chrom" in component.lower() for component in path.parts[:-1]
    ):
        os.close(parent_descriptor)
        return {}, "browser_executable_path_not_chromium"

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
    except OSError as exc:
        os.close(parent_descriptor)
        return {}, f"browser_executable_open_failed:{type(exc).__name__}"
    digest = hashlib.sha256()
    prefix = b""
    marker_window = b""
    chromium_marker_observed = False
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            return {}, "browser_executable_changed_before_hash"
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
            return {}, "browser_executable_changed_during_hash"
    except OSError as exc:
        return {}, f"browser_executable_hash_failed:{type(exc).__name__}"
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)
    if prefix != b"\x7fELF" or not chromium_marker_observed:
        return {}, "browser_executable_not_chromium_elf"
    return {
        "executable_path": raw_path,
        "executable_sha256": digest.hexdigest(),
        "executable_bytes": before.st_size,
    }, ""


def _receipt_generated_at(payload: dict[str, Any]) -> tuple[datetime | None, str, str]:
    candidates: tuple[tuple[str, object], ...] = (
        ("generated_at", payload.get("generated_at")),
        ("updated_at", payload.get("updated_at")),
        ("completed_at", payload.get("completed_at")),
        ("repair_summary.generated_at", (payload.get("repair_summary") or {}).get("generated_at") if isinstance(payload.get("repair_summary"), dict) else ""),
        ("summary.generated_at", (payload.get("summary") or {}).get("generated_at") if isinstance(payload.get("summary"), dict) else ""),
    )
    for source, value in candidates:
        parsed = _parse_receipt_datetime(value)
        if parsed is not None:
            return parsed, source, str(value or "").strip()
    return None, "", ""


def _receipt_freshness_status(
    receipts: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
    max_age_hours: float | None = None,
) -> tuple[bool, list[dict[str, Any]]]:
    if max_age_hours is None:
        return True, []
    if not math.isfinite(max_age_hours):
        return False, [
            {
                "area": "receipt_freshness",
                "status": "invalid_max_age_hours",
                "max_age_hours": max_age_hours,
            }
        ]
    if max_age_hours <= 0:
        return True, []
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows: list[dict[str, Any]] = []
    for area, payload in receipts.items():
        generated_at, timestamp_source, raw_generated_at = _receipt_generated_at(payload)
        if generated_at is None:
            rows.append(
                {
                    "area": area,
                    "status": "missing_or_invalid_generated_at",
                    "generated_at": str(payload.get("generated_at") or ""),
                }
            )
            continue
        age_seconds = (current_time - generated_at).total_seconds()
        if age_seconds < -30.0:
            rows.append(
                {
                    "area": area,
                    "status": "future_dated",
                    "generated_at": generated_at.isoformat(),
                    "timestamp_source": timestamp_source,
                    "raw_generated_at": raw_generated_at,
                    "future_seconds": round(-age_seconds, 2),
                    "maximum_future_skew_seconds": 30,
                }
            )
            continue
        age_seconds = max(0.0, age_seconds)
        age_hours = age_seconds / 3600.0
        if age_hours > max_age_hours:
            rows.append(
                {
                    "area": area,
                    "status": "stale",
                    "generated_at": generated_at.isoformat(),
                    "timestamp_source": timestamp_source,
                    "raw_generated_at": raw_generated_at,
                    "age_hours": round(age_hours, 2),
                    "max_age_hours": max_age_hours,
                }
            )
    return not rows, rows


def _slo_evidence_status(
    payload: dict[str, Any],
    *,
    receipt_present: bool,
    expected_release_commit_sha: str,
    expected_release_image_digest: str,
    now: datetime | None,
    max_age_seconds: int,
) -> tuple[bool, dict[str, Any]]:
    configured_max_age = min(
        DEFAULT_SLO_EVIDENCE_MAX_AGE_SECONDS,
        max(1, int(max_age_seconds or DEFAULT_SLO_EVIDENCE_MAX_AGE_SECONDS)),
    )
    probe = dict(payload.get("probe") or {}) if isinstance(payload.get("probe"), dict) else {}
    promtool = (
        dict(payload.get("promtool") or {})
        if isinstance(payload.get("promtool"), dict)
        else {}
    )
    amtool = (
        dict(payload.get("amtool") or {})
        if isinstance(payload.get("amtool"), dict)
        else {}
    )
    prometheus_range = (
        dict(payload.get("prometheus_range") or {})
        if isinstance(payload.get("prometheus_range"), dict)
        else {}
    )
    expected_sha = str(expected_release_commit_sha or "").strip().lower()
    expected_digest = str(expected_release_image_digest or "").strip().lower()
    observed_sha = str(payload.get("release_commit_sha") or "").strip().lower()
    observed_digest = str(payload.get("release_image_digest") or "").strip().lower()
    window_start = _parse_receipt_datetime(probe.get("window_start"))
    captured_at = _parse_receipt_datetime(probe.get("window_end"))
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = (
        (current_time - captured_at).total_seconds()
        if captured_at is not None
        else None
    )
    replica_ids = (
        [str(value or "").strip() for value in probe.get("replica_ids", [])]
        if isinstance(probe.get("replica_ids"), list)
        else []
    )
    try:
        replica_count = int(probe.get("replica_count") or 0)
    except (TypeError, ValueError):
        replica_count = 0
    try:
        window_seconds = float(probe.get("window_seconds"))
    except (TypeError, ValueError):
        window_seconds = math.nan

    def valid_sha256(value: object) -> bool:
        normalized = str(value or "").strip().lower()
        return len(normalized) == 64 and all(
            character in "0123456789abcdef" for character in normalized
        )

    errors: list[str] = []
    if not receipt_present:
        errors.append("receipt_missing")
    if payload.get("schema") != SLO_EVIDENCE_RECEIPT_SCHEMA:
        errors.append("receipt_schema_invalid")
    if payload.get("status") != "pass" or payload.get("gate_passed") is not True:
        errors.append("offline_slo_gate_not_passed")
    if payload.get("mode") != "flagship":
        errors.append("flagship_mode_not_proven")
    if payload.get("live_monitoring_contacted") is not False:
        errors.append("offline_evidence_boundary_invalid")
    if len(expected_sha) != 40 or any(character not in "0123456789abcdef" for character in expected_sha):
        errors.append("expected_release_commit_sha_invalid")
    elif observed_sha != expected_sha:
        errors.append("release_commit_sha_mismatch")
    if (
        not expected_digest.startswith("sha256:")
        or len(expected_digest) != 71
        or any(character not in "0123456789abcdef" for character in expected_digest[7:])
    ):
        errors.append("expected_release_image_digest_invalid")
    elif observed_digest != expected_digest:
        errors.append("release_image_digest_mismatch")
    if str(probe.get("release_commit_sha") or "").strip().lower() != observed_sha:
        errors.append("probe_release_commit_sha_mismatch")
    if str(probe.get("release_image_digest") or "").strip().lower() != observed_digest:
        errors.append("probe_release_image_digest_mismatch")
    if probe.get("schema") != "propertyquarry.metrics_snapshot_bundle.v2":
        errors.append("probe_snapshot_bundle_schema_invalid")
    if probe.get("probe_schema") != "propertyquarry.metrics_probe_bundle.v2":
        errors.append("probe_bundle_schema_invalid")
    if window_start is None or captured_at is None or captured_at <= window_start:
        errors.append("probe_window_invalid")
    elif not math.isfinite(window_seconds) or abs(
        window_seconds - (captured_at - window_start).total_seconds()
    ) > 0.001:
        errors.append("probe_window_seconds_invalid")
    elif window_seconds < 1:
        errors.append("probe_window_too_short")
    if captured_at is None:
        errors.append("probe_window_end_invalid")
    elif age_seconds is not None and age_seconds < -60:
        errors.append("probe_window_end_in_future")
    elif age_seconds is not None and age_seconds > configured_max_age:
        errors.append("probe_stale")
    if probe.get("credential_persisted") is not False:
        errors.append("probe_credential_boundary_invalid")
    if (
        replica_count <= 0
        or len(replica_ids) != replica_count
        or replica_ids != sorted(set(replica_ids))
        or any(not value or len(value) > 128 for value in replica_ids)
    ):
        errors.append("probe_replica_coverage_invalid")
    if not valid_sha256(probe.get("snapshot_bundle_sha256")):
        errors.append("probe_snapshot_bundle_sha256_invalid")
    if not valid_sha256(probe.get("probe_bundle_sha256")):
        errors.append("probe_bundle_sha256_invalid")

    range_start = _parse_receipt_datetime(prometheus_range.get("window_start"))
    range_end = _parse_receipt_datetime(prometheus_range.get("window_end"))
    try:
        range_window_seconds = float(prometheus_range.get("window_seconds"))
    except (TypeError, ValueError):
        range_window_seconds = math.nan
    range_replica_ids = (
        [str(value or "").strip() for value in prometheus_range.get("replica_ids", [])]
        if isinstance(prometheus_range.get("replica_ids"), list)
        else []
    )
    range_slo = (
        dict(prometheus_range.get("slo") or {})
        if isinstance(prometheus_range.get("slo"), dict)
        else {}
    )
    if prometheus_range.get("schema") != RANGE_RECEIPT_SCHEMA:
        errors.append("prometheus_range_schema_invalid")
    if prometheus_range.get("producer") != "propertyquarry-prometheus-range-capture":
        errors.append("prometheus_range_producer_invalid")
    if (
        prometheus_range.get("authenticated") is not True
        or prometheus_range.get("tls_verified") is not True
        or prometheus_range.get("credential_persisted") is not False
    ):
        errors.append("prometheus_range_transport_invalid")
    if (
        range_start is None
        or range_end is None
        or range_end <= range_start
        or not math.isfinite(range_window_seconds)
        or abs(range_window_seconds - (range_end - range_start).total_seconds()) > 0.001
        or range_window_seconds < 30 * 24 * 60 * 60
    ):
        errors.append("prometheus_range_window_invalid")
    if range_replica_ids != replica_ids:
        errors.append("prometheus_range_replica_coverage_mismatch")
    if not valid_sha256(prometheus_range.get("range_response_sha256")):
        errors.append("prometheus_range_response_sha256_invalid")
    if not valid_sha256(prometheus_range.get("receipt_sha256")):
        errors.append("prometheus_range_receipt_sha256_invalid")
    if range_slo.get("status") != "pass":
        errors.append("prometheus_range_slo_not_passed")
    if (
        promtool.get("available") is not True
        or promtool.get("version_pinned") is not True
        or promtool.get("rule_check_passed") is not True
        or promtool.get("config_check_passed") is not True
        or promtool.get("injection_test_passed") is not True
    ):
        errors.append("promtool_evidence_incomplete")
    if (
        amtool.get("available") is not True
        or amtool.get("version_pinned") is not True
        or amtool.get("routing_check_passed") is not True
    ):
        errors.append("amtool_evidence_incomplete")
    return not errors, {
        "status": "pass" if not errors else "blocked",
        "errors": errors,
        "release_commit_sha": observed_sha,
        "release_image_digest": observed_digest,
        "captured_at": str(probe.get("window_end") or ""),
        "window_start": str(probe.get("window_start") or ""),
        "window_end": str(probe.get("window_end") or ""),
        "window_seconds": window_seconds if math.isfinite(window_seconds) else None,
        "age_seconds": round(max(0.0, age_seconds), 3) if age_seconds is not None else None,
        "max_age_seconds": configured_max_age,
        "authenticated": prometheus_range.get("authenticated") is True,
        "private_route": not errors,
        "no_store": not errors,
        "credential_persisted": probe.get("credential_persisted"),
        "replica_id": replica_ids[0] if len(replica_ids) == 1 else "",
        "replica_ids": replica_ids,
        "replica_count": replica_count,
        "prometheus_range_window_seconds": (
            range_window_seconds if math.isfinite(range_window_seconds) else None
        ),
        "promtool_rule_check_passed": promtool.get("rule_check_passed") is True,
        "promtool_config_check_passed": promtool.get("config_check_passed") is True,
        "promtool_injection_test_passed": promtool.get("injection_test_passed") is True,
        "amtool_routing_check_passed": amtool.get("routing_check_passed") is True,
    }


def _normalize_readiness_profile(value: str) -> str:
    normalized = str(value or "standard").strip().lower().replace("-", "_")
    if normalized in {"", "standard", "development", "default"}:
        return "standard"
    if normalized == "flagship":
        return "flagship"
    if normalized == "launch":
        return "launch"
    raise ValueError(f"unsupported_propertyquarry_readiness_profile:{normalized}")


def _normalize_claim_scope(value: str) -> str:
    normalized = str(value or "core").strip().lower().replace("-", "_")
    if normalized in {"", "core"}:
        return "core"
    if normalized in {"advanced", "advanced_visual"}:
        return "advanced_visual"
    raise ValueError(f"unsupported_propertyquarry_claim_scope:{normalized}")


def _resolve_readiness_request(
    readiness_profile: str,
    claim_scope: str | None,
) -> tuple[str, str, str]:
    requested = str(readiness_profile or "standard").strip().lower().replace("-", "_")
    alias_scopes = {
        "core_gold": "core",
        "advanced_visual_gold": "advanced_visual",
    }
    if requested in alias_scopes:
        alias_scope = alias_scopes[requested]
        if claim_scope is not None and _normalize_claim_scope(claim_scope) != alias_scope:
            raise ValueError(
                f"propertyquarry_gold_alias_scope_conflict:{requested}:{claim_scope}"
            )
        return "launch", alias_scope, requested
    return (
        _normalize_readiness_profile(requested),
        _normalize_claim_scope(claim_scope or "core"),
        requested or "standard",
    )


def _magicfit_playback_receipt_ok(
    playback: dict[str, Any],
    *,
    required: bool,
) -> bool:
    if not required:
        return True
    try:
        ready_count = int(playback.get("ready_count") or 0)
        playable_count = int(playback.get("playable_count") or 0)
    except (TypeError, ValueError):
        return False
    evidence = [
        dict(row)
        for row in list(playback.get("evidence") or [])
        if isinstance(row, dict)
    ]
    identities: set[tuple[str, str]] = set()
    for row in evidence:
        slug = str(row.get("slug") or "").strip()
        provider = str(row.get("provider") or "").strip().lower()
        media_identity = str(row.get("media_identity") or "").strip()
        control_path = str(row.get("control_path") or "").strip()
        if (
            not slug
            or provider != "magicfit"
            or not media_identity
            or media_identity != control_path
        ):
            return False
        identities.add((slug, media_identity))
    return (
        playback.get("playback_ok") is True
        and ready_count > 0
        and playable_count == ready_count
        and len(evidence) == ready_count
        and len(identities) == ready_count
    )


def _missing_provider_modes(tour_receipt: dict[str, Any]) -> list[str]:
    required_modes = {
        str(provider or "").strip().lower()
        for provider in list(tour_receipt.get("required_provider_modes") or [])
        if str(provider or "").strip()
    } or set(REQUIRED_TOUR_PROVIDER_MODES)
    required_modes.intersection_update(REQUIRED_TOUR_PROVIDER_MODES)
    if not required_modes:
        required_modes = set(REQUIRED_TOUR_PROVIDER_MODES)
    ready = {
        str(provider or "").strip().lower()
        for provider in list(tour_receipt.get("ready_provider_modes") or [])
        if str(provider or "").strip()
    }
    missing = [
        provider
        for provider in REQUIRED_TOUR_PROVIDER_MODES
        if provider in required_modes and provider not in ready
    ]
    explicit_missing = [
        str(provider or "").strip().lower()
        for provider in list(tour_receipt.get("missing_provider_modes") or [])
        if str(provider or "").strip().lower() in required_modes
    ]
    for provider in explicit_missing:
        if provider not in missing:
            missing.append(provider)
    return missing


def _missing_profile_provider_modes(
    tour_receipt: dict[str, Any],
    *,
    required_field: str,
    missing_field: str,
    default_required_modes: tuple[str, ...],
) -> list[str]:
    required_modes = tuple(
        provider
        for provider in (
            str(value or "").strip().lower()
            for value in list(tour_receipt.get(required_field) or default_required_modes)
        )
        if provider in default_required_modes
    ) or default_required_modes
    ready_modes = {
        str(value or "").strip().lower()
        for value in list(tour_receipt.get("ready_provider_modes") or [])
        if str(value or "").strip()
    }
    explicit_missing = {
        str(value or "").strip().lower()
        for value in list(tour_receipt.get(missing_field) or [])
        if str(value or "").strip().lower() in required_modes
    }
    return [
        provider
        for provider in required_modes
        if provider not in ready_modes or provider in explicit_missing
    ]


def _tour_provider_evidence_action(missing_provider_modes: list[str]) -> str:
    actions = {
        "matterport": "verified hosted 3D model URL/control receipt",
        "3dvista": "verified branded 3D tour export or allowlisted hosted tour URL",
        "pano2vr": "verified panorama tour export",
        "krpano": "optional operator-only panorama scene evidence",
        "magicfit": "receipt-backed playable walkthrough",
    }
    provider_labels = {
        "matterport": "Matterport",
        "3dvista": "3DVista",
        "pano2vr": "Pano2VR",
        "krpano": "krpano",
        "magicfit": "walkthrough",
    }
    parts = [
        f"{provider_labels.get(provider, provider)}: {actions[provider]}"
        for provider in missing_provider_modes
        if provider in actions
    ]
    if not parts:
        return "rerun verify_property_tour_controls.py and attach any provider evidence it still reports missing"
    return f"attach real provider evidence for missing modes only: {', '.join(parts)}"


def _tour_provider_missing_note(missing_provider_modes: list[str]) -> str:
    provider_labels = {
        "matterport": "Matterport",
        "3dvista": "3DVista",
        "pano2vr": "Pano2VR",
        "krpano": "krpano",
        "magicfit": "MagicFit",
    }
    labels = {
        "matterport": "Matterport hosted 3D model",
        "3dvista": "3DVista branded 3D tour export",
        "pano2vr": "Pano2VR panorama tour export",
        "krpano": "optional krpano panorama/cube viewer",
        "magicfit": "MagicFit playable walkthrough",
    }
    providers = [provider_labels[provider] for provider in missing_provider_modes if provider in provider_labels]
    names = [labels[provider] for provider in missing_provider_modes if provider in labels]
    if not names:
        return "This receipt has no missing tour provider modes in the active verifier output."
    provider_joined = ", ".join(providers)
    joined = ", ".join(names)
    return f"This receipt intentionally treats missing {provider_joined} evidence ({joined}) as blocked rather than pass."


def _magicfit_renderer_summary(vendor_tooling: dict[str, Any], *, receipt_present: bool) -> dict[str, Any]:
    renderer = dict(vendor_tooling.get("magicfit_renderer") or {}) if receipt_present else {}
    python_modules = renderer.get("python_modules") if isinstance(renderer.get("python_modules"), dict) else {}
    missing_python_modules = [
        module
        for module, status in python_modules.items()
        if not isinstance(status, dict) or not bool(status.get("available"))
    ]
    return {
        "status": str(renderer.get("status") or ("not_configured" if not receipt_present else "missing")),
        "script_path": str(renderer.get("script_path") or "") if receipt_present else "",
        "script_ready": (
            bool(renderer.get("script_ready"))
            if receipt_present and "script_ready" in renderer
            else None
        ),
        "credentials_configured": (
            bool(renderer.get("credentials_configured"))
            if receipt_present and "credentials_configured" in renderer
            else None
        ),
        "credential_sources": list(renderer.get("credential_sources") or []) if receipt_present else [],
        "env_files_checked": list(renderer.get("env_files_checked") or []) if receipt_present else [],
        "python_modules_ready": (
            bool(renderer.get("python_modules_ready"))
            if receipt_present and "python_modules_ready" in renderer
            else None
        ),
        "python_modules": python_modules,
        "missing_python_modules": missing_python_modules,
        "ready": bool(renderer.get("ready")) if receipt_present and "ready" in renderer else None,
        "next_action": str(renderer.get("next_action") or "") if receipt_present else "",
        "note": str(renderer.get("note") or "") if receipt_present else "",
    }


def _omagic_adapter_summary(vendor_tooling: dict[str, Any], *, receipt_present: bool) -> dict[str, Any]:
    adapter = dict(vendor_tooling.get("omagic_adapter") or {}) if receipt_present else {}
    runtime_script = dict(adapter.get("runtime_script") or {})
    return {
        "status": str(adapter.get("status") or ("not_configured" if not receipt_present else "missing")),
        "ready": bool(adapter.get("ready")) if receipt_present and "ready" in adapter else None,
        "script_path": str(adapter.get("script_path") or "") if receipt_present else "",
        "script_ready": bool(adapter.get("script_ready")) if receipt_present and "script_ready" in adapter else None,
        "runtime_checked": bool(adapter.get("runtime_checked")) if receipt_present and "runtime_checked" in adapter else False,
        "runtime_script_ready": adapter.get("runtime_script_ready") if receipt_present else None,
        "runtime_script": runtime_script,
        "model_upload_enable_env": str(adapter.get("model_upload_enable_env") or "") if receipt_present else "",
        "model_upload_adapter_enabled": bool(adapter.get("model_upload_adapter_enabled")) if receipt_present and "model_upload_adapter_enabled" in adapter else None,
        "render_endpoint_env_names": list(adapter.get("render_endpoint_env_names") or []) if receipt_present else [],
        "render_command_env_names": list(adapter.get("render_command_env_names") or []) if receipt_present else [],
        "render_target_configured": bool(adapter.get("render_target_configured")) if receipt_present and "render_target_configured" in adapter else None,
        "credential_env_names": list(adapter.get("credential_env_names") or []) if receipt_present else [],
        "next_action": str(adapter.get("next_action") or "") if receipt_present else "",
        "note": str(adapter.get("note") or "") if receipt_present else "",
    }


def _performance_research_detail_checks(performance: dict[str, Any]) -> tuple[bool, list[str], str]:
    for row in list(performance.get("routes") or []):
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").split("?", 1)[0]
        if not path.startswith("/app/research/"):
            continue
        passed_checks = {
            str(check.get("name") or "")
            for check in list(row.get("checks") or [])
            if isinstance(check, dict) and check.get("ok") is True
        }
        missing = [name for name in REQUIRED_RESEARCH_PERFORMANCE_CHECKS if name not in passed_checks]
        return (not missing, missing, path)
    return (False, list(REQUIRED_RESEARCH_PERFORMANCE_CHECKS), "")


def _performance_search_checks(performance: dict[str, Any]) -> tuple[bool, list[str], str]:
    for row in list(performance.get("routes") or []):
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").split("?", 1)[0]
        if path != "/app/search":
            continue
        passed_checks = {
            str(check.get("name") or "")
            for check in list(row.get("checks") or [])
            if isinstance(check, dict) and check.get("ok") is True
        }
        missing = [name for name in REQUIRED_SEARCH_PERFORMANCE_CHECKS if name not in passed_checks]
        return (not missing, missing, path)
    return (False, list(REQUIRED_SEARCH_PERFORMANCE_CHECKS), "")


def _performance_analytics_checks(performance: dict[str, Any]) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]], int]:
    missing_by_route: list[dict[str, Any]] = []
    failed_checks: list[dict[str, Any]] = []
    routes_with_analytics_checks = 0

    for row in list(performance.get("routes") or []):
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or "").split("?", 1)[0].strip()
        checks = [check for check in list(row.get("checks") or []) if isinstance(check, dict)]
        rybbit_checks = [
            check
            for check in checks
            if str(check.get("name") or "").startswith("rybbit_")
        ]
        if not rybbit_checks:
            continue

        routes_with_analytics_checks += 1
        passed = {
            str(check.get("name") or "")
            for check in rybbit_checks
            if check.get("ok") is True
        }
        missing = [name for name in REQUIRED_RYBBIT_PERFORMANCE_CHECKS if name not in passed]
        if missing:
            missing_by_route.append({"path": path, "missing_checks": missing})
        failed_checks.extend(
            {
                "path": path,
                "name": str(check.get("name") or "unnamed_check"),
                "detail": str(check.get("detail") or ""),
            }
            for check in rybbit_checks
            if check.get("ok") is not True
        )

    if routes_with_analytics_checks == 0:
        missing_by_route.append(
            {
                "path": "*",
                "missing_checks": list(REQUIRED_RYBBIT_PERFORMANCE_CHECKS),
            }
        )
    return not missing_by_route and not failed_checks, missing_by_route, failed_checks, routes_with_analytics_checks


def _exact_public_https_origin(value: object) -> tuple[str, str, int] | None:
    if (
        type(value) is not str
        or value != value.strip()
        or not value
        or len(value) > 2_048
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return None
    hostname = str(parsed.hostname or "")
    labels = hostname.split(".")
    dns_valid = (
        hostname == hostname.lower()
        and hostname == hostname.rstrip(".")
        and len(labels) >= 2
        and all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        )
    )
    try:
        ipaddress.ip_address(hostname)
        is_ip_literal = True
    except ValueError:
        is_ip_literal = False
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path != ""
        or parsed.query
        or parsed.fragment
        or parsed.netloc != hostname
        or value != f"https://{hostname}"
        or is_ip_literal
        or not dns_valid
        or AUTHENTICATED_PERFORMANCE_PLACEHOLDER_HOST_RE.search(hostname)
        or not (
            hostname == "propertyquarry.com"
            or hostname.endswith(".propertyquarry.com")
        )
    ):
        return None
    return ("https", hostname, 443)


def _flagship_authenticated_performance_proof(
    performance: dict[str, Any],
    *,
    expected_public_origin: str = "",
    expected_release_commit_sha: str = "",
    expected_release_image_digest: str = "",
    expected_release_deployment_id: str = "",
    expected_release_manifest_sha256: str = "",
    expected_chromium_executable_path: str = "",
    expected_chromium_executable_sha256: str = "",
) -> tuple[bool, dict[str, Any]]:
    errors: list[str] = []

    def reject(reason: str) -> None:
        if reason not in errors:
            errors.append(reason)

    def normalized_origin(value: object) -> tuple[str, str, int] | None:
        try:
            parsed = urlsplit(str(value or "").strip())
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        return (
            parsed.scheme.lower(),
            parsed.hostname.lower(),
            port or (443 if parsed.scheme.lower() == "https" else 80),
        )

    raw_expected_manifest_sha256 = expected_release_manifest_sha256
    normalized_expected_manifest_sha256 = str(
        expected_release_manifest_sha256 or ""
    ).strip().lower()
    expected_release_identity = {
        "commit_sha": str(expected_release_commit_sha or "").strip().lower(),
        "image_digest": str(expected_release_image_digest or "").strip().lower(),
        "deployment_id": str(expected_release_deployment_id or "").strip(),
        "manifest_sha256": normalized_expected_manifest_sha256,
    }
    if AUTHENTICATED_PERFORMANCE_RELEASE_COMMIT_RE.fullmatch(
        expected_release_identity["commit_sha"]
    ) is None:
        reject("expected_release_commit_sha_invalid")
    if AUTHENTICATED_PERFORMANCE_RELEASE_IMAGE_RE.fullmatch(
        expected_release_identity["image_digest"]
    ) is None:
        reject("expected_release_image_digest_invalid")
    if AUTHENTICATED_PERFORMANCE_DEPLOYMENT_ID_RE.fullmatch(
        expected_release_identity["deployment_id"]
    ) is None:
        reject("expected_release_deployment_id_invalid")
    if (
        type(raw_expected_manifest_sha256) is not str
        or raw_expected_manifest_sha256 != normalized_expected_manifest_sha256
        or re.fullmatch(r"[0-9a-f]{64}", normalized_expected_manifest_sha256)
        is None
        or len(set(normalized_expected_manifest_sha256)) == 1
    ):
        reject("expected_release_manifest_sha256_invalid")
    normalized_expected_chromium_path = str(
        expected_chromium_executable_path or ""
    ).strip()
    normalized_expected_chromium_sha256 = str(
        expected_chromium_executable_sha256 or ""
    ).strip().lower()
    if not normalized_expected_chromium_path or not Path(
        normalized_expected_chromium_path
    ).is_absolute():
        reject("expected_chromium_executable_path_invalid")
    if re.fullmatch(
        r"[0-9a-f]{64}", normalized_expected_chromium_sha256
    ) is None or len(set(normalized_expected_chromium_sha256)) == 1:
        reject("expected_chromium_executable_sha256_invalid")

    if type(performance) is not dict or set(performance) != {
        "schema",
        "status",
        "status_scope",
        "flagship_status",
        "flagship_blockers",
        "generated_at",
        "release_identity",
        "principal_id",
        "run_id",
        "route_count",
        "failed_count",
        "thresholds",
        "server_request_evidence",
        "routes",
        "constrained_client_evidence",
        "claims",
        "notes",
    }:
        reject("performance_receipt_fields_not_exact")

    if performance.get("schema") != AUTHENTICATED_PERFORMANCE_FLAGSHIP_SCHEMA:
        reject("schema_must_be_authenticated_performance_v2")
    if performance.get("status") != "pass":
        reject("legacy_authenticated_route_status_not_pass")
    if performance.get("flagship_status") != "pass":
        reject("flagship_status_not_pass")
    if performance.get("flagship_blockers") != []:
        reject("flagship_blockers_not_empty")
    if performance.get("status_scope") != (
        "legacy_authenticated_route_smoke_plus_any_explicitly_requested_constrained_probe"
    ):
        reject("performance_status_scope_invalid")
    if type(performance.get("principal_id")) is not str or not str(
        performance.get("principal_id") or ""
    ).strip():
        reject("performance_principal_id_invalid")
    if type(performance.get("run_id")) is not str or not str(
        performance.get("run_id") or ""
    ).strip():
        reject("performance_run_id_invalid")
    if not isinstance(performance.get("notes"), list) or any(
        type(note) is not str or not note.strip()
        for note in list(performance.get("notes") or [])
    ):
        reject("performance_notes_invalid")
    if type(performance.get("failed_count")) is not int or performance.get(
        "failed_count"
    ) != 0:
        reject("server_failed_count_not_zero")
    if not _exact_json_equal(
        performance.get("thresholds"),
        AUTHENTICATED_PERFORMANCE_FIXED_SERVER_THRESHOLDS,
    ):
        reject("flagship_server_thresholds_not_fixed")
    if not _exact_json_equal(
        performance.get("release_identity"), expected_release_identity
    ):
        reject("performance_release_identity_not_exact")

    route_count = performance.get("route_count")
    routes = performance.get("routes")
    if (
        type(route_count) is not int
        or route_count != len(REQUIRED_AUTHENTICATED_PERFORMANCE_SERVER_ROUTES)
        or not isinstance(routes, list)
        or len(routes) != route_count
    ):
        reject("server_route_count_or_rows_invalid")
        route_rows: list[Mapping[str, Any]] = []
    else:
        route_rows = [row for row in routes if isinstance(row, Mapping)]
        if len(route_rows) != route_count:
            reject("server_route_row_not_object")

    observed_paths: set[str] = set()
    observed_route_keys: set[str] = set()
    for index, row in enumerate(route_rows):
        if type(row) is not dict or set(row) != {
            "path",
            "status_code",
            "duration_ms",
            "first_duration_ms",
            "attempt_durations_ms",
            "attempt_count",
            "budget_ms",
            "cold_budget_ms",
            "measurements",
            "cold_to_warm",
            "ok",
            "checks",
        }:
            reject(f"server_route_{index}_fields_invalid")
        path = str(row.get("path") or "").split("?", 1)[0]
        if path:
            observed_paths.add(path)
        route_key = "/app/research/" if path.startswith("/app/research/") else path
        if route_key in observed_route_keys:
            reject(f"server_route_{index}_duplicate")
        observed_route_keys.add(route_key)
        attempt_count = row.get("attempt_count")
        attempt_durations = row.get("attempt_durations_ms")
        cold_duration = row.get("first_duration_ms")
        warm_duration = row.get("duration_ms")
        if (
            row.get("ok") is not True
            or type(attempt_count) is not int
            or attempt_count != 2
            or type(attempt_durations) is not list
            or len(attempt_durations) != 2
            or any(type(value) is not int or value < 0 for value in attempt_durations)
            or type(cold_duration) is not int
            or type(warm_duration) is not int
            or cold_duration != attempt_durations[0]
            or warm_duration != attempt_durations[1]
            or type(row.get("budget_ms")) is not int
            or row.get("budget_ms") != 1_200
            or type(row.get("cold_budget_ms")) is not int
            or row.get("cold_budget_ms") != 2_400
            or type(row.get("status_code")) is not int
            or not 200 <= row.get("status_code") < 400
        ):
            reject(f"server_route_{index}_cold_warm_status_invalid")
        measurements = row.get("measurements")
        if type(measurements) is not dict or set(measurements) != {
            "cold",
            "warm",
        }:
            reject(f"server_route_{index}_measurements_invalid")
            continue
        for phase in ("cold", "warm"):
            measurement = measurements.get(phase)
            if type(measurement) is not dict or set(measurement) != {
                "sequence",
                "kind",
                "cache_state",
                "duration_ms",
                "status_code",
                "response_bytes",
                "budget_ms",
                "ok",
            }:
                reject(f"server_route_{index}_{phase}_measurement_invalid")
                continue
            duration_ms = measurement.get("duration_ms")
            budget_ms = measurement.get("budget_ms")
            status_code = measurement.get("status_code")
            expected_budget = 2_400 if phase == "cold" else 1_200
            expected_duration = cold_duration if phase == "cold" else warm_duration
            expected_sequence = 1 if phase == "cold" else 2
            expected_kind = (
                "first_measured_request_after_fixture_setup"
                if phase == "cold"
                else "same_client_immediate_repeat_request"
            )
            expected_cache_state = (
                "server_cache_not_explicitly_prewarmed_or_cleared"
                if phase == "cold"
                else "same_process_and_client_repeat_eligible"
            )
            if (
                measurement.get("ok") is not True
                or type(measurement.get("sequence")) is not int
                or measurement.get("sequence") != expected_sequence
                or measurement.get("kind") != expected_kind
                or measurement.get("cache_state") != expected_cache_state
                or type(duration_ms) is not int
                or duration_ms < 0
                or duration_ms != expected_duration
                or type(budget_ms) is not int
                or budget_ms != expected_budget
                or duration_ms > budget_ms
                or type(status_code) is not int
                or not 200 <= status_code < 400
                or type(measurement.get("response_bytes")) is not int
                or measurement.get("response_bytes") <= 0
                or (
                    phase == "warm"
                    and status_code != row.get("status_code")
                )
            ):
                reject(f"server_route_{index}_{phase}_measurement_invalid")

        comparison = row.get("cold_to_warm")
        if not _exact_json_equal(
            comparison,
            {
                "duration_delta_ms": cold_duration - warm_duration
                if type(cold_duration) is int and type(warm_duration) is int
                else None,
                "response_bytes_delta": (
                    measurements["cold"].get("response_bytes")
                    - measurements["warm"].get("response_bytes")
                    if type(measurements.get("cold")) is dict
                    and type(measurements.get("warm")) is dict
                    and type(measurements["cold"].get("response_bytes")) is int
                    and type(measurements["warm"].get("response_bytes")) is int
                    else None
                ),
            },
        ):
            reject(f"server_route_{index}_comparison_inconsistent")
        checks = row.get("checks")
        if not isinstance(checks, list) or not checks:
            reject(f"server_route_{index}_checks_invalid")
        else:
            check_names = [
                str(check.get("name") or "")
                for check in checks
                if isinstance(check, Mapping)
            ]
            if (
                len(check_names) != len(checks)
                or len(check_names) != len(set(check_names))
                or any(
                    not isinstance(check, Mapping)
                    or type(check.get("name")) is not str
                    or not str(check.get("name") or "").strip()
                    or check.get("ok") is not True
                    for check in checks
                )
            ):
                reject(f"server_route_{index}_checks_invalid")

    missing_server_routes = [
        required
        for required in REQUIRED_AUTHENTICATED_PERFORMANCE_SERVER_ROUTES
        if not (
            any(path.startswith(required) for path in observed_paths)
            if required == "/app/research/"
            else required in observed_paths
        )
    ]
    if missing_server_routes:
        reject("required_authenticated_server_routes_missing")
    if observed_route_keys != set(REQUIRED_AUTHENTICATED_PERFORMANCE_SERVER_ROUTES):
        reject("authenticated_server_route_set_not_exact")

    server_evidence = performance.get("server_request_evidence")
    if type(server_evidence) is not dict or set(server_evidence) != {
        "status",
        "cold_definition",
        "warm_definition",
        "cold_route_count",
        "warm_route_count",
    }:
        reject("server_request_evidence_missing")
    elif (
        server_evidence.get("status") != "pass"
        or type(server_evidence.get("cold_route_count")) is not int
        or server_evidence.get("cold_route_count") != route_count
        or type(server_evidence.get("warm_route_count")) is not int
        or server_evidence.get("warm_route_count") != route_count
        or server_evidence.get("cold_definition")
        != "first measured route request after authenticated fixture setup; server caches are neither claimed empty nor explicitly prewarmed"
        or server_evidence.get("warm_definition")
        != "immediate same-process and same-client repeat request"
    ):
        reject("server_request_evidence_invalid")

    claims = performance.get("claims")
    expected_claims = {
        "cold_and_warm_server_request_lab_evidence": True,
        "constrained_browser_lab_evidence": True,
        "signed_release_probe_authentication": True,
        "exact_live_release_identity_observed": True,
        "field_core_web_vitals": False,
        "physical_device_performance": False,
    }
    if not _exact_json_equal(claims, expected_claims):
        reject("performance_claims_not_exact_or_overclaimed")

    constrained = performance.get("constrained_client_evidence")
    if type(constrained) is not dict or set(constrained) != {
        "status",
        "profile",
        "target",
        "release_identity",
        "requested_browser_engines",
        "engine_rows",
        "limitations_by_engine",
        "field_core_web_vitals_claimed",
        "physical_device_claimed",
    }:
        reject("constrained_client_evidence_missing")
        constrained = {}
    if constrained.get("status") != "pass":
        reject("constrained_client_status_not_pass")
    if not _exact_json_equal(
        constrained.get("profile"), AUTHENTICATED_PERFORMANCE_FLAGSHIP_PROFILE
    ):
        reject("constrained_client_profile_not_exact")
    if not _exact_json_equal(
        constrained.get("requested_browser_engines"), ["chromium"]
    ):
        reject("constrained_client_requires_exact_chromium_engine_set")
    if not _exact_json_equal(constrained.get("limitations_by_engine"), {}):
        reject("passing_constrained_client_has_engine_limitations")
    if constrained.get("field_core_web_vitals_claimed") is not False:
        reject("field_core_web_vitals_claim_forbidden")
    if constrained.get("physical_device_claimed") is not False:
        reject("physical_device_claim_forbidden")

    target = str(constrained.get("target") or "").strip()
    target_origin = normalized_origin(target)
    try:
        target_parts = urlsplit(target)
    except ValueError:
        target_parts = urlsplit("")
    target_is_loopback = (
        target_origin is not None
        and target_origin[1] in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        target_origin is None
        or target_parts.query
        or target_parts.fragment
        or target_parts.path != "/app/search"
        or (target_origin[0] != "https" and not target_is_loopback)
    ):
        reject("constrained_client_target_not_sanitized_secure_url")
    expected_origin = _exact_public_https_origin(expected_public_origin)
    if (
        expected_origin is None
        or target_origin != expected_origin
    ):
        reject("constrained_client_target_origin_mismatch")

    target_hostname = str(target_parts.hostname or "").lower()
    target_rendered_hostname = (
        f"[{target_hostname}]" if ":" in target_hostname else target_hostname
    )
    target_default_port = 443 if target_parts.scheme == "https" else 80
    target_effective_port = target_origin[2] if target_origin is not None else 0
    target_rendered_port = (
        f":{target_effective_port}"
        if target_effective_port and target_effective_port != target_default_port
        else ""
    )
    expected_version_url = (
        f"{target_parts.scheme.lower()}://{target_rendered_hostname}"
        f"{target_rendered_port}/version"
        if target_origin is not None
        else ""
    )
    release_identity = constrained.get("release_identity")
    expected_release_identity_fields = {
        "status",
        "version_url",
        "status_code",
        "tls_verified",
        "expected",
        "observed",
        "matches_expected",
        "error",
        "credential_persisted",
    }
    if (
        type(release_identity) is not dict
        or set(release_identity) != expected_release_identity_fields
    ):
        reject("constrained_release_identity_fields_invalid")
        release_identity = {}
    observed_release_identity = release_identity.get("observed")
    expected_observed_release_identity_fields = {
        "commit_sha",
        "image_digest",
        "deployment_id",
        "manifest_status",
        "manifest_sha256",
        "replica_id",
    }
    if (
        type(observed_release_identity) is not dict
        or set(observed_release_identity)
        != expected_observed_release_identity_fields
    ):
        reject("constrained_observed_release_identity_fields_invalid")
        observed_release_identity = {}
    if (
        release_identity.get("status") != "pass"
        or release_identity.get("version_url") != expected_version_url
        or type(release_identity.get("status_code")) is not int
        or release_identity.get("status_code") != 200
        or release_identity.get("tls_verified") is not True
        or not _exact_json_equal(
            release_identity.get("expected"), expected_release_identity
        )
        or release_identity.get("matches_expected") is not True
        or release_identity.get("error") != ""
        or release_identity.get("credential_persisted") is not False
        or observed_release_identity.get("commit_sha")
        != expected_release_identity["commit_sha"]
        or observed_release_identity.get("image_digest")
        != expected_release_identity["image_digest"]
        or observed_release_identity.get("deployment_id")
        != expected_release_identity["deployment_id"]
        or observed_release_identity.get("manifest_status") != "complete"
        or re.fullmatch(
            r"[0-9a-f]{64}",
            str(observed_release_identity.get("manifest_sha256") or ""),
        )
        is None
        or observed_release_identity.get("manifest_sha256")
        != normalized_expected_manifest_sha256
        or type(observed_release_identity.get("replica_id")) is not str
        or AUTHENTICATED_PERFORMANCE_REPLICA_ID_RE.fullmatch(
            observed_release_identity.get("replica_id", "")
        )
        is None
    ):
        reject("constrained_release_identity_not_exact")

    engine_rows = constrained.get("engine_rows")
    if not isinstance(engine_rows, list) or len(engine_rows) != 1 or not isinstance(
        engine_rows[0], Mapping
    ):
        reject("constrained_client_exact_chromium_row_missing")
        engine: Mapping[str, Any] = {}
    else:
        engine = engine_rows[0]
    expected_engine_fields = {
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
    }
    if set(engine) != expected_engine_fields:
        reject("chromium_receipt_fields_not_exact")
    if engine.get("status") != "pass" or engine.get("browser_engine") != "chromium":
        reject("chromium_receipt_status_or_engine_invalid")

    identity = engine.get("identity")
    expected_identity_fields = {
        "engine",
        "browser_version",
        "playwright_version",
        "executable_path",
        "executable_sha256",
        "executable_bytes",
    }
    if type(identity) is not dict or set(identity) != expected_identity_fields:
        reject("chromium_identity_fields_invalid")
        identity = {}
    executable_path = str(identity.get("executable_path") or "")
    verified_executable_identity: dict[str, object] = {}
    if (
        identity.get("engine") != "chromium"
        or type(identity.get("browser_version")) is not str
        or not str(identity.get("browser_version") or "").strip()
        or len(str(identity.get("browser_version") or "")) > 100
        or type(identity.get("playwright_version")) is not str
        or not str(identity.get("playwright_version") or "").strip()
        or len(str(identity.get("playwright_version") or "")) > 100
        or not Path(executable_path).is_absolute()
        or type(identity.get("executable_sha256")) is not str
        or re.fullmatch(
            r"[0-9a-f]{64}", str(identity.get("executable_sha256") or "")
        )
        is None
        or type(identity.get("executable_bytes")) is not int
        or int(identity.get("executable_bytes") or 0) <= 0
    ):
        reject("chromium_identity_invalid")
    else:
        verified_executable_identity, executable_error = (
            _verified_regular_file_identity(executable_path)
        )
        if executable_error:
            reject(executable_error)
        elif not _exact_json_equal(
            verified_executable_identity,
            {
                "executable_path": executable_path,
                "executable_sha256": identity.get("executable_sha256"),
                "executable_bytes": identity.get("executable_bytes"),
            },
        ):
            reject("chromium_executable_identity_mismatch")
        elif (
            executable_path != normalized_expected_chromium_path
            or identity.get("executable_sha256")
            != normalized_expected_chromium_sha256
        ):
            reject("chromium_executable_not_controller_bound")

    launch_binding = engine.get("launch_binding")
    expected_launch_binding_fields = {
        "mechanism",
        "executable_path",
        "executable_sha256",
        "prelaunch_bytes",
        "postlaunch_identity_match",
    }
    if (
        type(launch_binding) is not dict
        or set(launch_binding) != expected_launch_binding_fields
        or launch_binding.get("mechanism")
        != "playwright_explicit_executable_path"
        or type(launch_binding.get("executable_path")) is not str
        or launch_binding.get("executable_path")
        != normalized_expected_chromium_path
        or launch_binding.get("executable_path") != identity.get("executable_path")
        or type(launch_binding.get("executable_sha256")) is not str
        or launch_binding.get("executable_sha256")
        != normalized_expected_chromium_sha256
        or launch_binding.get("executable_sha256")
        != identity.get("executable_sha256")
        or type(launch_binding.get("prelaunch_bytes")) is not int
        or launch_binding.get("prelaunch_bytes") != identity.get("executable_bytes")
        or launch_binding.get("postlaunch_identity_match") is not True
    ):
        reject("chromium_explicit_launch_binding_invalid")

    profile_support = engine.get("profile_support")
    expected_profile_support = {
        "cpu_throttling": {
            "requested_rate": 4,
            "applied": True,
            "mechanism": "chromium_cdp_Emulation.setCPUThrottlingRate",
        },
        "network_throttling": {
            "latency_ms": 150,
            "download_kbps": 1600,
            "upload_kbps": 750,
            "applied": True,
            "mechanism": "chromium_cdp_Network.emulateNetworkConditions",
        },
        "viewport_emulation": {
            "applied": True,
            "mechanism": "playwright_context",
        },
    }
    if not _exact_json_equal(profile_support, expected_profile_support):
        reject("chromium_profile_controls_not_exactly_applied")

    expected_authentication = {
        "method": "signed_release_probe_per_navigation",
        "navigation_signing_mechanism": (
            "chromium_cdp_Fetch.requestPaused_document_only"
        ),
        "playwright_routing_used": False,
        "subresource_http_cache_preserved": True,
        "signed_navigation_count": 2,
        "distinct_nonce_count": 2,
        "target_surface_observed": True,
        "release_probe_secret_persisted": False,
    }
    if not _exact_json_equal(
        engine.get("authentication"), expected_authentication
    ):
        reject("authenticated_target_not_proven")

    browser_measurements = engine.get("measurements")
    valid_measurements: dict[str, Mapping[str, Any]] = {}
    valid_probe_nonce_bindings: dict[str, Mapping[str, Any]] = {}
    if not isinstance(browser_measurements, Mapping) or set(
        browser_measurements
    ) != {"cold", "warm"}:
        reject("chromium_cold_warm_measurements_missing")
    else:
        thresholds = AUTHENTICATED_PERFORMANCE_FLAGSHIP_PROFILE["thresholds"]
        for phase in ("cold", "warm"):
            measurement = browser_measurements.get(phase)
            if not isinstance(measurement, Mapping) or set(
                measurement
            ) != AUTHENTICATED_PERFORMANCE_BROWSER_MEASUREMENT_FIELDS:
                reject(f"chromium_{phase}_measurement_fields_invalid")
                continue
            valid_measurements[phase] = measurement
            expected_cache_state = (
                "cleared_before_navigation"
                if phase == "cold"
                else "same_context_repeat_cache_observed"
            )
            duration_budget = int(
                thresholds[
                    "cold_navigation_budget_ms"
                    if phase == "cold"
                    else "warm_navigation_budget_ms"
                ]
            )
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
            if (
                measurement.get("phase") != phase
                or measurement.get("cache_state") != expected_cache_state
                or measurement.get("ok") is not True
                or type(duration_ms) is not int
                or not 0 <= duration_ms <= duration_budget
                or type(status_code) is not int
                or not 200 <= status_code < 300
                or type(final_url) is not str
                or final_url != target
                or type(request_count) is not int
                or not 1 <= request_count <= int(thresholds["max_request_count"])
                or type(transferred_bytes) is not int
                or not 0 <= transferred_bytes
                <= int(thresholds["max_transferred_bytes"])
                or (phase == "cold" and transferred_bytes == 0)
                or type(failed_request_count) is not int
                or failed_request_count != 0
                or type(incomplete_request_count) is not int
                or incomplete_request_count != 0
                or type(cache_hit_count) is not int
                or not 0 <= cache_hit_count <= request_count
                or (phase == "warm" and cache_hit_count < 1)
                or type(subresource_cache_hit_count) is not int
                or not 0 <= subresource_cache_hit_count <= cache_hit_count
                or (phase == "cold" and subresource_cache_hit_count != 0)
                or (phase == "warm" and subresource_cache_hit_count < 1)
            ):
                reject(f"chromium_{phase}_measurement_thresholds_invalid")

            if (
                type(document_release_identity) is not dict
                or set(document_release_identity)
                != AUTHENTICATED_PERFORMANCE_DOCUMENT_RELEASE_IDENTITY_FIELDS
                or type(document_release_identity.get("commit_sha")) is not str
                or document_release_identity.get("commit_sha")
                != expected_release_identity["commit_sha"]
                or type(document_release_identity.get("image_digest")) is not str
                or document_release_identity.get("image_digest")
                != expected_release_identity["image_digest"]
                or type(document_release_identity.get("deployment_id")) is not str
                or document_release_identity.get("deployment_id")
                != expected_release_identity["deployment_id"]
                or type(document_release_identity.get("manifest_status")) is not str
                or document_release_identity.get("manifest_status") != "complete"
                or type(document_release_identity.get("manifest_sha256")) is not str
                or document_release_identity.get("manifest_sha256")
                != normalized_expected_manifest_sha256
                or type(document_release_identity.get("replica_id")) is not str
                or AUTHENTICATED_PERFORMANCE_REPLICA_ID_RE.fullmatch(
                    document_release_identity.get("replica_id", "")
                )
                is None
            ):
                reject(f"chromium_{phase}_document_release_identity_invalid")

            if (
                type(document_authentication_binding) is not dict
                or set(document_authentication_binding)
                != AUTHENTICATED_PERFORMANCE_DOCUMENT_AUTHENTICATION_BINDING_FIELDS
            ):
                reject(
                    f"chromium_{phase}_document_authentication_binding_invalid"
                )
            else:
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
                    reject(
                        f"chromium_{phase}_document_authentication_binding_invalid"
                    )
                else:
                    valid_probe_nonce_bindings[phase] = (
                        document_authentication_binding
                    )

            failed_requests = measurement.get("failed_requests")
            if failed_requests != []:
                reject(f"chromium_{phase}_failed_requests_invalid")
            slowest_resources = measurement.get("slowest_resources")
            if (
                not isinstance(slowest_resources, list)
                or not slowest_resources
                or len(slowest_resources)
                > int(thresholds["slowest_resource_limit"])
            ):
                reject(f"chromium_{phase}_waterfall_summary_invalid")
            else:
                for resource in slowest_resources:
                    expected_resource_fields = {
                        "url",
                        "resource_type",
                        "status_code",
                        "duration_ms",
                        "transferred_bytes",
                        "cache_source",
                        "failed",
                        "incomplete",
                    }
                    if not isinstance(resource, Mapping) or set(resource) != expected_resource_fields:
                        reject(f"chromium_{phase}_waterfall_resource_invalid")
                        break
                    resource_url = str(resource.get("url") or "")
                    try:
                        resource_parts = urlsplit(resource_url)
                        resource_query = resource_parts.query
                        resource_port = resource_parts.port
                    except ValueError:
                        resource_parts = urlsplit("")
                        resource_query = "invalid"
                        resource_port = None
                    resource_hostname = str(resource_parts.hostname or "").lower()
                    resource_is_loopback = resource_hostname in {
                        "localhost",
                        "127.0.0.1",
                        "::1",
                    }
                    resource_path = resource_parts.path or "/"
                    resource_path_is_sanitized = (
                        resource_path in {"/version", "/app/search", "/app/assets/:asset"}
                        or re.fullmatch(
                            r"/_path-sha256/[0-9a-f]{24}", resource_path
                        )
                        is not None
                    )
                    if (
                        not resource_url
                        or resource_parts.scheme not in {"http", "https"}
                        or not resource_hostname
                        or resource_parts.username is not None
                        or resource_parts.password is not None
                        or resource_query
                        or resource_parts.fragment
                        or (resource_parts.scheme != "https" and not resource_is_loopback)
                        or resource_port is not None
                        and not 1 <= resource_port <= 65_535
                        or not resource_path_is_sanitized
                        or type(resource.get("resource_type")) is not str
                        or resource.get("resource_type")
                        not in AUTHENTICATED_PERFORMANCE_RESOURCE_TYPES
                        or type(resource.get("status_code")) is not int
                        or not 100 <= resource.get("status_code") < 400
                        or type(resource.get("duration_ms")) is not int
                        or int(resource.get("duration_ms") or 0) < 0
                        or type(resource.get("transferred_bytes")) is not int
                        or int(resource.get("transferred_bytes") or 0) < 0
                        or type(resource.get("cache_source")) is not str
                        or resource.get("cache_source")
                        not in {"network", "disk_cache", "browser_cache"}
                        or resource.get("failed") is not False
                        or resource.get("incomplete") is not False
                    ):
                        reject(f"chromium_{phase}_waterfall_resource_invalid")
                        break

            navigation_timing = measurement.get("navigation_timing")
            if (
                not isinstance(navigation_timing, Mapping)
                or set(navigation_timing)
                != AUTHENTICATED_PERFORMANCE_NAVIGATION_TIMING_FIELDS
                or any(
                    type(value) is not int or value < 0
                    for value in navigation_timing.values()
                )
            ):
                reject(f"chromium_{phase}_navigation_timing_invalid")

            checks = measurement.get("checks")
            expected_checks = {
                f"{phase}_navigation_under_budget",
                f"{phase}_request_observed",
                f"{phase}_request_count_under_budget",
                f"{phase}_transferred_bytes_under_budget",
                f"{phase}_failed_requests_under_budget",
                f"{phase}_requests_completed",
                f"{phase}_navigation_status_ok",
                f"{phase}_final_target_url_observed",
                f"{phase}_document_release_identity_exact",
                f"{phase}_document_cache_control_no_store",
                f"{phase}_server_verified_probe_nonce_acknowledged",
                f"{phase}_authenticated_app_surface_observed",
            }
            if phase == "cold":
                expected_checks.add("cold_transferred_bytes_observed")
                expected_checks.add("cold_cdp_document_signing_interception_ok")
            else:
                expected_checks.add("warm_signed_release_probe_nonces_unique")
                expected_checks.add("warm_cdp_document_signing_interception_ok")
                expected_checks.add("warm_http_cache_reuse_observed")
            if not isinstance(checks, list):
                reject(f"chromium_{phase}_checks_invalid")
            else:
                check_names = [
                    str(check.get("name") or "")
                    for check in checks
                    if isinstance(check, Mapping)
                ]
                if (
                    len(check_names) != len(checks)
                    or len(check_names) != len(set(check_names))
                    or set(check_names) != expected_checks
                    or any(
                        not isinstance(check, Mapping)
                        or check.get("ok") is not True
                        for check in checks
                    )
                ):
                    reject(f"chromium_{phase}_checks_invalid")

        if set(valid_probe_nonce_bindings) == {"cold", "warm"} and len(
            {
                valid_probe_nonce_bindings[phase][
                    "acknowledged_nonce_sha256"
                ]
                for phase in ("cold", "warm")
            }
        ) != 2:
            reject("chromium_probe_nonce_bindings_not_distinct")

    comparison = engine.get("cold_to_warm")
    expected_comparison_fields = {
        "duration_delta_ms",
        "request_count_delta",
        "transferred_bytes_delta",
    }
    if not isinstance(comparison, Mapping) or set(
        comparison
    ) != expected_comparison_fields or any(
        type(value) is not int for value in comparison.values()
    ):
        reject("chromium_cold_to_warm_comparison_invalid")
    elif set(valid_measurements) == {"cold", "warm"}:
        cold = valid_measurements["cold"]
        warm = valid_measurements["warm"]
        expected_comparison = {
            "duration_delta_ms": int(cold["duration_ms"])
            - int(warm["duration_ms"]),
            "request_count_delta": int(cold["request_count"])
            - int(warm["request_count"]),
            "transferred_bytes_delta": int(cold["transferred_bytes"])
            - int(warm["transferred_bytes"]),
        }
        if dict(comparison) != expected_comparison:
            reject("chromium_cold_to_warm_comparison_inconsistent")
        if (
            type(cold.get("transferred_bytes")) is not int
            or type(warm.get("transferred_bytes")) is not int
            or type(warm.get("subresource_cache_hit_count")) is not int
            or int(warm["subresource_cache_hit_count"]) < 1
            or int(warm["transferred_bytes"])
            >= int(cold["transferred_bytes"])
        ):
            reject("chromium_warm_http_cache_reuse_not_observed")

    limitations = engine.get("limitations")
    if (
        not isinstance(limitations, list)
        or not limitations
        or any(type(item) is not str or not item.strip() for item in limitations)
    ):
        reject("chromium_limitations_missing")
    if engine.get("field_core_web_vitals_claimed") is not False:
        reject("chromium_field_core_web_vitals_claim_forbidden")
    if engine.get("physical_device_claimed") is not False:
        reject("chromium_physical_device_claim_forbidden")

    details = {
        "required_schema": AUTHENTICATED_PERFORMANCE_FLAGSHIP_SCHEMA,
        "reported_schema": str(performance.get("schema") or ""),
        "flagship_status": str(performance.get("flagship_status") or ""),
        "server_request_evidence_status": str(
            server_evidence.get("status") if isinstance(server_evidence, Mapping) else ""
        ),
        "route_count": route_count if type(route_count) is int else None,
        "missing_server_routes": missing_server_routes,
        "constrained_client_status": str(constrained.get("status") or ""),
        "profile_name": str(
            dict(constrained.get("profile") or {}).get("name")
            if isinstance(constrained.get("profile"), Mapping)
            else ""
        ),
        "target": target,
        "target_origin_matches_expected": (
            target_origin == expected_origin
            if str(expected_public_origin or "").strip()
            else None
        ),
        "expected_release_identity": expected_release_identity,
        "expected_chromium_executable": {
            "path": normalized_expected_chromium_path,
            "sha256": normalized_expected_chromium_sha256,
        },
        "observed_release_identity": dict(observed_release_identity),
        "release_identity_matches_expected": (
            release_identity.get("matches_expected") is True
        ),
        "requested_browser_engines": list(
            constrained.get("requested_browser_engines") or []
        ),
        "browser_identity": {
            "engine": str(identity.get("engine") or ""),
            "browser_version": str(identity.get("browser_version") or ""),
            "playwright_version": str(identity.get("playwright_version") or ""),
            "executable_path": executable_path,
            "executable_sha256": str(identity.get("executable_sha256") or ""),
            "executable_bytes": identity.get("executable_bytes"),
            "independently_rehashed": bool(verified_executable_identity),
        },
        "field_core_web_vitals_claimed": False
        if constrained.get("field_core_web_vitals_claimed") is False
        and engine.get("field_core_web_vitals_claimed") is False
        and isinstance(claims, Mapping)
        and claims.get("field_core_web_vitals") is False
        else None,
        "physical_device_claimed": False
        if constrained.get("physical_device_claimed") is False
        and engine.get("physical_device_claimed") is False
        and isinstance(claims, Mapping)
        and claims.get("physical_device_performance") is False
        else None,
        "errors": errors[:80],
    }
    return not errors, details


def _covered_live_mobile_routes(live_mobile: dict[str, Any]) -> set[str]:
    covered: set[str] = set()
    for row in list(live_mobile.get("routes") or []):
        if not isinstance(row, dict) or row.get("ok") is not True:
            continue
        route = str(row.get("route") or "").split("?", 1)[0].strip()
        if route:
            covered.add(route)
    return covered


def _failed_live_mobile_coverage_checks(live_mobile: dict[str, Any]) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    observed_names: set[str] = set()
    for row in list(live_mobile.get("coverage_checks") or []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "unnamed_coverage_check")
        observed_names.add(name)
        if row.get("ok") is True:
            continue
        failed.append(
            {
                "name": name,
                "required_route_prefix": str(row.get("required_route_prefix") or ""),
                "reason": str(row.get("reason") or ""),
            }
        )
    for required_name in REQUIRED_LIVE_MOBILE_COVERAGE_CHECKS:
        if required_name not in observed_names:
            required_prefix = {
                "research_detail_route_configured": "/app/research/",
                "shortlist_run_route_configured": "/app/shortlist/run/",
                "public_tour_route_configured": "/tours/",
            }.get(required_name, "")
            failed.append(
                {
                    "name": required_name,
                    "required_route_prefix": required_prefix,
                    "reason": "Live mobile receipt predates the required all-surface coverage contract.",
                }
            )
    return failed


def _normalize_required_browser_engines(engines: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_engine in engines or DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES:
        engine = str(raw_engine or "").strip().lower()
        if engine not in SUPPORTED_FLAGSHIP_BROWSER_ENGINES:
            raise ValueError(f"unsupported_flagship_browser_engine:{engine or 'missing'}")
        if engine not in normalized:
            normalized.append(engine)
    return tuple(normalized or DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES)


def _live_mobile_flagship_browser_proof(
    live_mobile: dict[str, Any],
    *,
    required_browser_engines: tuple[str, ...] = DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES,
) -> tuple[bool, dict[str, Any]]:
    normalized_required_engines = _normalize_required_browser_engines(required_browser_engines)
    required_engine_set = set(normalized_required_engines)
    browser_proof = dict(live_mobile.get("browser_proof") or {})
    rows = [row for row in list(live_mobile.get("routes") or []) if isinstance(row, dict)]
    required_viewports = set(REQUIRED_FLAGSHIP_MOBILE_VIEWPORTS)
    declared_viewports = {
        (int(row.get("width") or 0), int(row.get("height") or 0))
        for row in list(live_mobile.get("supported_viewports") or browser_proof.get("supported_viewports") or [])
        if isinstance(row, dict)
    }
    receipt_declared_engines = {
        str(engine or "").strip().lower()
        for engine in list(live_mobile.get("required_browser_engines") or [])
        if str(engine or "").strip()
    }
    proof_declared_engines = {
        str(engine or "").strip().lower()
        for engine in list(browser_proof.get("required_browser_engines") or [])
        if str(engine or "").strip()
    }
    observed_samples: set[tuple[str, str, int, int]] = set()
    failed_rows: list[dict[str, Any]] = []
    static_or_synthetic_rows: list[dict[str, Any]] = []
    for row in rows:
        route = str(row.get("route") or "").strip()
        route_path = route.split("?", 1)[0]
        viewport = dict(row.get("viewport") or {})
        metrics = dict(row.get("metrics") or {})
        browser_engine = str(row.get("browser_engine") or metrics.get("browser_engine") or "").strip().lower()
        width = int(viewport.get("width") or metrics.get("viewport_width") or 0)
        height = int(viewport.get("height") or metrics.get("viewport_height") or 0)
        proof_mode = str(row.get("proof_mode") or metrics.get("proof_mode") or "").strip()
        passed_checks = {
            str(check.get("name") or "")
            for check in list(row.get("checks") or [])
            if isinstance(check, dict) and check.get("ok") is True
        }
        missing_checks = [name for name in REQUIRED_FLAGSHIP_BROWSER_CHECKS if name not in passed_checks]
        row_is_playwright = (
            proof_mode == "playwright"
            and metrics.get("proof_mode") == "playwright"
            and metrics.get("browser_probe") is True
            and metrics.get("static_html_probe") is not True
        )
        row_ok = row.get("ok") is True and not missing_checks and row_is_playwright
        if row_ok:
            observed_samples.add((browser_engine, route_path, width, height))
        else:
            failed_rows.append(
                {
                    "browser_engine": browser_engine or "missing",
                    "route": route_path,
                    "viewport": {"width": width, "height": height},
                    "proof_mode": proof_mode or "missing",
                    "missing_checks": missing_checks,
                }
            )
        if not row_is_playwright:
            static_or_synthetic_rows.append(
                {
                    "browser_engine": browser_engine or "missing",
                    "route": route_path,
                    "viewport": {"width": width, "height": height},
                    "proof_mode": proof_mode or "missing",
                }
            )
    missing_samples = [
        {"browser_engine": engine, "route": route, "viewport": {"width": width, "height": height}}
        for engine in normalized_required_engines
        for route in REQUIRED_LIVE_MOBILE_ROUTES
        for width, height in REQUIRED_FLAGSHIP_MOBILE_VIEWPORTS
        if (engine, route, width, height) not in observed_samples
    ]
    dynamic_route_families = {
        "research_detail": lambda route: route.startswith("/app/research/") and route != "/app/research",
        "shortlist_run": lambda route: route.startswith("/app/shortlist/run/") and route != "/app/shortlist/run",
        "public_tour": lambda route: route.startswith("/tours/") and route.count("/") == 2,
    }
    missing_dynamic_route_viewports: dict[str, list[dict[str, Any]]] = {}
    for family, route_matches in dynamic_route_families.items():
        observed_family_samples = {
            (engine, width, height)
            for engine, route, width, height in observed_samples
            if route_matches(route)
        }
        missing_dynamic_route_viewports[family] = [
            {"browser_engine": engine, "width": width, "height": height}
            for engine in normalized_required_engines
            for width, height in REQUIRED_FLAGSHIP_MOBILE_VIEWPORTS
            if (engine, width, height) not in observed_family_samples
        ]
    observed_engines = {engine for engine, _, _, _ in observed_samples if engine}
    missing_browser_engines = sorted(required_engine_set - observed_engines)
    top_level_contract_ok = (
        live_mobile.get("proof_mode") == "playwright_browser_all"
        and browser_proof.get("mode") == "playwright_browser_all"
        and browser_proof.get("ready") is True
        and required_viewports.issubset(declared_viewports)
        and required_engine_set.issubset(receipt_declared_engines)
        and required_engine_set.issubset(proof_declared_engines)
    )
    details = {
        "required_mode": "playwright_browser_all",
        "reported_mode": str(live_mobile.get("proof_mode") or ""),
        "browser_proof_ready": browser_proof.get("ready") is True,
        "required_browser_engines": list(normalized_required_engines),
        "receipt_declared_browser_engines": sorted(receipt_declared_engines),
        "browser_proof_declared_browser_engines": sorted(proof_declared_engines),
        "observed_browser_engines": sorted(observed_engines),
        "missing_browser_engines": missing_browser_engines,
        "required_viewports": [
            {"width": width, "height": height}
            for width, height in REQUIRED_FLAGSHIP_MOBILE_VIEWPORTS
        ],
        "declared_viewports": [
            {"width": width, "height": height}
            for width, height in sorted(declared_viewports)
        ],
        "missing_samples": missing_samples,
        "missing_research_detail_viewports": missing_dynamic_route_viewports["research_detail"],
        "missing_dynamic_route_viewports": missing_dynamic_route_viewports,
        "static_or_synthetic_rows": static_or_synthetic_rows,
        "failed_browser_rows": failed_rows,
    }
    return (
        top_level_contract_ok
        and not missing_browser_engines
        and not missing_samples
        and not any(missing_dynamic_route_viewports.values())
        and not static_or_synthetic_rows
        and not failed_rows,
        details,
    )


def _flagship_continuous_ux_proof(
    continuous_ux: dict[str, Any],
    *,
    expected_release_commit_sha: str,
    required_browser_engines: tuple[str, ...] = DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES,
) -> tuple[bool, dict[str, Any]]:
    engines = _normalize_required_browser_engines(required_browser_engines)
    engine_set = set(engines)
    expected_sha = str(expected_release_commit_sha or "").strip().lower()
    reported_sha = str(continuous_ux.get("release_commit_sha") or "").strip().lower()
    declared_engines = {
        str(engine or "").strip().lower()
        for engine in list(continuous_ux.get("required_browser_engines") or [])
        if str(engine or "").strip()
    }
    declared_routes = {
        str(route or "").strip()
        for route in list(continuous_ux.get("required_routes") or [])
        if str(route or "").strip()
    }
    declared_state_kinds = {
        str(state or "").strip().lower()
        for state in list(continuous_ux.get("required_state_kinds") or [])
        if str(state or "").strip()
    }
    top_checks = {
        str(check.get("name") or ""): check.get("ok") is True
        for check in list(continuous_ux.get("checks") or [])
        if isinstance(check, dict)
    }
    missing_top_checks = [
        name for name in REQUIRED_CONTINUOUS_UX_TOP_CHECKS
        if top_checks.get(name) is not True
    ]
    rows = [
        dict(row)
        for row in list(continuous_ux.get("rows") or [])
        if isinstance(row, dict)
    ]
    present_samples: set[tuple[str, str]] = set()
    passed_samples: set[tuple[str, str]] = set()
    row_sample_keys: list[tuple[str, str]] = []
    failed_rows: list[dict[str, Any]] = []
    for row in rows:
        engine = str(row.get("browser_engine") or "").strip().lower()
        route = str(row.get("route") or "").strip()
        sample_key = (engine, route)
        row_sample_keys.append(sample_key)
        present_samples.add(sample_key)
        checks = {
            str(check.get("name") or ""): check.get("ok") is True
            for check in list(row.get("checks") or [])
            if isinstance(check, dict)
        }
        required_checks = list(REQUIRED_CONTINUOUS_UX_ROW_CHECKS)
        if route == "/app/search":
            required_checks.extend(
                (
                    "loading_action_available",
                    "loading_state_visible",
                    "loading_state_semantic",
                )
            )
        elif route == "/app/search?continuous_ux_state=offline":
            required_checks.extend(
                (
                    "error_state_visible",
                    "error_state_semantic",
                    "error_state_recovers_online",
                )
            )
        missing_checks = [name for name in required_checks if checks.get(name) is not True]
        metrics = dict(row.get("metrics") or {})
        try:
            status_code = int(row["status_code"])
            first_value_ms = float(metrics["first_value_ms"])
            first_value_cold_ms = float(metrics["first_value_cold_ms"])
            first_value_samples = [
                float(value)
                for value in list(metrics["first_value_samples_ms"])
            ]
            first_value_initial_samples = [
                float(value)
                for value in list(metrics["first_value_initial_samples_ms"])
            ]
            first_value_sample_count = int(metrics["first_value_sample_count"])
            body_text_length = int(metrics["body_text_length"])
            visible_interactive_count = int(metrics["visible_interactive_count"])
            visible_image_count = int(metrics["visible_image_count"])
            terminal_visible_image_count = int(
                metrics["terminal_visible_image_count"]
            )
            broken_visible_image_count = int(metrics["broken_visible_image_count"])
            zoom_400_percent = int(metrics["zoom_400_percent"])
            zoom_400_viewport_width = int(metrics["zoom_400_viewport_width"])
            zoom_400_scroll_width = int(metrics["zoom_400_scroll_width"])
            clipped_interactive_count = int(
                metrics["zoom_400_clipped_interactive_count"]
            )
            route_fulfill_count = int(metrics["route_fulfill_count"])
        except (KeyError, TypeError, ValueError, OverflowError):
            status_code = 0
            first_value_ms = 0.0
            first_value_cold_ms = -1.0
            first_value_samples = []
            first_value_initial_samples = []
            first_value_sample_count = 0
            body_text_length = -1
            visible_interactive_count = -1
            visible_image_count = -1
            terminal_visible_image_count = -1
            broken_visible_image_count = -1
            zoom_400_percent = 0
            zoom_400_viewport_width = 0
            zoom_400_scroll_width = 0
            clipped_interactive_count = -1
            route_fulfill_count = -1
        first_value_median = (
            sorted(first_value_samples)[len(first_value_samples) // 2]
            if first_value_samples
            else 0.0
        )
        initial_first_value_median = (
            sorted(first_value_initial_samples)[
                len(first_value_initial_samples) // 2
            ]
            if first_value_initial_samples
            else 0.0
        )
        first_value_retry_used = metrics.get("first_value_retry_used")
        common_first_value_ok = (
            math.isfinite(first_value_cold_ms)
            and first_value_cold_ms >= 0
            and math.isfinite(first_value_ms)
            and first_value_ms >= 0
            and metrics.get("first_value_basis")
            == REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BASIS
            and isinstance(first_value_retry_used, bool)
        )
        if engine == REQUIRED_CONTINUOUS_UX_FIRST_VALUE_ENGINE:
            samples_ok = (
                first_value_sample_count
                == REQUIRED_CONTINUOUS_UX_FIRST_VALUE_SAMPLE_COUNT
                and len(first_value_samples)
                == REQUIRED_CONTINUOUS_UX_FIRST_VALUE_SAMPLE_COUNT
                and len(first_value_initial_samples)
                == REQUIRED_CONTINUOUS_UX_FIRST_VALUE_SAMPLE_COUNT
                and all(
                    math.isfinite(value) and value > 0
                    for value in first_value_samples
                )
                and all(
                    math.isfinite(value) and value > 0
                    for value in first_value_initial_samples
                )
            )
            retry_coherent = (
                first_value_retry_used is False
                and first_value_initial_samples == first_value_samples
            ) or (
                first_value_retry_used is True
                and initial_first_value_median
                > REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BUDGET_MS
                and 0
                < first_value_median
                <= REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BUDGET_MS
            )
            first_value_ok = (
                common_first_value_ok
                and metrics.get("first_value_gated") is True
                and samples_ok
                and retry_coherent
                and abs(first_value_median - first_value_ms) <= 0.5
                and first_value_ms
                <= REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BUDGET_MS
            )
        else:
            first_value_ok = (
                common_first_value_ok
                and metrics.get("first_value_gated") is False
                and first_value_sample_count == 1
                and len(first_value_samples) == 1
                and len(first_value_initial_samples) == 1
                and first_value_retry_used is False
                and first_value_initial_samples == first_value_samples
                and all(
                    math.isfinite(value) and value >= 0
                    for value in first_value_samples
                )
                and abs(first_value_median - first_value_ms) <= 0.5
            )
        route_document_ok = (
            status_code == 200
            and str(metrics.get("document_ready_state") or "")
            in {"interactive", "complete"}
            and str(metrics.get("final_route") or "") == route
            and row.get("error") == ""
        )
        structural_metrics_ok = (
            body_text_length > 0
            and visible_interactive_count > 0
            and visible_image_count >= 0
            and terminal_visible_image_count == visible_image_count
            and broken_visible_image_count == 0
            and metrics.get("navigation_visible") is True
            and metrics.get("horizontal_overflow") is False
            and (metrics.get("main_visible") is True or route == REQUIRED_CONTINUOUS_UX_ROUTES[-1])
        )
        zoom_metrics_ok = (
            zoom_400_percent == 400
            and zoom_400_viewport_width == 320
            and 0 < zoom_400_scroll_width <= zoom_400_viewport_width + 2
            and metrics.get("zoom_400_reflow_without_horizontal_scroll") is True
            and clipped_interactive_count == 0
        )
        provider_mocking_metrics_ok = (
            metrics.get("provider_response_mocked") is False
            and metrics.get("request_interception_mode")
            == "origin_scoped_headers_continue_only"
            and route_fulfill_count == 0
        )
        state_metrics_ok = True
        if route == "/app/search":
            state_metrics_ok = (
                metrics.get("loading_action_available") is True
                and metrics.get("loading_state_visible") is True
                and metrics.get("loading_state_semantic") is True
            )
        elif route == "/app/search?continuous_ux_state=offline":
            state_metrics_ok = (
                metrics.get("error_state_visible") is True
                and metrics.get("error_state_semantic") is True
                and metrics.get("error_state_recovered_online") is True
            )
        metrics_ok = (
            route_document_ok
            and structural_metrics_ok
            and zoom_metrics_ok
            and first_value_ok
            and provider_mocking_metrics_ok
            and state_metrics_ok
        )
        row_ok = (
            row.get("ok") is True
            and engine in declared_engines
            and engine in SUPPORTED_FLAGSHIP_BROWSER_ENGINES
            and route in REQUIRED_CONTINUOUS_UX_ROUTES
            and not missing_checks
            and metrics_ok
        )
        if row_ok:
            passed_samples.add((engine, route))
        else:
            failed_rows.append(
                {
                    "browser_engine": engine or "missing",
                    "route": route or "missing",
                    "missing_checks": missing_checks,
                    "first_value_ms": first_value_ms,
                    "error": str(row.get("error") or ""),
                }
            )
    expected_samples = {
        (engine, route)
        for engine in declared_engines
        for route in REQUIRED_CONTINUOUS_UX_ROUTES
    }
    missing_samples = sorted(expected_samples - passed_samples)
    missing_present_samples = sorted(expected_samples - present_samples)
    unexpected_samples = sorted(present_samples - expected_samples)
    duplicate_samples = sorted(
        sample for sample in present_samples if row_sample_keys.count(sample) > 1
    )
    engine_failures = [
        dict(failure)
        for failure in list(continuous_ux.get("engine_failures") or [])
        if isinstance(failure, dict)
    ]
    visual_baseline_ok, visual_baseline_errors = validate_visual_baseline_receipt(
        continuous_ux.get("visual_baseline"),
        expected_release_commit_sha=expected_sha,
    )
    visual_baseline_receipt_sha256 = str(
        continuous_ux.get("visual_baseline_receipt_sha256") or ""
    ).strip().lower()
    try:
        embedded_visual_baseline_sha256 = visual_baseline_payload_sha256(
            continuous_ux.get("visual_baseline")
        )
    except (TypeError, ValueError):
        embedded_visual_baseline_sha256 = ""
    contract_errors: list[str] = []
    if continuous_ux.get("schema") != REQUIRED_CONTINUOUS_UX_SCHEMA:
        contract_errors.append("schema_mismatch")
    if continuous_ux.get("proof_scope") != REQUIRED_CONTINUOUS_UX_PROOF_SCOPE:
        contract_errors.append("proof_scope_mismatch")
    if continuous_ux.get("proof_mode") != REQUIRED_CONTINUOUS_UX_PROOF_MODE:
        contract_errors.append("proof_mode_not_real_browser")
    if continuous_ux.get("production_claim") is not False:
        contract_errors.append("production_claim_must_be_false")
    if continuous_ux.get("deployed_or_live_proof") is not False:
        contract_errors.append("deployed_or_live_proof_must_be_false")
    if str(continuous_ux.get("storage_backend") or "") != "memory":
        contract_errors.append("memory_storage_required")
    if continuous_ux.get("provider_response_mocking") is not False:
        contract_errors.append("provider_response_mocking_forbidden")
    if continuous_ux.get("screenshot_pixel_comparison") is not True:
        contract_errors.append("screenshot_pixel_gate_required")
    if not visual_baseline_ok:
        contract_errors.extend(
            f"visual_baseline_{error}" for error in visual_baseline_errors
        )
    if re.fullmatch(r"[0-9a-f]{64}", visual_baseline_receipt_sha256) is None:
        contract_errors.append("visual_baseline_receipt_sha256_invalid")
    elif visual_baseline_receipt_sha256 != embedded_visual_baseline_sha256:
        contract_errors.append("visual_baseline_receipt_sha256_mismatch")
    if continuous_ux.get("base_origin_kind") != "loopback":
        contract_errors.append("loopback_origin_kind_required")
    if continuous_ux.get("first_value_basis") != REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BASIS:
        contract_errors.append("first_value_basis_mismatch")
    if (
        continuous_ux.get("first_value_max_attempts")
        != REQUIRED_CONTINUOUS_UX_FIRST_VALUE_MAX_ATTEMPTS
    ):
        contract_errors.append("first_value_max_attempts_mismatch")
    try:
        declared_first_value_budget_ms = float(
            continuous_ux.get("first_value_budget_ms") or 0.0
        )
    except (TypeError, ValueError, OverflowError):
        declared_first_value_budget_ms = 0.0
    if not (
        math.isfinite(declared_first_value_budget_ms)
        and abs(
            declared_first_value_budget_ms
            - REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BUDGET_MS
        )
        <= 0.001
    ):
        contract_errors.append("first_value_budget_invalid")
    if not expected_sha or reported_sha != expected_sha:
        contract_errors.append("release_commit_sha_mismatch")
    if not engine_set.issubset(declared_engines):
        contract_errors.append("browser_engine_declaration_incomplete")
    if not declared_engines.issubset(set(SUPPORTED_FLAGSHIP_BROWSER_ENGINES)):
        contract_errors.append("browser_engine_declaration_unsupported")
    if declared_routes != set(REQUIRED_CONTINUOUS_UX_ROUTES):
        contract_errors.append("route_declaration_mismatch")
    if declared_state_kinds != {"loading", "error"}:
        contract_errors.append("state_kind_declaration_mismatch")
    try:
        declared_expected_sample_count = int(
            continuous_ux.get("expected_sample_count") or -1
        )
        declared_observed_sample_count = int(
            continuous_ux.get("observed_sample_count") or -1
        )
        declared_passed_sample_count = int(
            continuous_ux.get("passed_sample_count", -1)
        )
        declared_missing_sample_count = int(
            continuous_ux.get("missing_sample_count", -1)
        )
        declared_duplicate_sample_count = int(
            continuous_ux.get("duplicate_sample_count", -1)
        )
        declared_failed_count = int(continuous_ux.get("failed_count", -1))
    except (TypeError, ValueError, OverflowError):
        declared_expected_sample_count = -1
        declared_observed_sample_count = -1
        declared_passed_sample_count = -1
        declared_missing_sample_count = -1
        declared_duplicate_sample_count = -1
        declared_failed_count = -1
    if declared_expected_sample_count != len(expected_samples):
        contract_errors.append("expected_sample_count_mismatch")
    if declared_observed_sample_count != len(present_samples):
        contract_errors.append("observed_sample_count_mismatch")
    if declared_passed_sample_count != len(passed_samples):
        contract_errors.append("passed_sample_count_mismatch")
    if declared_missing_sample_count != len(missing_present_samples):
        contract_errors.append("missing_sample_count_mismatch")
    if declared_duplicate_sample_count != len(duplicate_samples):
        contract_errors.append("duplicate_sample_count_mismatch")
    if declared_failed_count != len(failed_rows) + len(engine_failures):
        contract_errors.append("failed_count_mismatch")
    if missing_present_samples:
        contract_errors.append("observed_sample_matrix_incomplete")
    if unexpected_samples:
        contract_errors.append("unexpected_samples_present")
    if duplicate_samples:
        contract_errors.append("duplicate_samples_present")
    if engine_failures:
        contract_errors.append("browser_engine_failures_present")
    details = {
        "schema": str(continuous_ux.get("schema") or ""),
        "proof_scope": str(continuous_ux.get("proof_scope") or ""),
        "proof_mode": str(continuous_ux.get("proof_mode") or ""),
        "production_claim": continuous_ux.get("production_claim"),
        "deployed_or_live_proof": continuous_ux.get("deployed_or_live_proof"),
        "expected_release_commit_sha": expected_sha,
        "reported_release_commit_sha": reported_sha,
        "required_browser_engines": list(engines),
        "declared_browser_engines": sorted(declared_engines),
        "required_routes": list(REQUIRED_CONTINUOUS_UX_ROUTES),
        "declared_routes": sorted(declared_routes),
        "declared_state_kinds": sorted(declared_state_kinds),
        "first_value_budget_ms": REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BUDGET_MS,
        "declared_first_value_budget_ms": declared_first_value_budget_ms,
        "first_value_basis": REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BASIS,
        "first_value_browser_engine": REQUIRED_CONTINUOUS_UX_FIRST_VALUE_ENGINE,
        "first_value_sample_count": REQUIRED_CONTINUOUS_UX_FIRST_VALUE_SAMPLE_COUNT,
        "first_value_max_attempts": REQUIRED_CONTINUOUS_UX_FIRST_VALUE_MAX_ATTEMPTS,
        "declared_expected_sample_count": declared_expected_sample_count,
        "declared_observed_sample_count": declared_observed_sample_count,
        "declared_passed_sample_count": declared_passed_sample_count,
        "declared_missing_sample_count": declared_missing_sample_count,
        "declared_duplicate_sample_count": declared_duplicate_sample_count,
        "missing_top_checks": missing_top_checks,
        "missing_samples": [
            {"browser_engine": engine, "route": route}
            for engine, route in missing_samples
        ],
        "missing_present_samples": [
            {"browser_engine": engine, "route": route}
            for engine, route in missing_present_samples
        ],
        "unexpected_samples": [
            {"browser_engine": engine, "route": route}
            for engine, route in unexpected_samples
        ],
        "duplicate_samples": [
            {"browser_engine": engine, "route": route}
            for engine, route in duplicate_samples
        ],
        "engine_failures": engine_failures,
        "failed_rows": failed_rows,
        "contract_errors": contract_errors,
    }
    ready = (
        continuous_ux.get("status") == "pass"
        and declared_failed_count == 0
        and not contract_errors
        and not missing_top_checks
        and not missing_samples
        and not failed_rows
    )
    return ready, details


def _flagship_accessibility_route_has_literal_placeholder(route: object) -> bool:
    return bool(
        re.search(
            r"(?:[{}\[\]<>]|%(?:7b|7d|5b|5d|3c|3e))",
            str(route or ""),
            flags=re.IGNORECASE,
        )
    )


def _flagship_accessibility_route_key(route: object) -> str:
    route_text = str(route or "").strip()
    route_path, separator, _query = route_text.partition("?")
    route_path = route_path.rstrip("/") or "/"
    if _flagship_accessibility_route_has_literal_placeholder(route):
        return route_path
    if route_path.startswith("/app/research/") and route_path.count("/") == 3:
        return "/app/research/[detail]"
    if (
        not separator
        and route_path.startswith("/app/shortlist/run/")
        and route_path.count("/") == 4
    ):
        return "/app/shortlist/run/[run]"
    if (
        not separator
        and route_path.startswith("/tours/")
        and route_path.count("/") == 2
        and not route_path.endswith(".json")
    ):
        return "/tours/[slug]"
    return route_path


def _flagship_accessibility_proof(
    accessibility: dict[str, Any],
    *,
    required_browser_engines: tuple[str, ...] = DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES,
) -> tuple[bool, dict[str, Any]]:
    engines = _normalize_required_browser_engines(required_browser_engines)
    engine_set = set(engines)
    declared_engines = {
        str(engine or "").strip().lower()
        for engine in list(accessibility.get("required_browser_engines") or [])
        if str(engine or "").strip()
    }
    configured_routes = [str(route or "").strip() for route in list(accessibility.get("configured_routes") or [])]
    dynamic_routes = {
        family: [route for route in configured_routes if _flagship_accessibility_route_key(route) == family]
        for family in (
            "/app/research/[detail]",
            "/app/shortlist/run/[run]",
            "/tours/[slug]",
        )
    }
    detail_routes = dynamic_routes["/app/research/[detail]"]
    literal_placeholder_routes = [
        route
        for route in configured_routes
        if _flagship_accessibility_route_has_literal_placeholder(route)
    ]
    rows = [dict(row) for row in list(accessibility.get("routes") or []) if isinstance(row, dict)]
    observed_samples: set[tuple[str, str]] = set()
    failed_rows: list[dict[str, Any]] = []
    for row in rows:
        route = str(row.get("route") or "").strip()
        route_path = route.split("?", 1)[0].rstrip("/") or "/"
        route_key = _flagship_accessibility_route_key(route)
        engine = str(row.get("browser_engine") or "").strip().lower()
        checks = {
            str(check.get("name") or ""): check.get("ok") is True
            for check in list(row.get("checks") or [])
            if isinstance(check, dict)
        }
        missing_checks = [name for name in REQUIRED_FLAGSHIP_ACCESSIBILITY_CHECKS if checks.get(name) is not True]
        metrics = dict(row.get("metrics") or {})
        moderate_or_higher_wcag_count = metrics.get(
            "axe_moderate_or_higher_wcag_count"
        )
        row_ok = (
            row.get("ok") is True
            and not missing_checks
            and metrics.get("axe_core_version") == REQUIRED_AXE_CORE_VERSION
            and type(moderate_or_higher_wcag_count) is int
            and moderate_or_higher_wcag_count == 0
        )
        if row_ok:
            observed_samples.add((engine, route_key))
        else:
            failed_rows.append(
                {
                    "browser_engine": engine or "missing",
                    "route": route_path,
                    "missing_checks": missing_checks,
                    "axe_core_version": str(metrics.get("axe_core_version") or ""),
                    "serious_critical_count": int(metrics.get("axe_serious_critical_count") or 0),
                    "moderate_or_higher_wcag_count": (
                        moderate_or_higher_wcag_count
                        if type(moderate_or_higher_wcag_count) is int
                        else "missing_or_invalid"
                    ),
                    "error": str(metrics.get("error") or ""),
                }
            )
    expected_route_keys = {
        *(_flagship_accessibility_route_key(route) for route in REQUIRED_FLAGSHIP_ACCESSIBILITY_ROUTES),
        "/app/research/[detail]",
        "/app/shortlist/run/[run]",
        "/tours/[slug]",
    }
    expected_samples = {(engine, route) for engine in engines for route in expected_route_keys}
    missing_samples = sorted(expected_samples - observed_samples)
    top_checks = {
        str(check.get("name") or ""): check.get("ok") is True
        for check in list(accessibility.get("checks") or [])
        if isinstance(check, dict)
    }
    required_top_checks = (
        "axe_core_pinned_input",
        "accessibility_route_engine_matrix_complete",
        "public_information_route_matrix_configured",
        "flagship_static_route_matrix_configured",
        "literal_route_placeholders_absent",
        "research_detail_route_configured",
        "shortlist_run_route_configured",
        "public_tour_route_configured",
        "dialog_focus_interaction_sampled",
    )
    missing_top_checks = [name for name in required_top_checks if top_checks.get(name) is not True]
    observed_engines = {engine for engine, _route in observed_samples if engine}
    missing_engines = sorted(engine_set - observed_engines)
    details = {
        "required_browser_engines": list(engines),
        "declared_browser_engines": sorted(declared_engines),
        "observed_browser_engines": sorted(observed_engines),
        "missing_browser_engines": missing_engines,
        "required_routes": sorted(expected_route_keys),
        "configured_routes": configured_routes,
        "research_detail_routes": detail_routes,
        "dynamic_routes": dynamic_routes,
        "literal_placeholder_routes": literal_placeholder_routes,
        "required_axe_core_version": REQUIRED_AXE_CORE_VERSION,
        "reported_axe_core_version": str(accessibility.get("axe_core_version") or ""),
        "missing_samples": [
            {"browser_engine": engine, "route": route}
            for engine, route in missing_samples
        ],
        "missing_top_checks": missing_top_checks,
        "failed_rows": failed_rows,
    }
    ready = (
        accessibility.get("status") == "pass"
        and int(accessibility.get("failed_count") or 0) == 0
        and accessibility.get("axe_core_version") == REQUIRED_AXE_CORE_VERSION
        and engine_set.issubset(declared_engines)
        and all(dynamic_routes.values())
        and not literal_placeholder_routes
        and not missing_engines
        and not missing_samples
        and not missing_top_checks
        and not failed_rows
    )
    return ready, details


def _flagship_failure_state_proof(
    failure_states: dict[str, Any],
    *,
    required_browser_engines: tuple[str, ...] = DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES,
) -> tuple[bool, dict[str, Any]]:
    engines = _normalize_required_browser_engines(required_browser_engines)
    declared_engines = {
        str(engine or "").strip().lower()
        for engine in list(failure_states.get("required_browser_engines") or [])
        if str(engine or "").strip()
    }
    declared_states = {
        str(state or "").strip()
        for state in list(failure_states.get("required_failure_states") or [])
        if str(state or "").strip()
    }
    rows = [dict(row) for row in list(failure_states.get("rows") or []) if isinstance(row, dict)]
    preservation_probe_route = str(failure_states.get("preservation_probe_route") or "").strip()
    observed_samples: set[tuple[str, str]] = set()
    failed_rows: list[dict[str, Any]] = []
    for row in rows:
        engine = str(row.get("browser_engine") or "").strip().lower()
        state = str(row.get("state") or "").strip()
        row_checks = {
            str(check.get("name") or ""): check.get("ok") is True
            for check in list(row.get("checks") or [])
            if isinstance(check, dict)
        }
        missing_checks = [
            name for name in REQUIRED_FLAGSHIP_FAILURE_STATE_CHECKS
            if row_checks.get(name) is not True
        ]
        preservation = dict(row.get("preservation_probe") or {})
        preservation_before = dict(preservation.get("before") or {})
        preservation_after = dict(preservation.get("after") or {})
        before_sha = str(preservation_before.get("sha256") or "").strip().lower()
        after_sha = str(preservation_after.get("sha256") or "").strip().lower()
        before_status = preservation_before.get("status_code")
        after_status = preservation_after.get("status_code")
        before_body_bytes = preservation_before.get("body_bytes")
        after_body_bytes = preservation_after.get("body_bytes")
        before_canonical_bytes = preservation_before.get("canonical_bytes")
        after_canonical_bytes = preservation_after.get("canonical_bytes")
        preservation_receipt_ok = (
            row.get("customer_data_preserved") is True
            and preservation.get("same_digest") is True
            and preservation_before.get("ok") is True
            and preservation_after.get("ok") is True
            and type(before_status) is int
            and before_status == 200
            and type(after_status) is int
            and after_status == 200
            and type(before_body_bytes) is int
            and before_body_bytes > 0
            and type(after_body_bytes) is int
            and after_body_bytes > 0
            and type(before_canonical_bytes) is int
            and before_canonical_bytes > 0
            and type(after_canonical_bytes) is int
            and after_canonical_bytes > 0
            and re.fullmatch(r"[0-9a-f]{64}", before_sha) is not None
            and before_sha == after_sha
        )
        if not preservation_receipt_ok:
            missing_checks.append("preservation_probe_receipt")
        if row.get("ok") is True and not missing_checks:
            observed_samples.add((engine, state))
        else:
            failed_rows.append(
                {
                    "browser_engine": engine or "missing",
                    "state": state or "missing",
                    "missing_checks": missing_checks,
                    "error": str(row.get("error") or ""),
                }
            )
    expected_samples = {
        (engine, state)
        for engine in engines
        for state in REQUIRED_FLAGSHIP_FAILURE_STATES
    }
    missing_samples = sorted(expected_samples - observed_samples)
    top_checks = {
        str(check.get("name") or ""): check.get("ok") is True
        for check in list(failure_states.get("checks") or [])
        if isinstance(check, dict)
    }
    required_top_checks = (
        "required_failure_scenarios_configured",
        "browser_state_engine_matrix_complete",
        "no_provider_response_mocking",
        "customer_data_preservation_matrix_complete",
    )
    missing_top_checks = [name for name in required_top_checks if top_checks.get(name) is not True]
    details = {
        "proof_mode": str(failure_states.get("proof_mode") or ""),
        "required_browser_engines": list(engines),
        "declared_browser_engines": sorted(declared_engines),
        "required_failure_states": list(REQUIRED_FLAGSHIP_FAILURE_STATES),
        "declared_failure_states": sorted(declared_states),
        "required_preservation_probe_route": REQUIRED_FLAGSHIP_FAILURE_PRESERVATION_ROUTE,
        "preservation_probe_route": preservation_probe_route,
        "missing_top_checks": missing_top_checks,
        "missing_samples": [
            {"browser_engine": engine, "state": state}
            for engine, state in missing_samples
        ],
        "failed_rows": failed_rows,
    }
    ready = (
        failure_states.get("status") == "pass"
        and int(failure_states.get("failed_count") or 0) == 0
        and details["proof_mode"] == "playwright_browser_all"
        and set(engines).issubset(declared_engines)
        and set(REQUIRED_FLAGSHIP_FAILURE_STATES).issubset(declared_states)
        and preservation_probe_route == REQUIRED_FLAGSHIP_FAILURE_PRESERVATION_ROUTE
        and not missing_top_checks
        and not missing_samples
        and not failed_rows
    )
    return ready, details


def _flagship_activation_to_value_proof(
    activation: dict[str, Any],
    *,
    expected_release_commit_sha: str,
) -> tuple[bool, dict[str, Any]]:
    expected_release_sha = str(expected_release_commit_sha or "")
    reported_release_sha = str(activation.get("release_commit_sha") or "")
    release_sha_matches = (
        re.fullmatch(r"[0-9a-f]{40}", expected_release_sha) is not None
        and re.fullmatch(r"[0-9a-f]{40}", reported_release_sha) is not None
        and reported_release_sha == expected_release_sha
    )
    steps = [dict(row) for row in list(activation.get("steps") or []) if isinstance(row, dict)]
    step_by_name = {str(row.get("name") or ""): row for row in steps}
    missing_steps = [
        name
        for name in REQUIRED_ACTIVATION_TO_VALUE_STEPS
        if step_by_name.get(name, {}).get("ok") is not True
    ]
    checks = {
        str(row.get("name") or ""): row.get("ok") is True
        for row in list(activation.get("checks") or [])
        if isinstance(row, dict)
    }
    required_checks = (
        "protected_live_configuration",
        "idempotent_run_reservation",
        "activation_step_matrix_complete",
        "safe_cleanup_complete",
    )
    missing_checks = [name for name in required_checks if checks.get(name) is not True]
    live_contract = dict(activation.get("live_contract") or {})
    required_live_contract = (
        "explicit_persona",
        "principal_headers_forbidden",
        "session_injection_forbidden",
        "provider_response_mocking_forbidden",
        "local_execution_forbidden",
        "deployed_playwright_runner",
    )
    missing_live_contract = [
        name for name in required_live_contract if live_contract.get(name) is not True
    ]
    account_step = dict(step_by_name.get("account_create_or_reopen") or {})
    provider_step = dict(step_by_name.get("real_provider_results") or {})
    walkthrough_step = dict(step_by_name.get("walkthrough_request_or_reuse") or {})
    cleanup_step = dict(step_by_name.get("safe_cleanup") or {})
    details = {
        "auth_mode": str(activation.get("auth_mode") or ""),
        "browser_engine": str(activation.get("browser_engine") or ""),
        "proof_mode": str(activation.get("proof_mode") or ""),
        "expected_release_commit_sha": expected_release_sha,
        "reported_release_commit_sha": reported_release_sha,
        "release_commit_sha_matches": release_sha_matches,
        "persona_digest_present": bool(str(activation.get("persona_digest") or "").strip()),
        "run_key_present": bool(str(activation.get("run_key") or "").strip()),
        "missing_steps": missing_steps,
        "missing_checks": missing_checks,
        "missing_live_contract": missing_live_contract,
        "account_outcome": str(account_step.get("outcome") or ""),
        "provider_count": int(provider_step.get("provider_count") or 0),
        "result_count": int(provider_step.get("result_count") or 0),
        "walkthrough_mode": str(walkthrough_step.get("mode") or ""),
        "session_cleared": cleanup_step.get("session_cleared") is True,
    }
    ready = (
        activation.get("status") == "pass"
        and int(activation.get("failed_count") or 0) == 0
        and str(activation.get("auth_mode") or "") in {"google", "email_link"}
        and details["proof_mode"] == "deployed_playwright"
        and details["release_commit_sha_matches"]
        and details["persona_digest_present"]
        and details["run_key_present"]
        and not missing_steps
        and not missing_checks
        and not missing_live_contract
        and details["account_outcome"] in {"created", "reopened"}
        and details["provider_count"] > 0
        and details["result_count"] > 0
        and details["walkthrough_mode"] in {"requested", "reused_ready"}
        and details["session_cleared"]
    )
    return ready, details


def _public_sign_in_checks(public_smoke: dict[str, Any]) -> tuple[bool, list[str], list[dict[str, Any]]]:
    sign_in_row: dict[str, Any] = {}
    for row in list(public_smoke.get("checks") or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("path") or "").split("?", 1)[0].strip() == "/sign-in":
            sign_in_row = row
            break
    if not sign_in_row:
        return False, list(REQUIRED_PUBLIC_AUTH_CHECKS), []
    passed_checks = {
        str(check.get("name") or "")
        for check in list(sign_in_row.get("checks") or [])
        if isinstance(check, dict) and check.get("ok") is True
    }
    missing = [name for name in REQUIRED_PUBLIC_AUTH_CHECKS if name not in passed_checks]
    failed = [
        {
            "name": str(check.get("name") or "unnamed_check"),
            "ok": bool(check.get("ok")),
        }
        for check in list(sign_in_row.get("checks") or [])
        if isinstance(check, dict) and check.get("ok") is not True and str(check.get("name") or "").startswith("sign_in_")
    ]
    return not missing and not failed, missing, failed


def _route_named_checks(receipt: dict[str, Any], route_path: str) -> tuple[dict[str, Any], set[str], list[dict[str, Any]]]:
    route_row: dict[str, Any] = {}
    for row in list(receipt.get("checks") or receipt.get("routes") or []):
        if not isinstance(row, dict):
            continue
        path = str(row.get("path") or row.get("route") or "").split("?", 1)[0].strip()
        if path == route_path:
            route_row = row
            break
    if not route_row:
        return {}, set(), []
    passed = {
        str(check.get("name") or "")
        for check in list(route_row.get("checks") or [])
        if isinstance(check, dict) and check.get("ok") is True
    }
    failed = [
        {
            "name": str(check.get("name") or "unnamed_check"),
            "ok": bool(check.get("ok")),
            "detail": str(check.get("detail") or ""),
        }
        for check in list(route_row.get("checks") or [])
        if isinstance(check, dict) and check.get("ok") is not True
    ]
    return route_row, passed, failed


def _billing_route_availability_details(route_row: dict[str, Any]) -> dict[str, Any]:
    passed_checks = {
        str(check.get("name") or "")
        for check in list(route_row.get("checks") or [])
        if isinstance(check, dict) and check.get("ok") is True
    }
    if set(REQUIRED_FLAGSHIP_BILLING_HANDOFF_CHECKS).issubset(passed_checks):
        state = "available"
        reason = "no_second_login_handoff_verified"
    elif passed_checks & {"billing_internal_account_fallback", "billing_bridge_guided_login_assist"}:
        state = "degraded"
        reason = "account_fallback_or_second_login_required"
    elif "billing_fail_closed_recovery" in passed_checks:
        state = "unavailable"
        reason = "fail_closed_recovery_only"
    elif "billing_external_handoff" in passed_checks:
        state = "degraded"
        reason = "external_handoff_not_proven_usable"
    else:
        state = "unavailable"
        reason = "billing_route_not_available"
    return {
        "state": state,
        "available": state == "available",
        "reason": reason,
        "passed_checks": sorted(passed_checks),
    }


def _live_mobile_billing_availability_details(live_mobile: dict[str, Any]) -> dict[str, Any]:
    route_rows = [
        dict(row)
        for row in list(live_mobile.get("routes") or [])
        if isinstance(row, dict)
        and str(row.get("route") or "").split("?", 1)[0].strip() == "/app/billing"
    ]
    details = [_billing_route_availability_details(row) for row in route_rows]
    states = [str(detail.get("state") or "unavailable") for detail in details]
    state = (
        "available"
        if states and all(value == "available" for value in states)
        else "degraded"
        if any(value == "degraded" for value in states)
        else "unavailable"
    )
    return {
        "state": state,
        "available": state == "available",
        "sample_count": len(route_rows),
        "reasons": sorted({str(detail.get("reason") or "") for detail in details if detail.get("reason")}),
    }


def _authenticated_billing_surface_checks(
    authenticated_smoke: dict[str, Any],
    *,
    require_available: bool = False,
) -> tuple[bool, list[str], list[dict[str, Any]], str]:
    route_row, passed_checks, failed_checks = _route_named_checks(authenticated_smoke, "/app/billing")
    if not route_row:
        return False, ["billing_route_missing"], [], ""
    missing = [name for name in REQUIRED_BILLING_SURFACE_CHECKS if name not in passed_checks]
    handoff_or_recovery_ok = (
        ("billing_external_handoff" in passed_checks and "billing_no_second_login" in passed_checks)
        or "billing_fail_closed_recovery" in passed_checks
        or ("billing_bridge_launch" in passed_checks and "billing_internal_account_fallback" in passed_checks)
    )
    if not handoff_or_recovery_ok:
        missing.append("billing_external_handoff_or_fail_closed_recovery")
    if require_available:
        availability = _billing_route_availability_details(route_row)
        if availability.get("available") is not True:
            missing.append("billing_available_no_second_login_handoff")
    return not missing and not failed_checks, missing, failed_checks, str(route_row.get("status_code") or "")


def _authenticated_account_notification_checks(authenticated_smoke: dict[str, Any]) -> tuple[bool, list[str], list[dict[str, Any]]]:
    route_row, passed_checks, failed_checks = _route_named_checks(authenticated_smoke, "/app/account")
    if not route_row:
        return False, ["account_route_missing"], []
    missing = [name for name in REQUIRED_ACCOUNT_NOTIFICATION_CHECKS if name not in passed_checks]
    notification_failed = [
        check
        for check in failed_checks
        if str(check.get("name") or "").startswith("account_notification") or str(check.get("name") or "") == "account_notifications"
    ]
    return not missing and not notification_failed, missing, notification_failed


def _failed_receipt_checks(receipt: dict[str, Any], *, limit: int = 12) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for row in list(receipt.get("checks") or []):
        if not isinstance(row, dict) or row.get("ok") is True:
            continue
        failed.append(
            {
                "name": str(row.get("name") or "unnamed_check"),
                "detail": str(row.get("detail") or row.get("reason") or row.get("error") or ""),
                **({"state": row.get("state")} if isinstance(row.get("state"), dict) else {}),
                **({"coverage": row.get("coverage")} if isinstance(row.get("coverage"), dict) else {}),
                **({"frame_delta_stats": row.get("frame_delta_stats")} if isinstance(row.get("frame_delta_stats"), dict) else {}),
            }
        )
    return failed[:limit]


def _hard_gate_receipt_ok(receipt: dict[str, Any]) -> bool:
    return (
        receipt.get("status") == "pass"
        and int(receipt.get("failed_count") or 0) == 0
        and all(isinstance(row, dict) and row.get("ok") is True for row in list(receipt.get("checks") or []))
    )


def _walkthrough_provider_proof_receipt_ok(receipt: dict[str, Any]) -> bool:
    required_providers = set(REQUIRED_SCENE_VIDEO_PARITY_PROVIDERS) - {"magic"}
    verified_providers = {
        str(provider or "").strip().lower()
        for provider in list(receipt.get("verified_providers") or [])
        if str(provider or "").strip()
    }
    verified_orchestrators = {
        str(orchestrator or "").strip().lower()
        for orchestrator in list(receipt.get("verified_orchestrators") or [])
        if str(orchestrator or "").strip()
    }
    indexed_participants = {
        str(participant or "").strip().lower()
        for participant in list(receipt.get("indexed_participants") or [])
        if str(participant or "").strip()
    }
    ea_rows = [
        dict(row)
        for row in list(receipt.get("provenance_index") or [])
        if isinstance(row, dict)
        and str(row.get("key") or "").strip().lower() == "ea"
    ]
    ea_row_ok = len(ea_rows) == 1 and (
        str(ea_rows[0].get("kind") or "").strip().lower() == "orchestrator"
        and str(ea_rows[0].get("role") or "").strip().lower()
        == "governance_and_verification"
        and str(ea_rows[0].get("status") or "").strip().lower() == "pass"
        and ea_rows[0].get("media_authorship") is False
        and not any(
            field in ea_rows[0]
            for field in (
                "slug",
                "bundle_slug",
                "video_relpath",
                "video_path",
                "video_sha256",
                "bundle_media_path",
                "provider_media_binding",
                "evidence_sidecar_path",
                "evidence_bundle_slug",
                "evidence_video_relpath",
                "evidence_video_sha256",
            )
        )
    )
    return (
        _hard_gate_receipt_ok(receipt)
        and required_providers <= verified_providers
        and {"ea"} <= verified_orchestrators
        and required_providers | {"ea"} <= indexed_participants
        and ea_row_ok
    )


def _walkthrough_safe_slug(value: object) -> str:
    raw_value = str(value or "")
    raw = raw_value.strip()
    if (
        not raw
        or raw != raw_value
        or raw in {".", ".."}
        or "/" in raw
        or "\\" in raw
        or "://" in raw
        or "\x00" in raw
    ):
        return ""
    return raw


def _walkthrough_safe_relpath(value: object) -> str:
    raw_value = str(value or "")
    raw = raw_value.strip()
    if (
        not raw
        or raw != raw_value
        or raw.startswith("/")
        or "\\" in raw
        or "://" in raw
        or "\x00" in raw
    ):
        return ""
    path = PurePosixPath(raw)
    normalized = "/".join(path.parts)
    if (
        path.is_absolute()
        or normalized != raw
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        return ""
    return normalized


def _walkthrough_quality_provider_binding_status(
    quality_receipt: dict[str, Any],
    provider_receipt: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    quality_binding = (
        dict(quality_receipt.get("provider_media_binding") or {})
        if isinstance(quality_receipt.get("provider_media_binding"), dict)
        else {}
    )
    provider_results = [
        dict(row)
        for row in list(provider_receipt.get("provider_results") or [])
        if isinstance(row, dict)
        and str(row.get("provider") or "").strip().lower() == "magicfit"
        and str(row.get("status") or "").strip().lower() == "pass"
    ]
    provenance_rows = [
        dict(row)
        for row in list(provider_receipt.get("provenance_index") or [])
        if isinstance(row, dict)
        and str(row.get("key") or "").strip().lower() == "magicfit"
        and str(row.get("kind") or "").strip().lower() == "media_provider"
        and str(row.get("role") or "").strip().lower() == "walkthrough_media_provider"
        and str(row.get("status") or "").strip().lower() == "pass"
        and row.get("media_authorship") is True
    ]
    provider_result = provider_results[0] if len(provider_results) == 1 else {}
    provenance_row = provenance_rows[0] if len(provenance_rows) == 1 else {}

    slug = _walkthrough_safe_slug(provider_result.get("slug"))
    video_relpath = _walkthrough_safe_relpath(provider_result.get("video_relpath"))
    video_sha256 = str(provider_result.get("video_sha256") or "").strip().lower()
    sha256_valid = len(video_sha256) == 64 and all(
        character in "0123456789abcdef" for character in video_sha256
    )
    expected_binding = {
        "provider": "magicfit",
        "bundle_slug": slug,
        "video_relpath": video_relpath,
        "bundle_media_path": f"{slug}/{video_relpath}" if slug and video_relpath else "",
        "video_sha256": video_sha256,
    }
    provenance_binding = {
        "provider": "magicfit",
        "bundle_slug": _walkthrough_safe_slug(
            provenance_row.get("evidence_bundle_slug")
        ),
        "video_relpath": _walkthrough_safe_relpath(
            provenance_row.get("evidence_video_relpath")
        ),
        "bundle_media_path": (
            f"{_walkthrough_safe_slug(provenance_row.get('evidence_bundle_slug'))}/{_walkthrough_safe_relpath(provenance_row.get('evidence_video_relpath'))}"
            if provenance_row
            else ""
        ),
        "video_sha256": str(
            provenance_row.get("evidence_video_sha256") or ""
        ).strip().lower(),
    }
    checks = {
        "quality_contract": quality_receipt.get("contract_name")
        == "propertyquarry.walkthrough_quality_gate.v1",
        "provider_contract": provider_receipt.get("contract_name")
        == "propertyquarry.walkthrough_provider_proof_gate.v1",
        "provider_proof_receipt_recorded": bool(
            str(quality_receipt.get("provider_proof_receipt_path") or "").strip()
        ),
        "magicfit_result_unique": len(provider_results) == 1,
        "magicfit_provenance_unique": len(provenance_rows) == 1,
        "provider_media_identity_valid": bool(slug and video_relpath and sha256_valid),
        "quality_binding_matches_provider_result": quality_binding == expected_binding,
        "provider_provenance_matches_result": provenance_binding == expected_binding,
        "quality_top_level_identity_matches": (
            str(quality_receipt.get("demo_slug") or "").strip() == slug
            and str(quality_receipt.get("video_relpath") or "").strip()
            == video_relpath
            and str(quality_receipt.get("video_sha256") or "").strip().lower()
            == video_sha256
        ),
    }
    return all(checks.values()), {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "quality_binding": quality_binding,
        "provider_binding": expected_binding,
        "provenance_binding": provenance_binding,
    }


def _route_covers_required_detail(route: str, required_prefix: str) -> bool:
    normalized_route = str(route or "").split("?", 1)[0].strip().rstrip("/")
    normalized_prefix = str(required_prefix or "").strip().rstrip("/")
    if not normalized_route or not normalized_prefix:
        return False
    return normalized_route.startswith(f"{normalized_prefix}/")


def _host_readme_path(readme_path_text: str) -> Path:
    path = Path(readme_path_text)
    if path.is_file() or not str(path).startswith("/data/incoming_property_tours/"):
        return path
    host_incoming_root = Path(
        os.getenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR")
        or os.getenv("PROPERTYQUARRY_TOUR_EXPORT_DROP_DIR")
        or "/docker/property/state/incoming_property_tours"
    ).expanduser()
    try:
        relative = path.relative_to("/data/incoming_property_tours")
    except ValueError:
        return path
    return host_incoming_root / relative


def _repo_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "docker-compose.property.yml").is_file():
        return cwd.resolve()
    return Path(__file__).resolve().parents[1]


def _fallback_artifact_readme_path(*, slug: str, provider: str) -> Path:
    configured_artifact_dir = str(os.getenv("EA_ARTIFACT_DIR") or os.getenv("EA_ARTIFACTS_DIR") or "").strip()
    if configured_artifact_dir:
        return (
            Path(configured_artifact_dir).expanduser().resolve()
            / "property-tour-export-drop-readmes"
            / slug
            / provider
            / "README.propertyquarry-export.txt"
        )
    return (
        _repo_root()
        / "_completion"
        / "property_tour_exports"
        / "drop-readmes"
        / slug
        / provider
        / "README.propertyquarry-export.txt"
    ).resolve()


def _operator_drop_provider_rows(import_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    provider_rows: dict[str, dict[str, Any]] = {}
    for raw_row in list(import_manifest.get("prepared_drop_dirs") or []):
        if not isinstance(raw_row, dict):
            continue
        provider = str(raw_row.get("provider") or "").strip().lower()
        if not provider:
            continue
        provider_rows[provider] = dict(raw_row)
    for raw_row in list(import_manifest.get("imports") or []):
        if not isinstance(raw_row, dict):
            continue
        provider = str(raw_row.get("provider") or "").strip().lower()
        slug = str(raw_row.get("slug") or "").strip()
        export_dir_text = str(raw_row.get("export_dir") or raw_row.get("asset_dir") or "").strip()
        if not provider:
            continue
        export_dir = Path(export_dir_text).expanduser().resolve() if export_dir_text else None
        export_readme_path = export_dir / "README.propertyquarry-export.txt" if export_dir is not None else None
        synthesized_row: dict[str, Any] = {
            "provider": provider,
            "slug": slug,
            "export_dir": str(export_dir) if export_dir is not None else export_dir_text,
            "readme": str(export_readme_path) if export_readme_path is not None else "",
            "drop_readme": str(export_readme_path) if export_readme_path is not None else "",
            "artifact_readme": str(_fallback_artifact_readme_path(slug=slug, provider=provider)) if slug else "",
        }
        merged_row = dict(provider_rows.get(provider) or {})
        for key, value in synthesized_row.items():
            if merged_row.get(key):
                continue
            merged_row[key] = value
        provider_rows[provider] = merged_row
    return provider_rows


def _read_first_available_readme(row: dict[str, Any]) -> tuple[str, str, str]:
    attempted: list[str] = []
    for key in ("readme", "artifact_readme", "drop_readme"):
        readme_path_text = str(row.get(key) or "").strip()
        if not readme_path_text or readme_path_text in attempted:
            continue
        attempted.append(readme_path_text)
        readme_path = _host_readme_path(readme_path_text)
        try:
            return readme_path.read_text(encoding="utf-8"), str(readme_path), key
        except FileNotFoundError:
            continue
        except Exception as exc:
            return "", str(readme_path), f"{key}:{type(exc).__name__}: {exc}"
    return "", ", ".join(attempted), ""


def _billing_handoff_ready(
    billing_receipt: dict[str, Any],
    *,
    authenticated_smoke: dict[str, Any] | None = None,
    require_live_no_second_login: bool = False,
) -> bool:
    return bool(
        _billing_handoff_readiness_details(
            billing_receipt,
            authenticated_smoke=authenticated_smoke,
            require_live_no_second_login=require_live_no_second_login,
        ).get("ready")
    )


def _billing_handoff_readiness_details(
    billing_receipt: dict[str, Any],
    *,
    authenticated_smoke: dict[str, Any] | None = None,
    require_live_no_second_login: bool = False,
) -> dict[str, Any]:
    handoff = billing_receipt.get("billing_handoff")
    if not isinstance(handoff, dict):
        return {
            "ready": False,
            "ready_via": "",
            "direct_account_handoff_usable": None,
            "signed_handoff_usable": False,
            "live_smoke_external_handoff_usable": False,
            "live_smoke_no_second_login": False,
            "pricing_placeholder": False,
            "provider_disabled": False,
            "strict_live_proof_required": require_live_no_second_login,
        }
    pricing_probe = handoff.get("pricing_surface_probe")
    pricing_placeholder = isinstance(pricing_probe, dict) and pricing_probe.get("placeholder") is True
    provider_status = str(billing_receipt.get("status") or "").strip().lower()
    provider_disabled = (
        provider_status in {"disabled", "intentionally_disabled", "not_configured"}
        or billing_receipt.get("enabled") is False
        or handoff.get("enabled") is False
    )
    provider_receipt_eligible = provider_status != "blocked" and not provider_disabled
    direct_ready_without_live_proof = (
        bool(handoff.get("configured"))
        and bool(handoff.get("host_resolves"))
        and handoff.get("account_handoff_usable") is True
        and str(handoff.get("url") or "").strip().startswith("https://")
        and provider_receipt_eligible
        and not pricing_placeholder
    )
    bridge = billing_receipt.get("billing_sso_bridge")
    member_token_handoff = billing_receipt.get("member_login_token_handoff")
    authenticated_route_row, authenticated_passed_checks, _ = _route_named_checks(
        authenticated_smoke or {},
        "/app/billing",
    )
    live_external_usable = "billing_external_handoff_usable" in authenticated_passed_checks
    live_no_second_login = "billing_no_second_login" in authenticated_passed_checks
    authenticated_external_handoff_usable = bool(authenticated_route_row) and live_external_usable and live_no_second_login
    direct_ready = direct_ready_without_live_proof and (
        authenticated_external_handoff_usable or not require_live_no_second_login
    )
    member_token_ready = (
        isinstance(member_token_handoff, dict)
        and member_token_handoff.get("ready") is True
        and bool(handoff.get("configured"))
        and bool(handoff.get("host_resolves"))
        and str(handoff.get("url") or "").strip().startswith("https://")
        and provider_receipt_eligible
        and (authenticated_external_handoff_usable or not authenticated_route_row)
        and (authenticated_external_handoff_usable or not require_live_no_second_login)
        and not pricing_placeholder
    )
    bridge_session_ready = (
        isinstance(bridge, dict)
        and bridge.get("ready") is True
        and bridge.get("exchange_checked") is True
        and bridge.get("exchange_usable") is True
    )
    bridge_guided_login_assist = "billing_bridge_guided_login_assist" in authenticated_passed_checks
    authenticated_bridge_launch = bool(authenticated_route_row) and (
        "billing_bridge_launch" in authenticated_passed_checks
        and live_no_second_login
    )
    sso_bridge_ready = (
        bridge_session_ready
        and not bridge_guided_login_assist
        and (authenticated_bridge_launch or authenticated_external_handoff_usable)
    )
    ready_via = ""
    if direct_ready:
        ready_via = "direct_account"
    elif member_token_ready:
        ready_via = "member_login_token"
    elif sso_bridge_ready:
        ready_via = "sso_bridge"
    return {
        "ready": bool(ready_via),
        "ready_via": ready_via,
        "direct_account_handoff_usable": handoff.get("account_handoff_usable"),
        "signed_handoff_usable": bool(member_token_ready or sso_bridge_ready),
        "member_login_token_usable": bool(member_token_ready),
        "sso_bridge_usable": bool(sso_bridge_ready),
        "live_smoke_external_handoff_usable": bool(authenticated_external_handoff_usable),
        "live_smoke_no_second_login": bool(authenticated_route_row) and live_no_second_login,
        "live_smoke_bridge_launch": bool(authenticated_bridge_launch),
        "bridge_guided_login_assist": bool(bridge_guided_login_assist),
        "pricing_placeholder": bool(pricing_placeholder),
        "provider_disabled": bool(provider_disabled),
        "strict_live_proof_required": require_live_no_second_login,
    }


def _id_austria_gate_details(id_austria_receipt: dict[str, Any]) -> dict[str, Any]:
    status = str(id_austria_receipt.get("status") or "").strip().lower()
    required = bool(id_austria_receipt.get("required"))
    configured = bool(id_austria_receipt.get("configured"))
    ready = (
        status in {"dry_verified_configured", "live_verified_configured", "production_verified_configured"}
        or (status == "disabled" and not required and not configured)
    )
    return {
        "ready": ready,
        "status": status or "missing",
        "required": required,
        "configured": configured,
        "error": str(id_austria_receipt.get("error") or "").strip(),
        "missing_env": list(id_austria_receipt.get("missing_env") or []),
        "redirect_uri": str(id_austria_receipt.get("redirect_uri") or "").strip(),
        "issuer": str(id_austria_receipt.get("issuer") or "").strip(),
    }


def _operator_drop_readme_status(
    import_manifest: dict[str, Any],
    *,
    expected_providers: set[str] | None = None,
) -> tuple[bool, int, list[str], list[dict[str, Any]]]:
    if expected_providers is None:
        expected_providers = set(PROVIDER_OPERATOR_DROP_README_TOKENS)
    else:
        expected_providers = set(expected_providers)
    if not expected_providers:
        return True, 0, [], []
    provider_rows = _operator_drop_provider_rows(import_manifest)
    failures: list[dict[str, Any]] = []
    verified_providers: set[str] = set()
    for provider in sorted(expected_providers):
        row = provider_rows.get(provider)
        if not row:
            failures.append({"provider": provider, "status": "missing_manifest_row", "missing_tokens": ["readme_path"]})
            continue
        readme_path_text = str(row.get("readme") or "").strip()
        artifact_readme_path_text = str(row.get("artifact_readme") or "").strip()
        if not readme_path_text and not artifact_readme_path_text:
            failures.append({"provider": provider, "status": "missing_readme_path", "missing_tokens": ["readme_path"]})
            continue
        body, resolved_readme_path, readme_source = _read_first_available_readme(row)
        if not body:
            failures.append(
                {
                    "provider": provider,
                    "status": "missing_readme_file",
                    "readme": resolved_readme_path,
                    "readme_source": readme_source,
                    "readme_write_error": str(row.get("readme_write_error") or ""),
                    "artifact_readme_write_error": str(row.get("artifact_readme_write_error") or ""),
                    "missing_tokens": ["readme_file"],
                }
            )
            continue
        required_tokens = (*COMMON_OPERATOR_DROP_README_TOKENS, *PROVIDER_OPERATOR_DROP_README_TOKENS[provider])
        missing_labels = [label for label, token in required_tokens if token not in body]
        if missing_labels:
            failures.append(
                {
                    "provider": provider,
                    "status": "stale_readme",
                    "readme": resolved_readme_path,
                    "readme_source": readme_source,
                    "missing_tokens": missing_labels,
                }
            )
            continue
        verified_providers.add(provider)
    missing_providers = sorted(expected_providers - verified_providers)
    return (not failures and not missing_providers, len(verified_providers), missing_providers, failures[:8])


def _evidence_overlay_launch_status(
    receipt: dict[str, Any],
    *,
    receipt_present: bool,
    receipt_sha256: str,
    expected_release_commit_sha: str,
    expected_teable_origin: str,
    expected_teable_base_id_sha256: str,
    expected_phase: str,
    max_age_hours: float,
    now: datetime | None,
) -> tuple[bool, dict[str, Any]]:
    errors: list[str] = []
    if not receipt_present:
        errors.append("evidence overlay read-model receipt is required")
    elif not expected_release_commit_sha:
        errors.append("evidence overlay validation requires the expected release SHA")
    else:
        try:
            errors.extend(
                verify_evidence_overlay_read_model_receipt(
                    receipt,
                    expected_candidate_sha=expected_release_commit_sha,
                    max_age_hours=max_age_hours,
                    expected_teable_origin=expected_teable_origin,
                    expected_teable_base_id_sha256=expected_teable_base_id_sha256,
                    expected_phase=expected_phase,
                    now=now,
                )
            )
        except Exception as exc:
            errors.append(
                f"evidence overlay read-model verifier could not complete: {type(exc).__name__}"
            )
    ingestion = dict(receipt.get("ingestion") or {}) if isinstance(receipt.get("ingestion"), dict) else {}
    read_model = dict(receipt.get("read_model") or {}) if isinstance(receipt.get("read_model"), dict) else {}
    source_evidence = dict(receipt.get("source_evidence") or {}) if isinstance(receipt.get("source_evidence"), dict) else {}
    source_authority = dict(receipt.get("source_authority") or {}) if isinstance(receipt.get("source_authority"), dict) else {}
    activation = dict(receipt.get("activation") or {}) if isinstance(receipt.get("activation"), dict) else {}
    return (
        not errors,
        {
            "status": "pass" if not errors else "blocked",
            "errors": errors,
            "candidate_sha": str(receipt.get("candidate_sha") or ""),
            "snapshot_id": str(receipt.get("snapshot_id") or ""),
            "source_payload_sha256": str(receipt.get("source_payload_sha256") or ""),
            "registry_payload_sha256": str(receipt.get("registry_payload_sha256") or ""),
            "source": str(ingestion.get("source") or ""),
            "target": str(ingestion.get("target") or ""),
            "layer_count": ingestion.get("layer_count"),
            "record_count": ingestion.get("record_count"),
            "query_p95_ms": read_model.get("query_p95_ms"),
            "query_budget_ms": read_model.get("query_budget_ms"),
            "expected_teable_origin": expected_teable_origin,
            "expected_teable_base_id_sha256": expected_teable_base_id_sha256,
            "teable_origin": str(source_evidence.get("base_origin") or ""),
            "teable_base_id_sha256": str(source_evidence.get("base_id_sha256") or ""),
            "source_evidence": {
                "base_origin": str(source_evidence.get("base_origin") or ""),
                "base_id_sha256": str(source_evidence.get("base_id_sha256") or ""),
            },
            "source_authority": {
                "expected_origin": str(source_authority.get("expected_origin") or ""),
                "expected_base_id_sha256": str(
                    source_authority.get("expected_base_id_sha256") or ""
                ),
                "bound_independently": source_authority.get("bound_independently") is True,
            },
            "activation_phase": str(activation.get("phase") or ""),
            "receipt_sha256": receipt_sha256,
        },
    )


def _rybbit_launch_status(
    receipt: dict[str, Any],
    *,
    receipt_present: bool,
    expected_release_commit_sha: str,
    expected_public_origin: str,
    expected_analytics_origin: str,
    expected_site_id_sha256: str,
    max_age_minutes: float,
    now: datetime | None,
) -> tuple[bool, dict[str, Any]]:
    errors: list[str] = []
    expected_bindings = {
        "release SHA": expected_release_commit_sha,
        "public origin": expected_public_origin,
        "analytics origin": expected_analytics_origin,
        "site ID SHA-256": expected_site_id_sha256,
    }
    if not receipt_present:
        errors.append("Rybbit delivery receipt is required")
    missing_bindings = [name for name, value in expected_bindings.items() if not str(value or "").strip()]
    if receipt_present and missing_bindings:
        errors.append("Rybbit validation requires explicit " + ", ".join(missing_bindings))
    if receipt_present and not missing_bindings:
        try:
            errors.extend(
                verify_rybbit_delivery_receipt(
                    receipt,
                    expected_candidate_sha=expected_release_commit_sha,
                    expected_public_origin=expected_public_origin,
                    expected_analytics_origin=expected_analytics_origin,
                    expected_site_id_sha256=expected_site_id_sha256,
                    max_age_minutes=max_age_minutes,
                    now=now,
                )
            )
        except Exception as exc:
            errors.append(
                "Rybbit delivery verifier could not complete: "
                f"{type(exc).__name__}"
            )
    browser = dict(receipt.get("browser") or {}) if isinstance(receipt.get("browser"), dict) else {}
    collector = dict(browser.get("collector") or {}) if isinstance(browser.get("collector"), dict) else {}
    api = dict(receipt.get("api") or {}) if isinstance(receipt.get("api"), dict) else {}
    events = dict(api.get("events") or {}) if isinstance(api.get("events"), dict) else {}
    return (
        not errors,
        {
            "status": "pass" if not errors else "blocked",
            "errors": errors,
            "candidate_sha": str(receipt.get("candidate_sha") or ""),
            "public_origin": str(receipt.get("public_origin") or ""),
            "analytics_origin": str(receipt.get("analytics_origin") or ""),
            "site_id_sha256": str(receipt.get("site_id_sha256") or ""),
            "collector_status_code": collector.get("status_code"),
            "event_name": str(events.get("event_name") or ""),
            "event_count": events.get("event_count"),
            "observed_after_probe": events.get("observed_after_probe") is True,
        },
    )


def _exact_release_binding_errors(
    receipt: dict[str, Any],
    *,
    expected_release_commit_sha: str,
    expected_release_image_digest: str,
    label: str,
) -> tuple[list[str], dict[str, str]]:
    errors: list[str] = []
    expected_sha = str(expected_release_commit_sha or "").strip().lower()
    expected_image = str(expected_release_image_digest or "").strip().lower()
    identity = (
        dict(receipt.get("release_identity") or {})
        if isinstance(receipt.get("release_identity"), dict)
        else {}
    )
    observed_sha = str(identity.get("commit_sha") or "").strip().lower()
    observed_image = str(identity.get("image_digest") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        errors.append(f"{label} validation requires the exact Gold release SHA")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_image):
        errors.append(f"{label} validation requires the immutable Gold image digest")
    if observed_sha != expected_sha:
        errors.append(f"{label} receipt commit does not match the Gold release")
    if observed_image != expected_image:
        errors.append(f"{label} receipt image does not match the Gold release")
    return errors, {
        "commit_sha": observed_sha,
        "image_digest": observed_image,
    }


def _governed_receipt_age(
    receipt: dict[str, Any],
    *,
    now: datetime | None,
    max_age_hours: float,
    label: str,
) -> tuple[list[str], str, float | None]:
    errors: list[str] = []
    raw_generated_at = str(receipt.get("generated_at") or "").strip()
    generated_at = _parse_receipt_datetime(raw_generated_at)
    if generated_at is None:
        errors.append(f"{label} receipt has no timezone-aware generated_at")
        return errors, raw_generated_at, None
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = (observed_now - generated_at).total_seconds()
    if (
        not math.isfinite(age_seconds)
        or age_seconds < 0
        or age_seconds > float(max_age_hours) * 3600
    ):
        errors.append(f"{label} receipt is stale or future-dated")
    return errors, raw_generated_at, age_seconds


def _global_market_envelope_launch_status(
    receipt: dict[str, Any],
    *,
    receipt_present: bool,
    required: bool,
    expected_release_commit_sha: str,
    expected_release_image_digest: str,
    now: datetime | None,
    max_age_hours: float,
) -> tuple[bool, dict[str, Any]]:
    if not receipt_present:
        return (
            not required,
            {
                "status": "missing" if required else "not_required",
                "errors": (
                    ["global market envelope receipt is required for launch/Core"]
                    if required
                    else []
                ),
                "receipt_status": "",
                "schema": "",
                "release_identity": {},
                "generated_at": "",
                "age_seconds": None,
                "required_markets": list(ACTIVE_PROVIDER_MATRIX_COUNTRY_CODES),
                "launch_supported_markets": [],
                "market_results": [],
                "source_blockers": [],
            },
        )

    errors: list[str] = []
    schema = str(receipt.get("schema") or "").strip()
    receipt_status = str(receipt.get("status") or "").strip()
    if schema != GLOBAL_MARKET_ENVELOPE_RECEIPT_SCHEMA:
        errors.append("global market envelope receipt has the wrong schema")
    if str(receipt.get("source_schema") or "").strip() != (
        "propertyquarry.global_market_envelope.v1"
    ):
        errors.append("global market envelope receipt has the wrong source schema")
    if not str(receipt.get("source_envelope_id") or "").strip():
        errors.append("global market envelope receipt has no source envelope ID")
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        str(receipt.get("source_sha256") or "").strip().lower(),
    ):
        errors.append("global market envelope receipt has no valid source SHA-256")
    if receipt_status.lower() != "ready":
        errors.append("global market envelope receipt is not READY")
    if receipt.get("independently_attested") is not True:
        errors.append("global market envelope receipt is not independently attested")
    if not str(receipt.get("live_receipt_ref") or "").strip():
        errors.append("global market envelope receipt has no governed live evidence reference")
    try:
        live_receipt_age_seconds = float(receipt.get("live_receipt_age_seconds"))
    except (TypeError, ValueError):
        live_receipt_age_seconds = None
    if (
        live_receipt_age_seconds is None
        or not math.isfinite(live_receipt_age_seconds)
        or live_receipt_age_seconds < -300
        or live_receipt_age_seconds > float(max_age_hours) * 3600
    ):
        errors.append("global market envelope live evidence is stale, future-dated, or missing")
    source_blockers = [
        dict(row) if isinstance(row, dict) else {"detail": str(row)}
        for row in list(receipt.get("blockers") or [])[:20]
    ]
    if source_blockers:
        errors.append("global market envelope receipt still contains blockers")

    phase_one = (
        dict(receipt.get("phase_one") or {})
        if isinstance(receipt.get("phase_one"), dict)
        else {}
    )
    if str(phase_one.get("operating_mode") or "").strip() != "launch":
        errors.append("global market envelope is not declared for launch operation")

    summary = (
        dict(receipt.get("summary") or {})
        if isinstance(receipt.get("summary"), dict)
        else {}
    )
    expected_markets = tuple(ACTIVE_PROVIDER_MATRIX_COUNTRY_CODES)
    launch_supported_markets = tuple(
        dict.fromkeys(
            str(value or "").strip().upper()
            for value in list(summary.get("launch_supported_markets") or [])
            if str(value or "").strip()
        )
    )
    classifications = (
        dict(summary.get("classifications") or {})
        if isinstance(summary.get("classifications"), dict)
        else {}
    )
    classified_launch_markets = tuple(
        dict.fromkeys(
            str(value or "").strip().upper()
            for value in list(classifications.get("launch_supported") or [])
            if str(value or "").strip()
        )
    )
    if set(launch_supported_markets) != set(expected_markets):
        errors.append("global market envelope does not launch-support exact AT/DE/CR scope")
    if set(classified_launch_markets) != set(expected_markets):
        errors.append("global market envelope launch classification is incomplete")
    if type(summary.get("market_count")) is not int or summary.get(
        "market_count"
    ) != len(expected_markets):
        errors.append("global market envelope summary market count is invalid")
    if type(summary.get("blocker_count")) is not int or summary.get(
        "blocker_count"
    ) != 0:
        errors.append("global market envelope summary reports blockers")

    market_rows = [
        dict(row)
        for row in list(receipt.get("markets") or [])
        if isinstance(row, dict)
    ]
    market_codes = [
        str(row.get("country_code") or "").strip().upper()
        for row in market_rows
    ]
    if len(market_codes) != len(set(market_codes)) or set(market_codes) != set(
        expected_markets
    ):
        errors.append("global market envelope market rows are not exact unique AT/DE/CR")
    for row in market_rows:
        country_code = str(row.get("country_code") or "").strip().upper()
        if country_code not in expected_markets:
            continue
        if (
            str(row.get("declared_classification") or "").strip()
            != "launch_supported"
            or str(row.get("computed_classification") or "").strip()
            != "launch_supported"
            or row.get("classification_match") is not True
            or row.get("launch_supported") is not True
            or str(row.get("status") or "").strip().upper() != "READY"
            or list(row.get("missing_dimensions") or [])
        ):
            errors.append(
                f"global market envelope {country_code} row is not fully launch-supported"
            )

    binding_errors, release_identity = _exact_release_binding_errors(
        receipt,
        expected_release_commit_sha=expected_release_commit_sha,
        expected_release_image_digest=expected_release_image_digest,
        label="global market envelope",
    )
    age_errors, generated_at, age_seconds = _governed_receipt_age(
        receipt,
        now=now,
        max_age_hours=max_age_hours,
        label="global market envelope",
    )
    errors.extend(binding_errors)
    errors.extend(age_errors)
    errors = list(dict.fromkeys(errors))
    return (
        not errors,
        {
            "status": "pass" if not errors else "blocked",
            "errors": errors,
            "receipt_status": receipt_status,
            "schema": schema,
            "release_identity": release_identity,
            "generated_at": generated_at,
            "age_seconds": age_seconds,
            "live_receipt_age_seconds": live_receipt_age_seconds,
            "required_markets": list(expected_markets),
            "launch_supported_markets": list(launch_supported_markets),
            "market_results": market_rows,
            "source_blockers": source_blockers,
        },
    )


def _incident_support_launch_status(
    receipt: dict[str, Any],
    *,
    receipt_present: bool,
    required: bool,
    expected_release_commit_sha: str,
    expected_release_image_digest: str,
    now: datetime | None,
    max_age_hours: float,
) -> tuple[bool, dict[str, Any]]:
    if not receipt_present:
        return (
            not required,
            {
                "status": "missing" if required else "not_required",
                "errors": (
                    ["incident/support gate receipt is required for launch/Core"]
                    if required
                    else []
                ),
                "receipt_status": "",
                "schema": "",
                "release_identity": {},
                "generated_at": "",
                "age_seconds": None,
                "live_receipt_age_seconds": None,
                "required_markets": list(ACTIVE_PROVIDER_MATRIX_COUNTRY_CODES),
                "source_blockers": [],
            },
        )

    errors: list[str] = []
    schema = str(receipt.get("schema") or "").strip()
    receipt_status = str(receipt.get("status") or "").strip()
    if schema != INCIDENT_SUPPORT_GATE_RECEIPT_SCHEMA:
        errors.append("incident/support gate receipt has the wrong schema")
    if receipt_status.lower() != "pass":
        errors.append("incident/support gate receipt is not pass")
    source_blockers = [str(value or "").strip() for value in list(receipt.get("blockers") or [])]
    source_blockers = [value for value in source_blockers if value]
    if source_blockers:
        errors.append("incident/support gate receipt still contains blockers")
    source_contract = (
        dict(receipt.get("source_contract") or {})
        if isinstance(receipt.get("source_contract"), dict)
        else {}
    )
    if str(source_contract.get("status") or "").strip().lower() != "pass":
        errors.append("incident/support source contract is not pass")
    if not re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        str(source_contract.get("sha256") or "").strip().lower(),
    ):
        errors.append("incident/support source contract digest is invalid")
    if not str(receipt.get("live_receipt_path") or "").strip():
        errors.append("incident/support gate has no independently attested live receipt")

    expected_markets = tuple(ACTIVE_PROVIDER_MATRIX_COUNTRY_CODES)
    required_markets = tuple(
        dict.fromkeys(
            str(value or "").strip().upper()
            for value in list(receipt.get("required_markets") or [])
            if str(value or "").strip()
        )
    )
    if set(required_markets) != set(expected_markets):
        errors.append("incident/support gate does not cover exact AT/DE/CR launch scope")

    live_receipt_age_seconds: float | None
    try:
        live_receipt_age_seconds = float(receipt.get("live_receipt_age_seconds"))
    except (TypeError, ValueError):
        live_receipt_age_seconds = None
    if (
        live_receipt_age_seconds is None
        or not math.isfinite(live_receipt_age_seconds)
        or live_receipt_age_seconds < 0
        or live_receipt_age_seconds > float(max_age_hours) * 3600
    ):
        errors.append("incident/support live attestation is stale, future-dated, or missing")

    binding_errors, release_identity = _exact_release_binding_errors(
        receipt,
        expected_release_commit_sha=expected_release_commit_sha,
        expected_release_image_digest=expected_release_image_digest,
        label="incident/support gate",
    )
    age_errors, generated_at, age_seconds = _governed_receipt_age(
        receipt,
        now=now,
        max_age_hours=max_age_hours,
        label="incident/support gate",
    )
    errors.extend(binding_errors)
    errors.extend(age_errors)
    errors = list(dict.fromkeys(errors))
    return (
        not errors,
        {
            "status": "pass" if not errors else "blocked",
            "errors": errors,
            "receipt_status": receipt_status,
            "schema": schema,
            "release_identity": release_identity,
            "generated_at": generated_at,
            "age_seconds": age_seconds,
            "live_receipt_age_seconds": live_receipt_age_seconds,
            "required_markets": list(required_markets),
            "source_blockers": source_blockers,
        },
    )


def _global_experience_launch_status(
    receipt: dict[str, Any],
    *,
    receipt_present: bool,
    required: bool,
    expected_release_commit_sha: str,
    expected_release_image_digest: str,
    now: datetime | None,
    max_age_hours: float,
) -> tuple[bool, dict[str, Any]]:
    expected_locales = {"AT": "de-AT", "DE": "de-DE", "CR": "es-CR"}
    if not receipt_present:
        return (
            not required,
            {
                "status": "missing" if required else "not_required",
                "errors": (
                    ["global-experience gate receipt is required for launch/Core"]
                    if required
                    else []
                ),
                "receipt_status": "",
                "schema": "",
                "release_identity": {},
                "generated_at": "",
                "age_seconds": None,
                "live_receipt_age_seconds": None,
                "required_markets": list(expected_locales),
                "market_results": [],
                "source_blockers": [],
                "independently_attested": False,
            },
        )

    errors: list[str] = []
    schema = str(receipt.get("schema") or "").strip()
    receipt_status = str(receipt.get("status") or "").strip()
    if schema != GLOBAL_EXPERIENCE_GATE_RECEIPT_SCHEMA:
        errors.append("global-experience gate receipt has the wrong schema")
    if receipt_status.lower() != "pass":
        errors.append("global-experience gate receipt is not pass")
    if (
        str(receipt.get("profile") or "").strip() != "launch"
        or str(receipt.get("claim_scope") or "").strip() != "core"
    ):
        errors.append("global-experience gate is not scoped to launch/Core")
    if str(receipt.get("source_contract_status") or "").strip() != (
        "defined_not_live_evidence"
    ):
        errors.append("global-experience source contract status is invalid")
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        str(receipt.get("contract_sha256") or "").strip().lower(),
    ):
        errors.append("global-experience source contract digest is invalid")
    if not str(receipt.get("live_receipt_path") or "").strip():
        errors.append("global-experience gate has no independently attested live receipt")
    if receipt.get("independently_attested") is not True:
        errors.append("global-experience gate is not independently attested")

    source_blockers = [
        str(value or "").strip() for value in list(receipt.get("blockers") or [])
    ]
    source_blockers = [value for value in source_blockers if value]
    if source_blockers:
        errors.append("global-experience gate receipt still contains blockers")

    required_rows = [
        dict(row)
        for row in list(receipt.get("required_markets") or [])
        if isinstance(row, dict)
    ]
    required_codes = [
        str(row.get("country_code") or "").strip().upper()
        for row in required_rows
    ]
    if (
        len(required_codes) != len(set(required_codes))
        or set(required_codes) != set(expected_locales)
        or any(
            str(row.get("locale") or "").strip()
            != expected_locales.get(str(row.get("country_code") or "").strip().upper())
            for row in required_rows
        )
    ):
        errors.append("global-experience gate does not cover exact AT/de-AT, DE/de-DE, and CR/es-CR scope")

    market_results = [
        dict(row)
        for row in list(receipt.get("market_results") or [])
        if isinstance(row, dict)
    ]
    market_codes = [
        str(row.get("country_code") or "").strip().upper()
        for row in market_results
    ]
    if len(market_codes) != len(set(market_codes)) or set(market_codes) != set(
        expected_locales
    ):
        errors.append("global-experience market results are not exact unique AT/DE/CR")
    for row in market_results:
        country_code = str(row.get("country_code") or "").strip().upper()
        if (
            str(row.get("locale") or "").strip()
            != expected_locales.get(country_code)
            or str(row.get("status") or "").strip().lower() != "pass"
            or list(row.get("blockers") or [])
        ):
            errors.append(
                f"global-experience {country_code or 'unknown'} market result is not pass"
            )

    live_receipt_age_seconds: float | None
    try:
        live_receipt_age_seconds = float(receipt.get("live_receipt_age_seconds"))
    except (TypeError, ValueError):
        live_receipt_age_seconds = None
    if (
        live_receipt_age_seconds is None
        or not math.isfinite(live_receipt_age_seconds)
        or live_receipt_age_seconds < 0
        or live_receipt_age_seconds > float(max_age_hours) * 3600
    ):
        errors.append("global-experience live attestation is stale, future-dated, or missing")
    try:
        declared_max_age_hours = float(receipt.get("maximum_age_hours"))
    except (TypeError, ValueError):
        declared_max_age_hours = 0.0
    if (
        not math.isfinite(declared_max_age_hours)
        or declared_max_age_hours <= 0
        or declared_max_age_hours > min(24.0, float(max_age_hours))
    ):
        errors.append("global-experience evidence freshness policy is missing or weakened")

    binding_errors, release_identity = _exact_release_binding_errors(
        receipt,
        expected_release_commit_sha=expected_release_commit_sha,
        expected_release_image_digest=expected_release_image_digest,
        label="global-experience gate",
    )
    age_errors, generated_at, age_seconds = _governed_receipt_age(
        receipt,
        now=now,
        max_age_hours=max_age_hours,
        label="global-experience gate",
    )
    errors.extend(binding_errors)
    errors.extend(age_errors)
    errors = list(dict.fromkeys(errors))
    return (
        not errors,
        {
            "status": "pass" if not errors else "blocked",
            "errors": errors,
            "receipt_status": receipt_status,
            "schema": schema,
            "release_identity": release_identity,
            "generated_at": generated_at,
            "age_seconds": age_seconds,
            "live_receipt_age_seconds": live_receipt_age_seconds,
            "maximum_age_hours": declared_max_age_hours,
            "required_markets": required_rows,
            "market_results": market_results,
            "source_blockers": source_blockers,
            "independently_attested": receipt.get("independently_attested") is True,
        },
    )


def _jurisdiction_privacy_rights_launch_status(
    receipt: dict[str, Any],
    *,
    receipt_present: bool,
    required: bool,
    expected_release_commit_sha: str,
    expected_release_image_digest: str,
    now: datetime | None,
    max_age_hours: float,
) -> tuple[bool, dict[str, Any]]:
    expected_markets = tuple(ACTIVE_PROVIDER_MATRIX_COUNTRY_CODES)
    if not receipt_present:
        return (
            not required,
            {
                "status": "missing" if required else "not_required",
                "errors": (
                    [
                        "jurisdiction/privacy/provider-rights gate receipt is required for launch/Core"
                    ]
                    if required
                    else []
                ),
                "receipt_status": "",
                "schema": "",
                "release_identity": {},
                "generated_at": "",
                "age_seconds": None,
                "live_receipt_age_seconds": None,
                "required_markets": list(expected_markets),
                "source_contract": {},
                "market_envelope": {},
                "source_blockers": [],
            },
        )

    errors: list[str] = []
    schema = str(receipt.get("schema") or "").strip()
    receipt_status = str(receipt.get("status") or "").strip()
    if schema != JURISDICTION_PRIVACY_RIGHTS_GATE_RECEIPT_SCHEMA:
        errors.append("jurisdiction/privacy/provider-rights gate receipt has the wrong schema")
    if receipt_status.lower() != "pass":
        errors.append("jurisdiction/privacy/provider-rights gate receipt is not pass")
    source_blockers = [
        str(value or "").strip() for value in list(receipt.get("blockers") or [])
    ]
    source_blockers = [value for value in source_blockers if value]
    if source_blockers:
        errors.append(
            "jurisdiction/privacy/provider-rights gate receipt still contains blockers"
        )
    if not str(receipt.get("live_receipt_path") or "").strip():
        errors.append(
            "jurisdiction/privacy/provider-rights gate has no independently attested live receipt"
        )

    repo_root = Path(__file__).resolve().parents[1]
    expected_contract_path = JURISDICTION_PRIVACY_RIGHTS_CONTRACT_PATH.as_posix()
    expected_envelope_path = (
        JURISDICTION_PRIVACY_RIGHTS_MARKET_ENVELOPE_PATH.as_posix()
    )
    try:
        current_contract_sha256 = "sha256:" + hashlib.sha256(
            (repo_root / JURISDICTION_PRIVACY_RIGHTS_CONTRACT_PATH).read_bytes()
        ).hexdigest()
    except OSError:
        current_contract_sha256 = ""
        errors.append(
            "current jurisdiction/privacy/provider-rights source contract is unreadable"
        )
    try:
        current_market_envelope_sha256 = "sha256:" + hashlib.sha256(
            (
                repo_root / JURISDICTION_PRIVACY_RIGHTS_MARKET_ENVELOPE_PATH
            ).read_bytes()
        ).hexdigest()
    except OSError:
        current_market_envelope_sha256 = ""
        errors.append("current governed global market envelope is unreadable")

    source_contract = (
        dict(receipt.get("source_contract") or {})
        if isinstance(receipt.get("source_contract"), dict)
        else {}
    )
    if (
        str(source_contract.get("status") or "").strip().lower() != "pass"
        or str(source_contract.get("path") or "").strip() != expected_contract_path
        or str(source_contract.get("sha256") or "").strip().lower()
        != current_contract_sha256
    ):
        errors.append(
            "jurisdiction/privacy/provider-rights gate is not bound to the current source contract"
        )
    market_envelope = (
        dict(receipt.get("market_envelope") or {})
        if isinstance(receipt.get("market_envelope"), dict)
        else {}
    )
    if (
        str(market_envelope.get("status") or "").strip().lower() != "pass"
        or str(market_envelope.get("path") or "").strip()
        != expected_envelope_path
        or str(market_envelope.get("sha256") or "").strip().lower()
        != current_market_envelope_sha256
    ):
        errors.append(
            "jurisdiction/privacy/provider-rights gate is not bound to the current global market envelope"
        )

    required_markets = tuple(
        str(value or "").strip().upper()
        for value in list(receipt.get("required_markets") or [])
        if str(value or "").strip()
    )
    if required_markets != expected_markets:
        errors.append(
            "jurisdiction/privacy/provider-rights gate does not cover exact ordered AT/DE/CR scope"
        )

    live_receipt_age_seconds: float | None
    try:
        live_receipt_age_seconds = float(receipt.get("live_receipt_age_seconds"))
    except (TypeError, ValueError):
        live_receipt_age_seconds = None
    if (
        live_receipt_age_seconds is None
        or not math.isfinite(live_receipt_age_seconds)
        or live_receipt_age_seconds < 0
        or live_receipt_age_seconds > float(max_age_hours) * 3600
    ):
        errors.append(
            "jurisdiction/privacy/provider-rights live attestation is stale, future-dated, or missing"
        )

    binding_errors, release_identity = _exact_release_binding_errors(
        receipt,
        expected_release_commit_sha=expected_release_commit_sha,
        expected_release_image_digest=expected_release_image_digest,
        label="jurisdiction/privacy/provider-rights gate",
    )
    age_errors, generated_at, age_seconds = _governed_receipt_age(
        receipt,
        now=now,
        max_age_hours=max_age_hours,
        label="jurisdiction/privacy/provider-rights gate",
    )
    errors.extend(binding_errors)
    errors.extend(age_errors)
    errors = list(dict.fromkeys(errors))
    return (
        not errors,
        {
            "status": "pass" if not errors else "blocked",
            "errors": errors,
            "receipt_status": receipt_status,
            "schema": schema,
            "release_identity": release_identity,
            "generated_at": generated_at,
            "age_seconds": age_seconds,
            "live_receipt_age_seconds": live_receipt_age_seconds,
            "required_markets": list(required_markets),
            "source_contract": source_contract,
            "market_envelope": market_envelope,
            "current_source_contract_sha256": current_contract_sha256,
            "current_market_envelope_sha256": current_market_envelope_sha256,
            "source_blockers": source_blockers,
        },
    )


def build_gold_status_receipt(
    *,
    performance_receipt_path: Path,
    continuous_ux_receipt_path: Path | None = None,
    tour_control_receipt_path: Path,
    export_discovery_receipt_path: Path | None,
    import_manifest_receipt_path: Path | None = None,
    repair_canary_receipt_path: Path,
    provider_matrix_receipt_path: Path,
    billing_receipt_path: Path | None = None,
    live_mobile_receipt_path: Path | None = None,
    accessibility_receipt_path: Path | None = None,
    failure_state_receipt_path: Path | None = None,
    activation_to_value_receipt_path: Path | None = None,
    public_smoke_receipt_path: Path | None = None,
    authenticated_smoke_receipt_path: Path | None = None,
    tour_provider_ownership_receipt_path: Path | None = None,
    vendor_tooling_receipt_path: Path | None = None,
    whole_project_scope_receipt_path: Path | None = None,
    security_posture_receipt_path: Path | None = None,
    release_hygiene_receipt_path: Path | None = None,
    furniture_style_contract_receipt_path: Path | None = None,
    bts_methodology_contract_receipt_path: Path | None = None,
    tour_delivery_contract_receipt_path: Path | None = None,
    map_preview_flagship_receipt_path: Path | None = None,
    browser_3d_gate_receipt_path: Path | None = None,
    runtime_reconstruction_receipt_path: Path | None = None,
    service_generated_reconstruction_receipt_path: Path | None = None,
    walkthrough_quality_receipt_path: Path | None = None,
    walkthrough_provider_proof_receipt_path: Path | None = None,
    scene_video_readiness_receipt_path: Path | None = None,
    scene_video_readiness_verifier_receipt_path: Path | None = None,
    scene_video_runtime_status_receipt_path: Path | None = None,
    scene_video_provider_refresh_packet_path: Path | None = None,
    scene_video_provider_refresh_packet_verifier_receipt_path: Path | None = None,
    advanced_visual_binding_receipt_path: Path | None = None,
    slo_evidence_receipt_path: Path | None = None,
    evidence_overlay_receipt_path: Path | None = None,
    rybbit_evidence_receipt_path: Path | None = None,
    global_market_envelope_receipt_path: Path | None = None,
    incident_support_receipt_path: Path | None = None,
    global_experience_receipt_path: Path | None = None,
    jurisdiction_privacy_rights_receipt_path: Path | None = None,
    expected_release_commit_sha: str = "",
    expected_release_image_digest: str = "",
    expected_release_deployment_id: str = "",
    expected_release_manifest_sha256: str = "",
    expected_performance_chromium_executable_path: str = "",
    expected_performance_chromium_executable_sha256: str = "",
    expected_public_origin: str = "",
    expected_teable_origin: str = "",
    expected_teable_base_id_sha256: str = "",
    expected_evidence_overlay_phase: str = "staged",
    expected_rybbit_origin: str = "",
    expected_rybbit_site_id_sha256: str = "",
    slo_evidence_max_age_seconds: int = DEFAULT_SLO_EVIDENCE_MAX_AGE_SECONDS,
    evidence_overlay_max_age_hours: float = DEFAULT_EVIDENCE_OVERLAY_MAX_AGE_HOURS,
    rybbit_evidence_max_age_minutes: float = DEFAULT_RYBBIT_EVIDENCE_MAX_AGE_MINUTES,
    id_austria_receipt_path: Path | None = None,
    max_receipt_age_hours: float | None = None,
    provider_catalog_receipt_path: Path | None = None,
    readiness_profile: str = "standard",
    claim_scope: str | None = None,
    required_browser_engines: tuple[str, ...] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    normalized_expected_release_commit_sha = str(
        expected_release_commit_sha or ""
    ).strip().lower()
    normalized_expected_release_image_digest = str(
        expected_release_image_digest or ""
    ).strip().lower()
    normalized_expected_release_deployment_id = str(
        expected_release_deployment_id or ""
    ).strip()
    normalized_expected_release_manifest_sha256 = str(
        expected_release_manifest_sha256 or ""
    ).strip().lower()
    (
        normalized_readiness_profile,
        normalized_claim_scope,
        requested_readiness_profile,
    ) = _resolve_readiness_request(readiness_profile, claim_scope)
    flagship_profile = normalized_readiness_profile in {"flagship", "launch"}
    launch_profile = normalized_readiness_profile == "launch"
    advanced_visual_profile = normalized_claim_scope == "advanced_visual"
    core_launch_governance_required = launch_profile and not advanced_visual_profile
    if not advanced_visual_profile:
        export_discovery_receipt_path = None
        import_manifest_receipt_path = None
        vendor_tooling_receipt_path = None
        walkthrough_quality_receipt_path = None
        walkthrough_provider_proof_receipt_path = None
        scene_video_readiness_receipt_path = None
        scene_video_readiness_verifier_receipt_path = None
        scene_video_runtime_status_receipt_path = None
        scene_video_provider_refresh_packet_path = None
        scene_video_provider_refresh_packet_verifier_receipt_path = None
        advanced_visual_binding_receipt_path = None
    core_customer_ux_receipt_areas = tuple(
        area
        for area in FLAGSHIP_CUSTOMER_UX_RECEIPT_AREAS
        if area != "walkthrough_quality"
    )
    configured_browser_engines = _normalize_required_browser_engines(
        required_browser_engines
        if required_browser_engines is not None
        else tuple(
            engine.strip()
            for engine in os.environ.get(
                "PROPERTYQUARRY_FLAGSHIP_REQUIRED_BROWSER_ENGINES",
                ",".join(DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES),
            ).split(",")
            if engine.strip()
        )
    )
    if max_receipt_age_hours is not None and not math.isfinite(
        float(max_receipt_age_hours)
    ):
        raise ValueError("max_receipt_age_hours_must_be_finite")
    effective_max_receipt_age_hours = (
        DEFAULT_FLAGSHIP_MAX_RECEIPT_AGE_HOURS
        if flagship_profile
        and (max_receipt_age_hours is None or max_receipt_age_hours <= 0)
        else max_receipt_age_hours
    )
    flagship_customer_ux_receipt_paths: dict[str, Path | None] = {
        "continuous_ux": continuous_ux_receipt_path,
        "public_auth_surfaces": public_smoke_receipt_path,
        "authenticated_customer_surfaces": authenticated_smoke_receipt_path,
        "live_mobile_surfaces": live_mobile_receipt_path,
        "accessibility": accessibility_receipt_path,
        "failure_states": failure_state_receipt_path,
        "activation_to_value": activation_to_value_receipt_path,
        "billing_handoff": billing_receipt_path,
        "browser_rendered_3d": browser_3d_gate_receipt_path,
        "map_preview_flagship": map_preview_flagship_receipt_path,
        "walkthrough_quality": walkthrough_quality_receipt_path,
    }
    missing_flagship_customer_ux_receipts = [
        area
        for area in core_customer_ux_receipt_areas
        if flagship_customer_ux_receipt_paths.get(area) is None
    ] if flagship_profile else []
    performance = _load_json(performance_receipt_path)
    continuous_ux = (
        _load_json(continuous_ux_receipt_path)
        if continuous_ux_receipt_path is not None
        else {}
    )
    live_mobile = _load_json(live_mobile_receipt_path) if live_mobile_receipt_path is not None else {}
    accessibility = _load_json(accessibility_receipt_path) if accessibility_receipt_path is not None else {}
    failure_states = _load_json(failure_state_receipt_path) if failure_state_receipt_path is not None else {}
    activation_to_value = (
        _load_json(activation_to_value_receipt_path)
        if activation_to_value_receipt_path is not None
        else {}
    )
    public_smoke = _load_json(public_smoke_receipt_path) if public_smoke_receipt_path is not None else {}
    authenticated_smoke = _load_json(authenticated_smoke_receipt_path) if authenticated_smoke_receipt_path is not None else {}
    tour_controls = _load_json(tour_control_receipt_path)
    export_discovery = (
        _load_json(export_discovery_receipt_path)
        if export_discovery_receipt_path is not None
        else {}
    )
    import_manifest = _load_json(import_manifest_receipt_path) if import_manifest_receipt_path is not None else {}
    billing_receipt = _load_json(billing_receipt_path) if billing_receipt_path is not None else {}
    tour_provider_ownership = _load_json(tour_provider_ownership_receipt_path) if tour_provider_ownership_receipt_path is not None else {}
    vendor_tooling = _load_json(vendor_tooling_receipt_path) if vendor_tooling_receipt_path is not None else {}
    magicfit_renderer = _magicfit_renderer_summary(
        vendor_tooling,
        receipt_present=vendor_tooling_receipt_path is not None,
    )
    omagic_adapter = _omagic_adapter_summary(
        vendor_tooling,
        receipt_present=vendor_tooling_receipt_path is not None,
    )
    whole_project_scope = _load_json(whole_project_scope_receipt_path) if whole_project_scope_receipt_path is not None else {}
    security_posture = _load_json(security_posture_receipt_path) if security_posture_receipt_path is not None else {}
    release_hygiene = _load_json(release_hygiene_receipt_path) if release_hygiene_receipt_path is not None else {}
    furniture_style_contract = _load_json(furniture_style_contract_receipt_path) if furniture_style_contract_receipt_path is not None else {}
    bts_methodology_contract = _load_json(bts_methodology_contract_receipt_path) if bts_methodology_contract_receipt_path is not None else {}
    tour_delivery_contract = _load_json(tour_delivery_contract_receipt_path) if tour_delivery_contract_receipt_path is not None else {}
    map_preview_flagship = _load_json(map_preview_flagship_receipt_path) if map_preview_flagship_receipt_path is not None else {}
    browser_3d_gate = _load_json(browser_3d_gate_receipt_path) if browser_3d_gate_receipt_path is not None else {}
    runtime_reconstruction = _load_json(runtime_reconstruction_receipt_path) if runtime_reconstruction_receipt_path is not None else {}
    service_generated_reconstruction = (
        _load_json(service_generated_reconstruction_receipt_path)
        if service_generated_reconstruction_receipt_path is not None
        else {}
    )
    walkthrough_quality = _load_json(walkthrough_quality_receipt_path) if walkthrough_quality_receipt_path is not None else {}
    walkthrough_provider_proof = (
        _load_json(walkthrough_provider_proof_receipt_path)
        if walkthrough_provider_proof_receipt_path is not None
        else {}
    )
    scene_video_readiness = _load_json(scene_video_readiness_receipt_path) if scene_video_readiness_receipt_path is not None else {}
    scene_video_readiness_verifier = _load_json(scene_video_readiness_verifier_receipt_path) if scene_video_readiness_verifier_receipt_path is not None else {}
    scene_video_runtime_status = _load_json(scene_video_runtime_status_receipt_path) if scene_video_runtime_status_receipt_path is not None else {}
    scene_video_provider_refresh_packet = _load_json(scene_video_provider_refresh_packet_path) if scene_video_provider_refresh_packet_path is not None else {}
    scene_video_provider_refresh_packet_verifier = (
        _load_json(scene_video_provider_refresh_packet_verifier_receipt_path)
        if scene_video_provider_refresh_packet_verifier_receipt_path is not None
        else {}
    )
    advanced_visual_binding = (
        _load_json(advanced_visual_binding_receipt_path)
        if advanced_visual_binding_receipt_path is not None
        else {}
    )
    slo_evidence = (
        _load_json(slo_evidence_receipt_path)
        if slo_evidence_receipt_path is not None
        else {}
    )
    evidence_overlay_receipt, evidence_overlay_receipt_sha256 = (
        _load_json_snapshot(evidence_overlay_receipt_path)
        if evidence_overlay_receipt_path is not None
        else ({}, "")
    )
    rybbit_evidence_receipt = (
        _load_json(rybbit_evidence_receipt_path)
        if rybbit_evidence_receipt_path is not None
        else {}
    )
    global_market_envelope = (
        _load_json(global_market_envelope_receipt_path)
        if global_market_envelope_receipt_path is not None
        else {}
    )
    incident_support = (
        _load_json(incident_support_receipt_path)
        if incident_support_receipt_path is not None
        else {}
    )
    global_experience = (
        _load_json(global_experience_receipt_path)
        if global_experience_receipt_path is not None
        else {}
    )
    jurisdiction_privacy_rights = (
        _load_json(jurisdiction_privacy_rights_receipt_path)
        if jurisdiction_privacy_rights_receipt_path is not None
        else {}
    )
    id_austria_receipt = _load_json(id_austria_receipt_path) if id_austria_receipt_path is not None else {}
    repair_canary = _load_json(repair_canary_receipt_path)
    provider_catalog = _load_json(provider_catalog_receipt_path) if provider_catalog_receipt_path is not None else {}
    provider_matrix = _load_json(provider_matrix_receipt_path)
    receipt_freshness_ok, stale_receipts = _receipt_freshness_status(
        {
            "performance": performance,
            **({"continuous_ux": continuous_ux} if continuous_ux_receipt_path is not None else {}),
            "tour_controls": tour_controls,
            **(
                {"export_discovery": export_discovery}
                if export_discovery_receipt_path is not None
                else {}
            ),
            **({"billing_handoff": billing_receipt} if billing_receipt_path is not None else {}),
            **({"id_austria": id_austria_receipt} if id_austria_receipt_path is not None else {}),
            "repair_canary": repair_canary,
            **({"provider_catalog_smoke": provider_catalog} if provider_catalog_receipt_path is not None else {}),
            "provider_matrix": provider_matrix,
            **({"live_mobile_surfaces": live_mobile} if live_mobile_receipt_path is not None else {}),
            **({"accessibility": accessibility} if accessibility_receipt_path is not None else {}),
            **({"failure_states": failure_states} if failure_state_receipt_path is not None else {}),
            **({"activation_to_value": activation_to_value} if activation_to_value_receipt_path is not None else {}),
            **({"public_auth_surfaces": public_smoke} if public_smoke_receipt_path is not None else {}),
            **({"authenticated_customer_surfaces": authenticated_smoke} if authenticated_smoke_receipt_path is not None else {}),
            **({"tour_provider_ownership": tour_provider_ownership} if tour_provider_ownership_receipt_path is not None else {}),
            **({"vendor_tooling": vendor_tooling} if vendor_tooling_receipt_path is not None else {}),
            **({"whole_project_scope": whole_project_scope} if whole_project_scope_receipt_path is not None else {}),
            **({"security_posture": security_posture} if security_posture_receipt_path is not None else {}),
            **({"release_hygiene": release_hygiene} if release_hygiene_receipt_path is not None else {}),
            **({"furniture_style_contract": furniture_style_contract} if furniture_style_contract_receipt_path is not None else {}),
            **({"bts_methodology_contract": bts_methodology_contract} if bts_methodology_contract_receipt_path is not None else {}),
            **({"tour_delivery_contract": tour_delivery_contract} if tour_delivery_contract_receipt_path is not None else {}),
            **({"map_preview_flagship": map_preview_flagship} if map_preview_flagship_receipt_path is not None else {}),
            **({"browser_rendered_3d": browser_3d_gate} if browser_3d_gate_receipt_path is not None else {}),
            **({"generated_reconstruction_glb": runtime_reconstruction} if runtime_reconstruction_receipt_path is not None else {}),
            **(
                {"service_generated_reconstruction": service_generated_reconstruction}
                if service_generated_reconstruction_receipt_path is not None
                else {}
            ),
            **({"walkthrough_quality": walkthrough_quality} if walkthrough_quality_receipt_path is not None else {}),
            **(
                {"walkthrough_provider_proof": walkthrough_provider_proof}
                if walkthrough_provider_proof_receipt_path is not None
                else {}
            ),
            **({"scene_video_readiness": scene_video_readiness} if scene_video_readiness_receipt_path is not None else {}),
            **({"scene_video_readiness_verifier": scene_video_readiness_verifier} if scene_video_readiness_verifier_receipt_path is not None else {}),
            **({"scene_video_runtime_status": scene_video_runtime_status} if scene_video_runtime_status_receipt_path is not None else {}),
            **({"scene_video_provider_refresh_packet": scene_video_provider_refresh_packet} if scene_video_provider_refresh_packet_path is not None else {}),
            **({"scene_video_provider_refresh_packet_verifier": scene_video_provider_refresh_packet_verifier} if scene_video_provider_refresh_packet_verifier_receipt_path is not None else {}),
            **(
                {"advanced_visual_candidate_binding": advanced_visual_binding}
                if advanced_visual_binding_receipt_path is not None
                else {}
            ),
        },
        now=now,
        max_age_hours=effective_max_receipt_age_hours,
    )
    slo_evidence_ok, slo_evidence_details = _slo_evidence_status(
        slo_evidence,
        receipt_present=slo_evidence_receipt_path is not None,
        expected_release_commit_sha=expected_release_commit_sha,
        expected_release_image_digest=expected_release_image_digest,
        now=now,
        max_age_seconds=slo_evidence_max_age_seconds,
    )
    slo_evidence_required = flagship_profile or slo_evidence_receipt_path is not None
    evidence_overlay_required = launch_profile or evidence_overlay_receipt_path is not None
    requested_evidence_overlay_max_age_hours = float(evidence_overlay_max_age_hours)
    effective_evidence_overlay_max_age_hours = (
        min(
            requested_evidence_overlay_max_age_hours,
            DEFAULT_EVIDENCE_OVERLAY_MAX_AGE_HOURS,
        )
        if math.isfinite(requested_evidence_overlay_max_age_hours)
        and requested_evidence_overlay_max_age_hours > 0
        else 0.0
    )
    evidence_overlay_ok, evidence_overlay_details = (
        _evidence_overlay_launch_status(
            evidence_overlay_receipt,
            receipt_present=evidence_overlay_receipt_path is not None,
            receipt_sha256=evidence_overlay_receipt_sha256,
            expected_release_commit_sha=expected_release_commit_sha,
            expected_teable_origin=expected_teable_origin,
            expected_teable_base_id_sha256=expected_teable_base_id_sha256,
            expected_phase=expected_evidence_overlay_phase,
            max_age_hours=effective_evidence_overlay_max_age_hours,
            now=now,
        )
        if evidence_overlay_required
        else (True, {"status": "not_required", "errors": []})
    )
    rybbit_evidence_required = launch_profile or rybbit_evidence_receipt_path is not None
    requested_rybbit_evidence_max_age_minutes = float(rybbit_evidence_max_age_minutes)
    effective_rybbit_evidence_max_age_minutes = (
        min(
            requested_rybbit_evidence_max_age_minutes,
            DEFAULT_RYBBIT_EVIDENCE_MAX_AGE_MINUTES,
        )
        if math.isfinite(requested_rybbit_evidence_max_age_minutes)
        and requested_rybbit_evidence_max_age_minutes > 0
        else 0.0
    )
    rybbit_evidence_ok, rybbit_evidence_details = (
        _rybbit_launch_status(
            rybbit_evidence_receipt,
            receipt_present=rybbit_evidence_receipt_path is not None,
            expected_release_commit_sha=expected_release_commit_sha,
            expected_public_origin=expected_public_origin,
            expected_analytics_origin=expected_rybbit_origin,
            expected_site_id_sha256=expected_rybbit_site_id_sha256,
            max_age_minutes=effective_rybbit_evidence_max_age_minutes,
            now=now,
        )
        if rybbit_evidence_required
        else (True, {"status": "not_required", "errors": []})
    )
    requested_governance_max_age_hours = (
        float(effective_max_receipt_age_hours)
        if effective_max_receipt_age_hours is not None
        else DEFAULT_FLAGSHIP_MAX_RECEIPT_AGE_HOURS
    )
    effective_governance_max_age_hours = (
        min(
            requested_governance_max_age_hours,
            DEFAULT_FLAGSHIP_MAX_RECEIPT_AGE_HOURS,
        )
        if math.isfinite(requested_governance_max_age_hours)
        and requested_governance_max_age_hours > 0
        else DEFAULT_FLAGSHIP_MAX_RECEIPT_AGE_HOURS
    )
    global_market_envelope_ok, global_market_envelope_details = (
        _global_market_envelope_launch_status(
            global_market_envelope,
            receipt_present=global_market_envelope_receipt_path is not None,
            required=core_launch_governance_required,
            expected_release_commit_sha=expected_release_commit_sha,
            expected_release_image_digest=expected_release_image_digest,
            now=now,
            max_age_hours=effective_governance_max_age_hours,
        )
    )
    incident_support_ok, incident_support_details = _incident_support_launch_status(
        incident_support,
        receipt_present=incident_support_receipt_path is not None,
        required=core_launch_governance_required,
        expected_release_commit_sha=expected_release_commit_sha,
        expected_release_image_digest=expected_release_image_digest,
        now=now,
        max_age_hours=effective_governance_max_age_hours,
    )
    global_experience_ok, global_experience_details = (
        _global_experience_launch_status(
            global_experience,
            receipt_present=global_experience_receipt_path is not None,
            required=core_launch_governance_required,
            expected_release_commit_sha=expected_release_commit_sha,
            expected_release_image_digest=expected_release_image_digest,
            now=now,
            max_age_hours=effective_governance_max_age_hours,
        )
    )
    (
        jurisdiction_privacy_rights_ok,
        jurisdiction_privacy_rights_details,
    ) = _jurisdiction_privacy_rights_launch_status(
        jurisdiction_privacy_rights,
        receipt_present=jurisdiction_privacy_rights_receipt_path is not None,
        required=core_launch_governance_required,
        expected_release_commit_sha=expected_release_commit_sha,
        expected_release_image_digest=expected_release_image_digest,
        now=now,
        max_age_hours=effective_governance_max_age_hours,
    )

    missing_provider_modes = _missing_provider_modes(tour_controls)
    core_missing_provider_modes = _missing_profile_provider_modes(
        tour_controls,
        required_field="core_required_provider_modes",
        missing_field="core_missing_provider_modes",
        default_required_modes=CORE_REQUIRED_TOUR_PROVIDER_MODES,
    )
    advanced_visual_tour_missing_provider_modes = _missing_profile_provider_modes(
        tour_controls,
        required_field="advanced_visual_required_provider_modes",
        missing_field="advanced_visual_missing_provider_modes",
        default_required_modes=("magicfit",),
    )
    magicfit_playback = dict(tour_controls.get("magicfit_playback") or {})
    magicfit_ready = "magicfit" in {
        str(provider or "").strip().lower()
        for provider in list(tour_controls.get("ready_provider_modes") or [])
        if str(provider or "").strip()
    }
    walkthrough_provider_binding_claimed = bool(
        isinstance(walkthrough_quality.get("provider_media_binding"), dict)
        and walkthrough_quality.get("provider_media_binding")
    ) or bool(
        str(walkthrough_quality.get("provider_proof_receipt_path") or "").strip()
    )
    magicfit_playback_required = (
        magicfit_ready
        or advanced_visual_profile
        or walkthrough_provider_binding_claimed
    )
    magicfit_playback_ok = _magicfit_playback_receipt_ok(
        magicfit_playback,
        required=magicfit_playback_required,
    )
    provider_matrix_summary = dict(provider_matrix.get("targeted_search_matrix_summary") or {})
    provider_matrix_case_count = int(provider_matrix_summary.get("case_count") or provider_matrix_summary.get("executed_case_count") or 0)
    provider_matrix_passed_case_count = int(provider_matrix_summary.get("passed_case_count") or 0)
    provider_matrix_modes_ok = (
        provider_matrix_summary.get("all_search_ready_provider_modes_passed") is True
        or (
            provider_matrix_summary.get("missing_mode_pairs") == []
            and provider_matrix_case_count > 0
            and provider_matrix_passed_case_count == provider_matrix_case_count
        )
    )
    provider_matrix_country_scope_ok = (
        provider_matrix_summary.get("provider_country_scope_ok") is True
        or (
            provider_matrix.get("country_scope") == "all_search_ready"
            and bool(provider_matrix_summary.get("country_codes"))
        )
    )
    provider_matrix_target_context_ok = (
        provider_matrix_summary.get("target_context_country_scope_ok") is True
        or provider_matrix.get("country_scope") == "all_search_ready"
    )
    cross_country_sanitization_summary = dict(provider_matrix.get("cross_country_sanitization_summary") or {})
    cross_country_sanitization_ok = (
        cross_country_sanitization_summary.get("sanitization_ok") is True
        and int(cross_country_sanitization_summary.get("case_count") or 0) > 0
        and int(dict(cross_country_sanitization_summary.get("status_counts") or {}).get("fail") or 0) == 0
    )
    provider_catalog_smoke = _provider_catalog_smoke_summary(provider_catalog, provider_catalog_receipt_path)
    provider_catalog_smoke_ok = provider_catalog_receipt_path is None or provider_catalog_smoke.get("status") == "pass"
    provider_matrix_catalog_scope = _provider_matrix_catalog_scope_details(provider_matrix, provider_catalog)
    provider_matrix_catalog_scope_ok = (
        provider_catalog_receipt_path is None or provider_matrix_catalog_scope.get("ok") is True
    )
    research_performance_ok, missing_research_performance_checks, research_performance_path = _performance_research_detail_checks(performance)
    search_performance_ok, missing_search_performance_checks, search_performance_path = _performance_search_checks(performance)
    analytics_ok, missing_analytics_checks, failed_analytics_checks, analytics_route_count = _performance_analytics_checks(performance)
    legacy_performance_ok = (
        performance.get("status") == "pass"
        and int(performance.get("failed_count") or 0) == 0
        and research_performance_ok
        and search_performance_ok
    )
    (
        flagship_authenticated_performance_ok,
        flagship_authenticated_performance_details,
    ) = _flagship_authenticated_performance_proof(
        performance,
        expected_public_origin=expected_public_origin,
        expected_release_commit_sha=normalized_expected_release_commit_sha,
        expected_release_image_digest=normalized_expected_release_image_digest,
        expected_release_deployment_id=normalized_expected_release_deployment_id,
        expected_release_manifest_sha256=(
            normalized_expected_release_manifest_sha256
        ),
        expected_chromium_executable_path=(
            expected_performance_chromium_executable_path
        ),
        expected_chromium_executable_sha256=(
            expected_performance_chromium_executable_sha256
        ),
    )
    performance_ok = legacy_performance_ok and (
        not flagship_profile or flagship_authenticated_performance_ok
    )
    flagship_continuous_ux_ok, flagship_continuous_ux_details = (
        _flagship_continuous_ux_proof(
            continuous_ux,
            expected_release_commit_sha=expected_release_commit_sha,
            required_browser_engines=configured_browser_engines,
        )
    )
    continuous_ux_ok = not flagship_profile or (
        continuous_ux_receipt_path is not None and flagship_continuous_ux_ok
    )
    live_mobile_covered_routes = _covered_live_mobile_routes(live_mobile)
    missing_live_mobile_routes = [
        route for route in REQUIRED_LIVE_MOBILE_ROUTES if route not in live_mobile_covered_routes
    ]
    missing_live_mobile_detail_routes = [
        prefix
        for prefix in REQUIRED_LIVE_MOBILE_DETAIL_PREFIXES
        if not any(_route_covers_required_detail(route, prefix) for route in live_mobile_covered_routes)
    ]
    failed_live_mobile_coverage_checks = _failed_live_mobile_coverage_checks(live_mobile)
    flagship_live_mobile_browser_ok, flagship_live_mobile_browser_details = _live_mobile_flagship_browser_proof(
        live_mobile,
        required_browser_engines=configured_browser_engines,
    )
    live_mobile_billing_availability = _live_mobile_billing_availability_details(live_mobile)
    live_mobile_ok = (
        (live_mobile_receipt_path is None and not flagship_profile)
        or (
            live_mobile.get("status") == "pass"
            and int(live_mobile.get("failed_count") or 0) == 0
            and int(live_mobile.get("route_count") or 0) >= len(REQUIRED_LIVE_MOBILE_ROUTES)
            and not missing_live_mobile_routes
            and not missing_live_mobile_detail_routes
            and not failed_live_mobile_coverage_checks
            and (not flagship_profile or flagship_live_mobile_browser_ok)
            and (not flagship_profile or live_mobile_billing_availability.get("available") is True)
        )
    )
    flagship_accessibility_ok, flagship_accessibility_details = _flagship_accessibility_proof(
        accessibility,
        required_browser_engines=configured_browser_engines,
    )
    accessibility_ok = not flagship_profile or (
        accessibility_receipt_path is not None and flagship_accessibility_ok
    )
    flagship_failure_states_ok, flagship_failure_states_details = _flagship_failure_state_proof(
        failure_states,
        required_browser_engines=configured_browser_engines,
    )
    failure_states_ok = not flagship_profile or (
        failure_state_receipt_path is not None and flagship_failure_states_ok
    )
    flagship_activation_to_value_ok, flagship_activation_to_value_details = _flagship_activation_to_value_proof(
        activation_to_value,
        expected_release_commit_sha=expected_release_commit_sha,
    )
    activation_to_value_ok = not flagship_profile or (
        activation_to_value_receipt_path is not None and flagship_activation_to_value_ok
    )
    public_sign_in_ok, missing_public_sign_in_checks, failed_public_sign_in_checks = _public_sign_in_checks(public_smoke)
    public_auth_ok = (
        (public_smoke_receipt_path is None and not flagship_profile)
        or (
            public_smoke.get("status") == "pass"
            and int(public_smoke.get("failed_count") or 0) == 0
            and public_sign_in_ok
        )
    )
    authenticated_billing_ok, missing_authenticated_billing_checks, failed_authenticated_billing_checks, authenticated_billing_status_code = _authenticated_billing_surface_checks(
        authenticated_smoke,
        require_available=flagship_profile,
    )
    authenticated_billing_route, _, _ = _route_named_checks(authenticated_smoke, "/app/billing")
    authenticated_billing_availability = _billing_route_availability_details(authenticated_billing_route)
    authenticated_notifications_ok, missing_authenticated_notification_checks, failed_authenticated_notification_checks = _authenticated_account_notification_checks(authenticated_smoke)
    authenticated_customer_ok = (
        (authenticated_smoke_receipt_path is None and not flagship_profile)
        or (
            authenticated_smoke.get("status") == "pass"
            and int(authenticated_smoke.get("failed_count") or 0) == 0
            and authenticated_billing_ok
            and authenticated_notifications_ok
        )
    )
    tour_control_status = str(tour_controls.get("status") or "").strip().lower()
    tour_controls_ok = (
        tour_control_status == "pass"
        or (
            tour_control_status == "blocked_missing_provider_modes"
            and not core_missing_provider_modes
        )
    ) and not core_missing_provider_modes and magicfit_playback_ok
    browser_3d_gate_ok = (
        (browser_3d_gate_receipt_path is None and not flagship_profile)
        or _hard_gate_receipt_ok(browser_3d_gate)
    )
    runtime_reconstruction_details = dict(runtime_reconstruction.get("details") or {})
    runtime_reconstruction_paths = dict(runtime_reconstruction_details.get("paths") or {})
    runtime_reconstruction_glb = dict(runtime_reconstruction_paths.get("glb") or {})
    runtime_reconstruction_browser_shell = dict(runtime_reconstruction.get("browser_shell") or {})
    runtime_reconstruction_public_contract = dict(runtime_reconstruction.get("public_route_contract") or {})
    runtime_reconstruction_browser_failures = [
        str(failure or "").strip()
        for failure in list(runtime_reconstruction_browser_shell.get("failures") or [])
        if str(failure or "").strip()
    ]
    runtime_reconstruction_public_failures = [
        str(failure or "").strip()
        for failure in (
            list(runtime_reconstruction.get("public_contract_failures") or [])
            or list(runtime_reconstruction_public_contract.get("failures") or [])
        )
        if str(failure or "").strip()
    ]
    runtime_reconstruction_walkthrough_status = str(runtime_reconstruction_details.get("walkthrough_status") or "")
    runtime_reconstruction_ok = (
        runtime_reconstruction_receipt_path is None
        or (
            runtime_reconstruction.get("status") == "pass"
            and runtime_reconstruction_browser_shell.get("status") == "pass"
            and runtime_reconstruction.get("browser_shell_ok") is True
            and runtime_reconstruction.get("public_route_contract_ok") is True
            and runtime_reconstruction.get("glb_non_empty") is True
            and runtime_reconstruction.get("glb_manifest_ok") is True
            and runtime_reconstruction.get("glb_capability_ok") is True
            and runtime_reconstruction.get("required_paths_ok") is True
            and runtime_reconstruction.get("honest_disclosure_ok") is True
            and runtime_reconstruction.get("route_label_quality_ok") is True
            and runtime_reconstruction.get("walkthrough_label_quality_ok") is True
            and runtime_reconstruction.get("walkthrough_generated_ok") is True
        )
    )
    service_generated_reconstruction_ok = (
        service_generated_reconstruction_receipt_path is None
        or (
            service_generated_reconstruction.get("status") == "pass"
            and service_generated_reconstruction.get("browser_shell_ok") is True
            and service_generated_reconstruction.get("required_paths_ok") is True
            and service_generated_reconstruction.get("top_level_video_contract_ok") is True
            and service_generated_reconstruction.get("route_label_quality_ok") is True
            and service_generated_reconstruction.get("walkthrough_generated_ok") is True
            and service_generated_reconstruction.get("delivery_contract_ok") is True
            and service_generated_reconstruction.get("public_route_contract_ok") is True
        )
    )
    walkthrough_provider_proof_required = (
        advanced_visual_profile
        or walkthrough_provider_proof_receipt_path is not None
        or walkthrough_provider_binding_claimed
    )
    walkthrough_provider_proof_ok = (
        not walkthrough_provider_proof_required
        or (
            walkthrough_provider_proof_receipt_path is not None
            and _walkthrough_provider_proof_receipt_ok(walkthrough_provider_proof)
        )
    )
    walkthrough_provider_binding_ok, walkthrough_provider_binding_details = (
        _walkthrough_quality_provider_binding_status(
            walkthrough_quality,
            walkthrough_provider_proof,
        )
        if walkthrough_provider_proof_required
        else (
            True,
            {
                "status": "not_required",
                "checks": {},
                "quality_binding": {},
                "provider_binding": {},
                "provenance_binding": {},
            },
        )
    )
    walkthrough_quality_ok = (
        (
            (walkthrough_quality_receipt_path is None and not flagship_profile)
            or _hard_gate_receipt_ok(walkthrough_quality)
        )
        and walkthrough_provider_binding_ok
    )
    scene_video_readiness_verifier_ok = (
        scene_video_readiness_verifier_receipt_path is None
        or (
            scene_video_readiness_verifier.get("status") == "pass"
            and not list(scene_video_readiness_verifier.get("blockers") or [])
        )
    )
    scene_video_provider_refresh_packet_verifier_ok = (
        scene_video_provider_refresh_packet_verifier_receipt_path is None
        or (
            scene_video_provider_refresh_packet_verifier.get("status") == "pass"
            and not list(scene_video_provider_refresh_packet_verifier.get("blockers") or [])
        )
    )
    scene_video_runtime_status_summary = _scene_video_runtime_status_summary(scene_video_runtime_status)
    scene_video_runtime_status_provider_rows = _scene_video_runtime_status_provider_rows(scene_video_runtime_status)
    scene_video_runtime_provider_names = {
        str(row.get("provider") or row.get("provider_key") or "").strip().lower()
        for row in scene_video_runtime_status_provider_rows
        if str(row.get("provider") or row.get("provider_key") or "").strip()
    }
    scene_video_runtime_ready_provider_names = {
        str(row.get("provider") or row.get("provider_key") or "").strip().lower()
        for row in scene_video_runtime_status_provider_rows
        if str(row.get("provider") or row.get("provider_key") or "").strip() and row.get("ready") is True
    }
    scene_video_verifier_checked_provider_names = {
        str(provider or "").strip().lower()
        for provider in list(scene_video_readiness_verifier.get("checked_providers") or [])
        if str(provider or "").strip()
    }
    scene_video_required_provider_set = set(REQUIRED_SCENE_VIDEO_PARITY_PROVIDERS)
    scene_video_runtime_missing_required_providers = (
        sorted(scene_video_required_provider_set - scene_video_runtime_provider_names)
        if scene_video_runtime_status_receipt_path is not None
        else []
    )
    scene_video_runtime_not_ready_required_providers = (
        sorted(scene_video_required_provider_set & (scene_video_runtime_provider_names - scene_video_runtime_ready_provider_names))
        if scene_video_runtime_status_receipt_path is not None
        else []
    )
    scene_video_verifier_missing_required_providers = (
        sorted(scene_video_required_provider_set - scene_video_verifier_checked_provider_names)
        if scene_video_readiness_verifier_receipt_path is not None
        else []
    )
    scene_video_required_provider_gap_set = (
        set(scene_video_runtime_missing_required_providers)
        | set(scene_video_runtime_not_ready_required_providers)
        | set(scene_video_verifier_missing_required_providers)
    )
    scene_video_required_provider_gaps = [
        provider
        for provider in REQUIRED_SCENE_VIDEO_PARITY_PROVIDERS
        if provider in scene_video_required_provider_gap_set
    ]
    scene_video_runtime_status_blocked_rows = [
        dict(row)
        for row in scene_video_runtime_status_provider_rows
        if row.get("ready") is not True
    ]
    scene_video_readiness_summary = (
        dict(scene_video_readiness.get("summary") or {})
        if isinstance(scene_video_readiness.get("summary"), dict)
        else {}
    )
    scene_video_provider_summary = scene_video_runtime_status_summary or scene_video_readiness_summary
    scene_video_readiness_next_actions = [
        dict(action)
        for action in list(scene_video_readiness.get("next_actions") or [])
        if isinstance(action, dict)
    ]
    scene_video_runtime_next_actions = _scene_video_runtime_status_next_actions(scene_video_runtime_status)
    scene_video_next_actions = scene_video_runtime_next_actions or scene_video_readiness_next_actions
    scene_video_blocked_provider_count = int(scene_video_provider_summary.get("blocked_count") or 0)
    scene_video_blocked_providers = [
        str(provider or "").strip()
        for provider in list(scene_video_provider_summary.get("blocked_providers") or [])
        if str(provider or "").strip()
    ]
    scene_video_action_providers = [
        str(action.get("provider") or "").strip()
        for action in scene_video_next_actions
        if str(action.get("provider") or "").strip()
    ]
    if not scene_video_blocked_providers:
        scene_video_blocked_providers = sorted(set(scene_video_action_providers))
    if not scene_video_blocked_providers and scene_video_runtime_status_blocked_rows:
        scene_video_blocked_providers = sorted(
            {
                str(row.get("provider") or row.get("provider_key") or "").strip()
                for row in scene_video_runtime_status_blocked_rows
                if str(row.get("provider") or row.get("provider_key") or "").strip()
            }
        )
    if scene_video_required_provider_gaps:
        for provider in scene_video_required_provider_gaps:
            if provider not in scene_video_blocked_providers:
                scene_video_blocked_providers.append(provider)
    if scene_video_blocked_provider_count == 0 and scene_video_blocked_providers:
        scene_video_blocked_provider_count = len(scene_video_blocked_providers)
    scene_video_provider_runtime_ready = (
        scene_video_readiness_receipt_path is not None
        and scene_video_blocked_provider_count == 0
        and not scene_video_next_actions
        and not scene_video_required_provider_gaps
    )
    scene_video_provider_action_required = (
        scene_video_readiness_receipt_path is not None
        and (scene_video_blocked_provider_count > 0 or bool(scene_video_next_actions) or bool(scene_video_required_provider_gaps))
    )
    advanced_visual_missing_provider_set = set(
        advanced_visual_tour_missing_provider_modes
    )
    if scene_video_runtime_status_receipt_path is None:
        advanced_visual_missing_provider_set.update(
            REQUIRED_SCENE_VIDEO_PARITY_PROVIDERS
        )
    else:
        advanced_visual_missing_provider_set.update(
            scene_video_required_provider_gaps
        )
    advanced_visual_missing_provider_modes = [
        provider
        for provider in ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES
        if provider in advanced_visual_missing_provider_set
    ]
    advanced_visual_missing_receipts = [
        name
        for name, path in (
            ("walkthrough_quality", walkthrough_quality_receipt_path),
            ("walkthrough_provider_proof", walkthrough_provider_proof_receipt_path),
            ("scene_video_readiness", scene_video_readiness_receipt_path),
            ("scene_video_readiness_verifier", scene_video_readiness_verifier_receipt_path),
            ("scene_video_runtime_status", scene_video_runtime_status_receipt_path),
            ("scene_video_provider_refresh_packet", scene_video_provider_refresh_packet_path),
            (
                "scene_video_provider_refresh_packet_verifier",
                scene_video_provider_refresh_packet_verifier_receipt_path,
            ),
            ("privacy", security_posture_receipt_path),
            ("advanced_visual_candidate_binding", advanced_visual_binding_receipt_path),
        )
        if path is None
    ]
    advanced_visual_source_receipt_paths = {
        "walkthrough_quality": walkthrough_quality_receipt_path,
        "walkthrough_provider_proof": walkthrough_provider_proof_receipt_path,
        "scene_video_readiness": scene_video_readiness_receipt_path,
        "scene_video_readiness_verifier": scene_video_readiness_verifier_receipt_path,
        "scene_video_runtime_status": scene_video_runtime_status_receipt_path,
        "scene_video_provider_refresh_packet": scene_video_provider_refresh_packet_path,
        "scene_video_provider_refresh_packet_verifier": (
            scene_video_provider_refresh_packet_verifier_receipt_path
        ),
        "privacy": security_posture_receipt_path,
    }
    advanced_visual_binding_errors: list[str] = []
    if advanced_visual_binding_receipt_path is None:
        advanced_visual_binding_errors.append("binding_receipt_missing")
    elif any(path is None for path in advanced_visual_source_receipt_paths.values()):
        advanced_visual_binding_errors.append("binding_source_receipts_missing")
    else:
        try:
            advanced_visual_binding_errors.extend(
                verify_advanced_visual_binding_receipt(
                    advanced_visual_binding,
                    expected_release_commit_sha=expected_release_commit_sha,
                    expected_release_image_digest=expected_release_image_digest,
                    source_receipt_paths={
                        name: path
                        for name, path in advanced_visual_source_receipt_paths.items()
                        if path is not None
                    },
                    max_age_hours=float(
                        effective_max_receipt_age_hours
                        or DEFAULT_FLAGSHIP_MAX_RECEIPT_AGE_HOURS
                    ),
                    now=now,
                )
            )
        except Exception as exc:
            advanced_visual_binding_errors.append(
                f"binding_verifier_error:{type(exc).__name__}"
            )
    advanced_visual_binding_ok = not advanced_visual_binding_errors
    advanced_visual_evidence_ok = (
        not advanced_visual_missing_provider_modes
        and not advanced_visual_missing_receipts
        and magicfit_playback_ok
        and _hard_gate_receipt_ok(walkthrough_quality)
        and _walkthrough_provider_proof_receipt_ok(walkthrough_provider_proof)
        and scene_video_readiness_verifier_ok
        and scene_video_provider_refresh_packet_verifier_ok
        and scene_video_provider_runtime_ready
        and not scene_video_provider_action_required
        and advanced_visual_binding_ok
    )
    billing_readiness = _billing_handoff_readiness_details(
        billing_receipt,
        authenticated_smoke=authenticated_smoke if authenticated_smoke_receipt_path is not None else None,
        require_live_no_second_login=flagship_profile,
    )
    billing_ok = (
        (billing_receipt_path is None and not flagship_profile)
        or bool(billing_readiness.get("ready"))
    )
    id_austria_details = _id_austria_gate_details(id_austria_receipt)
    id_austria_ok = id_austria_receipt_path is None or bool(id_austria_details.get("ready"))
    manifest_providers = {
        str(provider or "").strip().lower()
        for provider in list(import_manifest.get("providers") or [])
        if str(provider or "").strip()
    }
    import_manifest_status = str(import_manifest.get("status") or "").strip()
    import_manifest_not_needed = (
        not advanced_visual_profile
        or (
            import_manifest_receipt_path is not None
            and import_manifest_status not in {"missing", "invalid"}
            and not core_missing_provider_modes
            and int(import_manifest.get("import_count") or 0) == 0
        )
    )
    export_discovery_ok = (
        not advanced_visual_profile
        or export_discovery.get("status") in {"ready", "pass"}
        or import_manifest_not_needed
    )
    expected_import_providers = (
        set()
        if not advanced_visual_profile or import_manifest_not_needed
        else (manifest_providers or {"3dvista", "magicfit"})
    )
    prepared_drop_providers = set(_operator_drop_provider_rows(import_manifest))
    hardened_readmes_ok, hardened_readme_provider_count, missing_hardened_readme_providers, hardened_readme_failures = _operator_drop_readme_status(
        import_manifest,
        expected_providers=expected_import_providers,
    )
    operator_import_manifest_ready = (
        not advanced_visual_profile
        or import_manifest_not_needed
        or (
            import_manifest_status in {"ready_for_exports", "waiting_for_verified_assets", "partial_ready_for_import", "ready_for_import"}
            and int(import_manifest.get("import_count") or 0) >= len(expected_import_providers)
            and expected_import_providers.issubset(manifest_providers)
            and expected_import_providers.issubset(prepared_drop_providers)
            and hardened_readmes_ok
        )
    )
    repair_canary_ok = (
        repair_canary.get("status") == "pass"
        and repair_canary.get("run_status") == "completed_partial"
        and repair_canary.get("source_repair_status") == "returned"
        and repair_canary.get("receipt_resolution") == "provider_quarantined_retry_budget_exhausted"
    )
    provider_matrix_ok = (
        provider_matrix.get("status") == "pass"
        and provider_matrix.get("targeted_search_matrix_status") == "pass"
        and provider_matrix.get("targeted_search_matrix_executed") is True
        and provider_matrix_summary.get("executed") is True
        and provider_matrix_summary.get("all_search_ready_providers_covered") is True
        and provider_matrix_modes_ok
        and provider_matrix_summary.get("dispatch_acceptance_complete") is True
        and provider_matrix_summary.get("status_readback_complete") is True
        and provider_matrix_summary.get("payload_contracts_ok") is True
        and provider_matrix_country_scope_ok
        and provider_matrix_target_context_ok
        and provider_matrix_catalog_scope_ok
        and cross_country_sanitization_ok
        and provider_matrix_summary.get("agent_unlimited_results_ok") is True
        and provider_matrix_summary.get("strict_without_soft_filters_ok") is True
        and provider_matrix_summary.get("soft_filters_present_ok") is True
        and int(provider_matrix_summary.get("failed_case_count") or 0) == 0
    )
    whole_project_scope_ok = (
        whole_project_scope_receipt_path is None
        or (
            whole_project_scope.get("status") == "pass"
            and not list(whole_project_scope.get("failures") or [])
        )
    )
    security_posture_ok = (
        security_posture_receipt_path is None
        or (
            security_posture.get("status") == "pass"
            and not list(security_posture.get("failures") or [])
        )
    )
    release_hygiene_ok = (
        release_hygiene_receipt_path is None
        or (
            release_hygiene.get("status") == "pass"
            and not list(release_hygiene.get("failures") or [])
        )
    )
    release_hygiene_manifest_runtime_commit = str(release_hygiene.get("manifest_runtime_commit") or "")
    release_hygiene_head_commit = str(release_hygiene.get("head_commit") or "")
    try:
        release_hygiene_tracked_dirty_path_count = int(release_hygiene.get("tracked_dirty_path_count") or 0)
    except Exception:
        release_hygiene_tracked_dirty_path_count = 0
    furniture_style_contract_ok = (
        furniture_style_contract_receipt_path is None
        or (
            furniture_style_contract.get("schema") == "propertyquarry.furniture_style_contract_receipt.v2"
            and furniture_style_contract.get("status") == "pass"
            and not list(furniture_style_contract.get("failures") or [])
            and int(furniture_style_contract.get("style_count") or 0) >= 5
            and dict(furniture_style_contract.get("plan_caps") or {}) == {"free": 5, "plus": 5, "agent": 5}
            and dict(furniture_style_contract.get("helper_plan_caps") or {}) == {"free": 5, "plus": 5, "agent": 5}
            and str(furniture_style_contract.get("availability_mode") or "") == "per_visual_request"
            and furniture_style_contract.get("pricing_surface_bound") is True
        )
    )
    bts_methodology_contract_ok = (
        bts_methodology_contract_receipt_path is None
        or (
            bts_methodology_contract.get("status") == "pass"
            and not list(bts_methodology_contract.get("failures") or [])
            and int(bts_methodology_contract.get("source_section_count") or 0) >= 5
            and {"en", "de"}.issubset(set(bts_methodology_contract.get("languages") or []))
        )
    )
    tour_delivery_contract_ok = (
        tour_delivery_contract_receipt_path is None
        or (
            tour_delivery_contract.get("status") == "pass"
            and not list(tour_delivery_contract.get("failures") or [])
            and set(CORE_REQUIRED_TOUR_PROVIDER_MODES).issubset(
                set(tour_delivery_contract.get("ready_provider_modes") or [])
            )
            and (
                set(
                    tour_delivery_contract.get("core_required_provider_modes")
                    or tour_delivery_contract.get("required_provider_modes")
                    or tour_delivery_contract.get("required_providers")
                    or []
                )
                .intersection(CORE_REQUIRED_TOUR_PROVIDER_MODES)
                == set(CORE_REQUIRED_TOUR_PROVIDER_MODES)
            )
        )
    )
    map_preview_flagship_ok = (
        (map_preview_flagship_receipt_path is None and not flagship_profile)
        or _hard_gate_receipt_ok(map_preview_flagship)
    )

    blockers: list[dict[str, Any]] = []
    if core_launch_governance_required and not global_market_envelope_ok:
        blockers.append(
            {
                "area": "global_market_envelope",
                "status": global_market_envelope_details.get("status")
                or "blocked",
                "errors": list(global_market_envelope_details.get("errors") or []),
                "release_identity": dict(
                    global_market_envelope_details.get("release_identity") or {}
                ),
                "required_markets": list(
                    global_market_envelope_details.get("required_markets") or []
                ),
                "launch_supported_markets": list(
                    global_market_envelope_details.get("launch_supported_markets")
                    or []
                ),
                "source_blockers": list(
                    global_market_envelope_details.get("source_blockers") or []
                )[:20],
                "action": "materialize a fresh global-market-envelope launch receipt for exact AT/DE/CR scope with every market launch-supported, no missing dimensions, and the exact Gold release SHA/image identity",
            }
        )
    if core_launch_governance_required and not incident_support_ok:
        blockers.append(
            {
                "area": "incident_support",
                "status": incident_support_details.get("status") or "blocked",
                "errors": list(incident_support_details.get("errors") or []),
                "release_identity": dict(
                    incident_support_details.get("release_identity") or {}
                ),
                "required_markets": list(
                    incident_support_details.get("required_markets") or []
                ),
                "live_receipt_age_seconds": incident_support_details.get(
                    "live_receipt_age_seconds"
                ),
                "source_blockers": list(
                    incident_support_details.get("source_blockers") or []
                )[:20],
                "action": "run propertyquarry_incident_support_gate.py with a fresh independently attested AT/DE/CR live support receipt and the exact Gold release SHA/image identity",
            }
        )
    if core_launch_governance_required and not global_experience_ok:
        blockers.append(
            {
                "area": "global_experience",
                "status": global_experience_details.get("status") or "blocked",
                "errors": list(global_experience_details.get("errors") or []),
                "release_identity": dict(
                    global_experience_details.get("release_identity") or {}
                ),
                "required_markets": list(
                    global_experience_details.get("required_markets") or []
                ),
                "market_results": list(
                    global_experience_details.get("market_results") or []
                ),
                "live_receipt_age_seconds": global_experience_details.get(
                    "live_receipt_age_seconds"
                ),
                "independently_attested": global_experience_details.get(
                    "independently_attested"
                ),
                "source_blockers": list(
                    global_experience_details.get("source_blockers") or []
                )[:20],
                "action": "run propertyquarry_global_experience_gate.py with fresh independently attested AT/DE/CR native-language, WCAG 2.2 AA, manual accessibility, tri-engine/mobile, field-CWV, degraded-network, and localized-SEO evidence bound to the exact Gold release SHA/image",
            }
        )
    if core_launch_governance_required and not jurisdiction_privacy_rights_ok:
        blockers.append(
            {
                "area": "jurisdiction_privacy_rights",
                "status": jurisdiction_privacy_rights_details.get("status")
                or "blocked",
                "errors": list(
                    jurisdiction_privacy_rights_details.get("errors") or []
                ),
                "release_identity": dict(
                    jurisdiction_privacy_rights_details.get("release_identity")
                    or {}
                ),
                "required_markets": list(
                    jurisdiction_privacy_rights_details.get("required_markets")
                    or []
                ),
                "source_contract": dict(
                    jurisdiction_privacy_rights_details.get("source_contract")
                    or {}
                ),
                "market_envelope": dict(
                    jurisdiction_privacy_rights_details.get("market_envelope")
                    or {}
                ),
                "live_receipt_age_seconds": jurisdiction_privacy_rights_details.get(
                    "live_receipt_age_seconds"
                ),
                "source_blockers": list(
                    jurisdiction_privacy_rights_details.get("source_blockers")
                    or []
                )[:20],
                "action": "run propertyquarry_jurisdiction_privacy_rights_gate.py with a fresh independently attested AT/DE/CR legal, privacy-residency, consumer-rights, and exact provider-permission receipt bound to the current contract, current global market envelope, and exact Gold release SHA/image",
            }
        )
    if slo_evidence_required and not slo_evidence_ok:
        blockers.append(
            {
                "area": "slo_evidence",
                "status": slo_evidence_details.get("status") or "blocked",
                "errors": list(slo_evidence_details.get("errors") or []),
                "captured_at": str(slo_evidence_details.get("captured_at") or ""),
                "age_seconds": slo_evidence_details.get("age_seconds"),
                "max_age_seconds": slo_evidence_details.get("max_age_seconds"),
                "release_commit_sha": str(
                    slo_evidence_details.get("release_commit_sha") or ""
                ),
                "release_image_digest": str(
                    slo_evidence_details.get("release_image_digest") or ""
                ),
                "replica_id": str(slo_evidence_details.get("replica_id") or ""),
                "replica_count": slo_evidence_details.get("replica_count"),
                "action": "capture fresh authenticated private /internal/metrics evidence for the exact release image and replica set, then rerun propertyquarry_slo_evidence.py --flagship before promotion",
            }
        )
    if evidence_overlay_required and not evidence_overlay_ok:
        blockers.append(
            {
                "area": "evidence_overlay_read_model",
                "status": evidence_overlay_details.get("status") or "blocked",
                "errors": list(evidence_overlay_details.get("errors") or []),
                "candidate_sha": evidence_overlay_details.get("candidate_sha") or "",
                "layer_count": evidence_overlay_details.get("layer_count"),
                "record_count": evidence_overlay_details.get("record_count"),
                "query_p95_ms": evidence_overlay_details.get("query_p95_ms"),
                "query_budget_ms": evidence_overlay_details.get("query_budget_ms"),
                "action": "ingest a fresh exact eight-table Teable export into the indexed Postgres cached read model for this candidate, then supply its mode-600 receipt",
            }
        )
    if rybbit_evidence_required and not rybbit_evidence_ok:
        blockers.append(
            {
                "area": "rybbit_delivery",
                "status": rybbit_evidence_details.get("status") or "blocked",
                "errors": list(rybbit_evidence_details.get("errors") or []),
                "candidate_sha": rybbit_evidence_details.get("candidate_sha") or "",
                "collector_status_code": rybbit_evidence_details.get("collector_status_code"),
                "event_name": rybbit_evidence_details.get("event_name") or "",
                "event_count": rybbit_evidence_details.get("event_count"),
                "action": "run the protected Rybbit browser and authenticated API probe for this candidate until script delivery, collector acceptance, dashboard data, and launch-event arrival all pass",
            }
        )
    if missing_flagship_customer_ux_receipts:
        blockers.append(
            {
                "area": "flagship_customer_ux_evidence",
                "status": "missing_required_receipts",
                "missing_receipts": missing_flagship_customer_ux_receipts,
                "action": "generate every required deployed customer-UX receipt before claiming the PropertyQuarry flagship launch profile",
            }
        )
    if not continuous_ux_ok:
        blockers.append(
            {
                "area": "continuous_ux",
                "status": continuous_ux.get("status")
                or (
                    "not_configured"
                    if continuous_ux_receipt_path is None
                    else "missing"
                ),
                "proof": flagship_continuous_ux_details,
                "action": "run propertyquarry_continuous_ux_gate.py against an isolated loopback EA_STORAGE_BACKEND=memory app at the exact candidate SHA; fix cross-engine structural visual, 400% reflow, loading/error semantics, or the 3.2-second three-sample Chromium median first-value budget without treating this local receipt as deployed proof",
            }
        )
    if not performance_ok:
        blockers.append(
            {
                "area": "mobile_and_authenticated_surfaces",
                "status": performance.get("status") or "unknown",
                "flagship_status": performance.get("flagship_status"),
                "missing_research_detail_checks": missing_research_performance_checks,
                "missing_search_checks": missing_search_performance_checks,
                "flagship_proof": (
                    flagship_authenticated_performance_details
                    if flagship_profile
                    else None
                ),
                "action": (
                    "run propertyquarry_authenticated_performance_smoke.py against the exact deployed /app/search target with the protected read-only release-probe credential; require two fresh nonce-bound signed navigations, exact /version commit/image/deployment identity, the fixed low_end_mobile_lab_v1 Chromium CDP CPU/network/viewport/cache profile, independently valid cold and warm waterfall evidence, and no field Core Web Vitals or physical-device claim"
                    if flagship_profile
                    else "rerun and fix propertyquarry_authenticated_performance_smoke until every measured route passes"
                ),
            }
        )
    if not analytics_ok:
        blockers.append(
            {
                "area": "analytics_privacy",
                "status": performance.get("status") or "unknown",
                "missing_checks": missing_analytics_checks,
                "failed_checks": failed_analytics_checks,
                "action": "rerun propertyquarry_authenticated_performance_smoke.py and keep Rybbit taxonomy, no-identify, allowed-attribute, and private-payload checks passing on measured app routes",
            }
        )
    if not live_mobile_ok:
        blockers.append(
            {
                "area": "live_mobile_surfaces",
                "status": live_mobile.get("status") or ("not_configured" if live_mobile_receipt_path is None else "missing"),
                "missing_routes": missing_live_mobile_routes,
                "missing_detail_routes": missing_live_mobile_detail_routes,
                "failed_coverage_checks": failed_live_mobile_coverage_checks,
                "flagship_browser_proof": flagship_live_mobile_browser_details if flagship_profile else None,
                "billing_availability": live_mobile_billing_availability,
                "action": (
                    "run propertyquarry_live_mobile_surface_smoke.py --proof-mode browser-all against the deployed stack for every configured required browser engine and require /app/billing to open a resolving no-second-login account handoff while fixing every browser-measured viewport, overflow, touch, focus, navigation, detail, or logout regression"
                    if flagship_profile
                    else "run propertyquarry_live_mobile_surface_smoke.py against the deployed stack with PROPERTYQUARRY_LIVE_RESEARCH_DETAIL_ROUTE and fix any overflow, chrome, touch-target, detail, or logout regressions"
                ),
            }
        )
    if not accessibility_ok:
        blockers.append(
            {
                "area": "accessibility",
                "status": accessibility.get("status")
                or ("not_configured" if accessibility_receipt_path is None else "missing"),
                "proof": flagship_accessibility_details,
                "action": "run propertyquarry_accessibility_gate.py against every Gold static route plus concrete research-detail, shortlist-run, and public-tour routes in every configured browser engine with the pinned local axe-core input, then fix moderate-or-higher WCAG-tagged violations, 24 CSS-pixel target-size, unobscured keyboard focus, dialog, semantics, reflow, contrast, and reduced-motion failures; manual assistive-technology evidence remains separately required",
            }
        )
    if not failure_states_ok:
        blockers.append(
            {
                "area": "failure_states",
                "status": failure_states.get("status")
                or ("not_configured" if failure_state_receipt_path is None else "missing"),
                "proof": flagship_failure_states_details,
                "action": "run propertyquarry_failure_state_gate.py against pre-provisioned read-only 500, delayed, quota-blocked, payment-failed, empty, partial, provider-blocked, and missing-media canaries plus the deterministic 404, offline, expired-session, stale-link, and missing-packet routes in every required browser engine; prove the exact authenticated preferences snapshot has the same canonical digest before and after every state, and fix calm copy, semantics, recovery actions, or leaked diagnostics without mocking provider responses",
            }
        )
    if not activation_to_value_ok:
        blockers.append(
            {
                "area": "activation_to_value",
                "status": activation_to_value.get("status")
                or ("not_configured" if activation_to_value_receipt_path is None else "missing"),
                "proof": flagship_activation_to_value_details,
                "action": "run the protected propertyquarry_activation_to_value_live.py journey with an explicitly provisioned persona and unique run key, then require real authentication, real provider search/results, shortlist, research, walkthrough readiness, logout, relogin, and safe final logout to pass without test session injection",
            }
        )
    if not public_auth_ok:
        blockers.append(
            {
                "area": "public_auth_surfaces",
                "status": public_smoke.get("status") or ("not_configured" if public_smoke_receipt_path is None else "missing"),
                "missing_sign_in_checks": missing_public_sign_in_checks,
                "failed_sign_in_checks": failed_public_sign_in_checks,
                "action": "run propertyquarry_live_public_smoke.py against the deployed stack and fix provider sign-in account-creation copy, unavailable-copy, or provider-opening regressions",
            }
        )
    if not id_austria_ok:
        blockers.append(
            {
                "area": "id_austria_sign_in",
                "status": id_austria_details.get("status") or "missing",
                "required": bool(id_austria_details.get("required")),
                "configured": bool(id_austria_details.get("configured")),
                "error": str(id_austria_details.get("error") or ""),
                "missing_env": list(id_austria_details.get("missing_env") or []),
                "redirect_uri": str(id_austria_details.get("redirect_uri") or ""),
                "action": "either configure the production ID Austria OIDC contract or keep PROPERTYQUARRY_ID_AUSTRIA_REQUIRED unset so the sign-in lane stays explicitly fail-closed",
            }
        )
    if not authenticated_customer_ok:
        blockers.append(
            {
                "area": "authenticated_customer_surfaces",
                "status": authenticated_smoke.get("status") or ("not_configured" if authenticated_smoke_receipt_path is None else "missing"),
                "billing_status_code": authenticated_billing_status_code,
                "missing_billing_checks": missing_authenticated_billing_checks,
                "failed_billing_checks": failed_authenticated_billing_checks,
                "billing_availability": authenticated_billing_availability,
                "missing_notification_checks": missing_authenticated_notification_checks,
                "failed_notification_checks": failed_authenticated_notification_checks,
                "action": (
                    "run propertyquarry_live_authenticated_smoke.py against the deployed stack and prove /app/billing opens a resolving no-second-login account handoff for the paid persona while /app/account keeps the notification routing form usable"
                    if flagship_profile
                    else "run propertyquarry_live_authenticated_smoke.py against the deployed stack and keep /app/billing either redirected to a resolving billing portal or fail-closed without local billing-board copy, while /app/account keeps the notification routing form usable"
                ),
            }
        )
    if core_missing_provider_modes:
        blockers.append(
            {
                "area": "verified_tour_provider_modes",
                "missing_provider_modes": core_missing_provider_modes,
                "core_missing_provider_modes": core_missing_provider_modes,
                "action": _tour_provider_evidence_action(core_missing_provider_modes),
            }
        )
    if advanced_visual_missing_provider_modes or advanced_visual_missing_receipts:
        advanced_provider_details: dict[str, dict[str, Any]] = {}
        if "magicfit" in advanced_visual_missing_provider_modes:
            advanced_provider_details["magicfit"] = {
                "renderer_ready": magicfit_renderer.get("ready"),
                "renderer_status": magicfit_renderer.get("status"),
                "script_ready": magicfit_renderer.get("script_ready"),
                "credentials_configured": magicfit_renderer.get(
                    "credentials_configured"
                ),
                "missing_python_modules": magicfit_renderer.get(
                    "missing_python_modules"
                ),
                "next_action": magicfit_renderer.get("next_action"),
            }
        blockers.append(
            {
                "area": "advanced_visual_provider_modes",
                "status": "unavailable",
                "missing_provider_modes": advanced_visual_missing_provider_modes,
                "advanced_visual_missing_provider_modes": advanced_visual_missing_provider_modes,
                "missing_receipts": advanced_visual_missing_receipts,
                "action": "supply exact governed provider, playback, quota, privacy, and isolation receipts before selecting Advanced Visual Gold",
                **(
                    {"provider_details": advanced_provider_details}
                    if advanced_provider_details
                    else {}
                ),
            }
        )
    if not magicfit_playback_ok:
        blockers.append(
            {
                "area": "magicfit_walkthrough_playback",
                "status": "failed",
                "customer_claim_blocking": magicfit_ready,
                "playable_count": magicfit_playback.get("playable_count"),
                "ready_count": magicfit_playback.get("ready_count"),
                "action": "rerun verify_property_tour_controls.py and keep MagicFit ready only when every ready MagicFit control has local playable or live-probed video evidence",
            }
        )
    if not browser_3d_gate_ok:
        blockers.append(
            {
                "area": "browser_rendered_3d",
                "status": browser_3d_gate.get("status") or ("not_configured" if browser_3d_gate_receipt_path is None else "missing"),
                "failed_count": browser_3d_gate.get("failed_count"),
                "providers": browser_3d_gate.get("providers") or [],
                "provider_results": browser_3d_gate.get("provider_results") or [],
                "failed_checks": _failed_receipt_checks(browser_3d_gate),
                "action": "rerun propertyquarry_3d_browser_gate.py and only claim 3D readiness when every visible 3D tour renders in a real browser without CSP, frame, network, loading, or blank-viewer failures",
            }
        )
    if not runtime_reconstruction_ok:
        blockers.append(
            {
                "area": "generated_reconstruction_glb",
                "status": runtime_reconstruction.get("status") or ("not_configured" if runtime_reconstruction_receipt_path is None else "missing"),
                "glb_export_status": runtime_reconstruction_details.get("glb_export_status"),
                "glb_non_empty": runtime_reconstruction.get("glb_non_empty"),
                "glb_manifest_ok": runtime_reconstruction.get("glb_manifest_ok"),
                "glb_capability_ok": runtime_reconstruction.get("glb_capability_ok"),
                "required_paths_ok": runtime_reconstruction.get("required_paths_ok"),
                "route_label_quality_ok": runtime_reconstruction.get("route_label_quality_ok"),
                "walkthrough_label_quality_ok": runtime_reconstruction.get("walkthrough_label_quality_ok"),
                "walkthrough_generated_ok": runtime_reconstruction.get("walkthrough_generated_ok"),
                "walkthrough_status": runtime_reconstruction_walkthrough_status,
                "browser_shell_ok": runtime_reconstruction.get("browser_shell_ok"),
                "browser_shell_status": runtime_reconstruction_browser_shell.get("status"),
                "browser_shell_failures": runtime_reconstruction_browser_failures,
                "public_route_contract_ok": runtime_reconstruction.get("public_route_contract_ok"),
                "public_contract_failures": runtime_reconstruction_public_failures,
                "honest_disclosure_ok": runtime_reconstruction.get("honest_disclosure_ok"),
                "manifest_runtime_commit": release_hygiene_manifest_runtime_commit,
                "head_commit": release_hygiene_head_commit,
                "tracked_dirty_path_count": release_hygiene_tracked_dirty_path_count if release_hygiene_receipt_path is not None else None,
                "viewer_url": str(runtime_reconstruction.get("viewer_url") or ""),
                "action": "rerun property_runtime_reconstruction_smoke.py with --require-public-contract --require-browser-shell --require-glb --host-header propertyquarry.com; because propertyquarry-api and propertyquarry-render-tools run image-baked /app code rather than a bind-mounted repo, rebuild/recreate the runtime first whenever /version or release hygiene shows repo/runtime drift, then fix model export, honest generated-preview disclosure, walkthrough generation, launch-shell, layout-preview, or public-contract failures before claiming generated reconstruction readiness",
            }
        )
    if not service_generated_reconstruction_ok:
        blockers.append(
            {
                "area": "service_generated_reconstruction",
                "status": service_generated_reconstruction.get("status")
                or ("not_configured" if service_generated_reconstruction_receipt_path is None else "missing"),
                "required_paths_ok": service_generated_reconstruction.get("required_paths_ok"),
                "top_level_video_contract_ok": service_generated_reconstruction.get("top_level_video_contract_ok"),
                "route_label_quality_ok": service_generated_reconstruction.get("route_label_quality_ok"),
                "walkthrough_generated_ok": service_generated_reconstruction.get("walkthrough_generated_ok"),
                "delivery_contract_ok": service_generated_reconstruction.get("delivery_contract_ok"),
                "public_route_contract_ok": service_generated_reconstruction.get("public_route_contract_ok"),
                "browser_shell_ok": service_generated_reconstruction.get("browser_shell_ok"),
                "viewer_url": str(service_generated_reconstruction.get("viewer_url") or ""),
                "action": "rerun property_service_generated_reconstruction_smoke.py --container propertyquarry-api --require-public-contract --require-browser-shell --host-header propertyquarry.com --public-base-url https://propertyquarry.com and fix the service-owned bundle writer, launch shell, walkthrough contract, route-label, delivery, or public-contract failures before claiming generated reconstruction end-to-end coverage",
            }
        )
    if not map_preview_flagship_ok:
        blockers.append(
            {
                "area": "map_preview_flagship",
                "status": map_preview_flagship.get("status") or ("not_configured" if map_preview_flagship_receipt_path is None else "missing"),
                "failed_count": map_preview_flagship.get("failed_count"),
                "preview_count": map_preview_flagship.get("preview_count"),
                "failed_checks": _failed_receipt_checks(map_preview_flagship),
                "preview_results": list(map_preview_flagship.get("preview_results") or [])[:6],
                "action": "rerun propertyquarry_map_preview_flagship_gate.py and keep generated map thumbnails ready, non-placeholder, calm, readable, and free of excessive red overlays or artifact text",
            }
        )
    if not walkthrough_quality_ok:
        blockers.append(
            {
                "area": "walkthrough_quality",
                "customer_claim_blocking": walkthrough_quality_receipt_path is not None,
                "status": walkthrough_quality.get("status") or ("not_configured" if walkthrough_quality_receipt_path is None else "missing"),
                "failed_count": walkthrough_quality.get("failed_count"),
                "video_relpath": str(walkthrough_quality.get("video_relpath") or ""),
                "video_sha256": str(walkthrough_quality.get("video_sha256") or ""),
                "provider_media_binding": walkthrough_quality.get("provider_media_binding") or {},
                "provider_binding": walkthrough_provider_binding_details,
                "failed_checks": _failed_receipt_checks(walkthrough_quality),
                "action": "run propertyquarry_walkthrough_provider_proof_gate.py first, then rerun propertyquarry_walkthrough_quality_gate.py with --provider-proof-receipt pointing at that exact passing proof and the same tour root; fix media identity, digest, room coverage, duration, or frame-continuity failures",
            }
        )
    if not walkthrough_provider_proof_ok:
        blockers.append(
            {
                "area": "walkthrough_provider_proof",
                "customer_claim_blocking": (
                    walkthrough_provider_proof_receipt_path is not None
                    or walkthrough_provider_binding_claimed
                ),
                "status": walkthrough_provider_proof.get("status")
                or ("not_configured" if walkthrough_provider_proof_receipt_path is None else "missing"),
                "required_providers": ["magicfit", "omagic"],
                "required_orchestrators": ["ea"],
                "verified_providers": list(walkthrough_provider_proof.get("verified_providers") or []),
                "verified_orchestrators": list(walkthrough_provider_proof.get("verified_orchestrators") or []),
                "indexed_participants": list(walkthrough_provider_proof.get("indexed_participants") or []),
                "missing_providers": list(walkthrough_provider_proof.get("missing_providers") or []),
                "failed_count": walkthrough_provider_proof.get("failed_count"),
                "provider_results": list(walkthrough_provider_proof.get("provider_results") or [])[:4],
                "action": "run propertyquarry_walkthrough_provider_proof_gate.py after successful, non-disqualified MagicFit and OMagic hosted-tour renders; the receipt must index EA as governance_and_verification with media_authorship=false, readiness or adapter configuration alone is not provider proof, and audit runs must not consume quota",
            }
        )
    if not scene_video_readiness_verifier_ok:
        blockers.append(
            {
                "area": "scene_video_readiness",
                "status": scene_video_readiness_verifier.get("status") or ("not_configured" if scene_video_readiness_verifier_receipt_path is None else "missing"),
                "verifier_blockers": list(scene_video_readiness_verifier.get("blockers") or [])[:12],
                "provider_summary": scene_video_readiness.get("summary") or {},
                "action": "rerun property_scene_video_readiness_report.py and verify_property_scene_video_readiness.py, then repair any Mootion BrowserAct, Telegram, 1min isolation, MagicFit, or OMagic/Magic routing regressions",
            }
        )
    if not scene_video_provider_refresh_packet_verifier_ok:
        blockers.append(
            {
                "area": "scene_video_provider_refresh_packet",
                "status": scene_video_provider_refresh_packet_verifier.get("status") or ("not_configured" if scene_video_provider_refresh_packet_verifier_receipt_path is None else "missing"),
                "verifier_blockers": list(scene_video_provider_refresh_packet_verifier.get("blockers") or [])[:12],
                "checked_providers": scene_video_provider_refresh_packet_verifier.get("checked_providers") or [],
                "packet_path": str(scene_video_provider_refresh_packet_path) if scene_video_provider_refresh_packet_path is not None else "",
                "action": "rerun materialize_scene_video_provider_refresh_packet.py and verify_scene_video_provider_refresh_packet.py; do not modify ONEMIN_* credentials while refreshing MagicFit or OMagic/Magic accounts",
            }
        )
    if bool(omagic_adapter.get("runtime_checked")) and not bool(omagic_adapter.get("ready")):
        blockers.append(
            {
                "area": "omagic_model_upload_adapter_deploy",
                "status": omagic_adapter.get("status") or "missing",
                "script_ready": omagic_adapter.get("script_ready"),
                "runtime_script_ready": omagic_adapter.get("runtime_script_ready"),
                "runtime_script": omagic_adapter.get("runtime_script") or {},
                "action": str(omagic_adapter.get("next_action") or "")
                or "rebuild/redeploy the PropertyQuarry runtime so the OMagic model-upload adapter exists before claiming provider parity",
            }
        )
    if scene_video_provider_action_required:
        blockers.append(
            {
                "area": "scene_video_provider_runtime",
                "status": "action_required",
                "provider_runtime_ready": False,
                "provider_blocked_count": scene_video_blocked_provider_count,
                "blocked_providers": scene_video_blocked_providers,
                "provider_summary": scene_video_provider_summary,
                "next_actions": scene_video_next_actions[:12],
                "required_providers": list(REQUIRED_SCENE_VIDEO_PARITY_PROVIDERS),
                "missing_required_providers": scene_video_required_provider_gaps,
                "runtime_missing_required_providers": scene_video_runtime_missing_required_providers,
                "runtime_not_ready_required_providers": scene_video_runtime_not_ready_required_providers,
                "verifier_missing_required_providers": scene_video_verifier_missing_required_providers,
                "runtime_status_receipt_path": str(scene_video_runtime_status_receipt_path) if scene_video_runtime_status_receipt_path is not None else "",
                "runtime_status_providers": scene_video_runtime_status_blocked_rows[:12],
                "action": "clear the current scene-video provider runtime gaps, rerun property_scene_video_readiness_report.py, then refresh the gold receipt before claiming Crezlo-level video/provider parity",
            }
        )
    if not advanced_visual_binding_ok:
        blockers.append(
            {
                "area": "advanced_visual_candidate_binding",
                "status": str(advanced_visual_binding.get("status") or "blocked"),
                "errors": advanced_visual_binding_errors,
                "release_commit_sha": str(
                    advanced_visual_binding.get("release_commit_sha") or ""
                ),
                "release_image_digest": str(
                    advanced_visual_binding.get("release_image_digest") or ""
                ),
                "receipt_path": str(advanced_visual_binding_receipt_path)
                if advanced_visual_binding_receipt_path is not None
                else "",
                "action": "materialize a fresh offline advanced-visual binding for the exact release SHA/image and unchanged source receipt/media hashes, then rerun Gold without provider or network access",
            }
        )
    if not export_discovery_ok:
        blockers.append(
            {
                "area": "tour_export_drop",
                "status": export_discovery.get("status") or "unknown",
                "action": "place verified 3D-tour exports in the configured drop directory and rerun discovery/import",
            }
        )
    if not billing_ok:
        billing_handoff = billing_receipt.get("billing_handoff") if isinstance(billing_receipt.get("billing_handoff"), dict) else {}
        billing_sso_bridge = billing_receipt.get("billing_sso_bridge") if isinstance(billing_receipt.get("billing_sso_bridge"), dict) else {}
        member_token_handoff = billing_receipt.get("member_login_token_handoff") if isinstance(billing_receipt.get("member_login_token_handoff"), dict) else {}
        blockers.append(
            {
                "area": "billing_handoff",
                "status": billing_receipt.get("status") or "missing",
                "error": billing_receipt.get("error") or "",
                "host": str(billing_handoff.get("host") or ""),
                "host_resolves": bool(billing_handoff.get("host_resolves")),
                "required_dns_record": billing_handoff.get("required_dns_record") if isinstance(billing_handoff.get("required_dns_record"), dict) else {},
                "next_action": str(billing_handoff.get("next_action") or ""),
                "sso_bridge_ready": billing_sso_bridge.get("ready"),
                "sso_bridge_config_ready": billing_sso_bridge.get("config_ready"),
                "sso_bridge_exchange_checked": billing_sso_bridge.get("exchange_checked"),
                "sso_bridge_exchange_usable": billing_sso_bridge.get("exchange_usable"),
                "sso_bridge_error": str(billing_sso_bridge.get("error") or ""),
                "sso_bridge_next_action": str(billing_sso_bridge.get("next_action") or ""),
                "member_login_token_enabled": member_token_handoff.get("enabled"),
                "member_login_token_configured": member_token_handoff.get("configured"),
                "member_login_token_ready": member_token_handoff.get("ready"),
                "member_login_token_error": str(member_token_handoff.get("error") or ""),
                "member_login_token_next_action": str(member_token_handoff.get("next_action") or ""),
                "member_login_token_required_env": list(BILLING_MEMBER_TOKEN_REQUIRED_ENV),
                "ready_via": str(billing_readiness.get("ready_via") or ""),
                "signed_handoff_usable": bool(billing_readiness.get("signed_handoff_usable")),
                "provider_disabled": bool(billing_readiness.get("provider_disabled")),
                "launch_blocker_reason": (
                    "billing_intentionally_disabled"
                    if billing_readiness.get("provider_disabled") is True
                    else "no_verified_no_second_login_handoff"
                ),
                "live_smoke_external_handoff_usable": bool(billing_readiness.get("live_smoke_external_handoff_usable")),
                "live_smoke_no_second_login": bool(billing_readiness.get("live_smoke_no_second_login")),
                "admin_action": BILLING_MEMBER_TOKEN_ADMIN_ACTION,
                "action": (
                    "enable billing and configure the Brilliant Directories white-label host or signed member-login handoff so /app/billing opens a usable external account lane for the paid persona without a second login"
                    if billing_readiness.get("provider_disabled") is True
                    else "configure the Brilliant Directories white-label billing host or signed member-login handoff so /app/billing opens a usable external account lane without a second login"
                ),
            }
        )
    if import_manifest_receipt_path is not None and import_manifest_status in {"ready_for_exports", "waiting_for_verified_assets", "partial_ready_for_import", "ready_for_import"} and not hardened_readmes_ok:
        blockers.append(
            {
                "area": "tour_operator_drop_readmes",
                "status": "stale_or_missing",
                "missing_hardened_readme_providers": missing_hardened_readme_providers,
                "failures": hardened_readme_failures,
                "action": "rerun materialize_property_tour_export_manifest.py --prepare-dirs so each provider drop folder has current import and verification instructions",
            }
        )
    if import_manifest_receipt_path is not None and not operator_import_manifest_ready:
        blockers.append(
            {
                "area": "tour_operator_import_manifest",
                "status": import_manifest.get("status") or "missing",
                "missing_prepared_providers": sorted(expected_import_providers - prepared_drop_providers),
                "hardened_readmes_ok": hardened_readmes_ok,
                "action": "prepare the 3D tour, panorama, and walkthrough operator import lanes before claiming gold",
            }
        )
    if not repair_canary_ok:
        blockers.append(
            {
                "area": "self_healing_repair",
                "status": repair_canary.get("status") or "unknown",
                "action": "rerun and fix propertyquarry_repair_fleet_canary until failed provider sources are repaired or safely quarantined",
            }
        )
    if not provider_matrix_ok:
        blockers.append(
            {
                "area": "provider_targeted_search_matrix",
                "status": provider_matrix.get("status") or "unknown",
                "targeted_search_matrix_status": provider_matrix.get("targeted_search_matrix_status") or "unknown",
                "executed": bool(provider_matrix.get("targeted_search_matrix_executed")),
                "catalog_scope_ok": provider_matrix_catalog_scope_ok,
                "catalog_scope": provider_matrix_catalog_scope,
                "cross_country_sanitization_ok": cross_country_sanitization_ok,
                "action": "deploy the current provider catalog, then run property_live_provider_smoke.py for all search-ready countries with --execute-search-matrix so every current provider has strict/soft-filter evidence and wrong-country provider selections are sanitized before dispatch",
            }
        )
    if not provider_catalog_smoke_ok:
        blockers.append(
            {
                "area": "provider_catalog_smoke",
                "status": provider_catalog_smoke.get("raw_status") or provider_catalog_smoke.get("status") or "unknown",
                "failed_checks": provider_catalog_smoke.get("failed_checks") or [],
                "action": "rerun the post-deploy provider catalog smoke and fix provider/default-provider parity or country-scope failures before using the latest runtime catalog",
            }
        )
    if not whole_project_scope_ok:
        blockers.append(
            {
                "area": "whole_project_scope",
                "status": whole_project_scope.get("status") or "missing",
                "failures": list(whole_project_scope.get("failures") or [])[:12],
                "action": "rerun check_property_whole_project_scope.py --write and keep the evidence-overlay registry, Teable/cached-read-model policy, and whole-product scope contracts passing",
            }
        )
    if not security_posture_ok:
        blockers.append(
            {
                "area": "production_security_posture",
                "status": security_posture.get("status") or "missing",
                "failures": list(security_posture.get("failures") or [])[:12],
                "action": "rerun check_property_security_posture.py --write and keep the isolated runtime, pinned supply chain, public-tour privacy, and locked-dependency posture passing",
            }
        )
    if not release_hygiene_ok:
        blockers.append(
            {
                "area": "release_hygiene",
                "status": release_hygiene.get("status") or "missing",
                "manifest_runtime_commit": str(release_hygiene.get("manifest_runtime_commit") or ""),
                "head_commit": str(release_hygiene.get("head_commit") or ""),
                "failures": list(release_hygiene.get("failures") or [])[:12],
                "action": "rerun check_property_release_hygiene.py --write after reconciling the release manifest runtime commit with current HEAD or the deployed parent",
            }
        )
    if not furniture_style_contract_ok:
        blockers.append(
            {
                "area": "furniture_style_variants",
                "status": furniture_style_contract.get("status") or "missing",
                "style_count": furniture_style_contract.get("style_count"),
                "plan_caps": furniture_style_contract.get("plan_caps") or {},
                "helper_plan_caps": furniture_style_contract.get("helper_plan_caps") or {},
                "availability_mode": furniture_style_contract.get("availability_mode") or "",
                "pricing_surface_bound": furniture_style_contract.get("pricing_surface_bound") is True,
                "failures": list(furniture_style_contract.get("failures") or [])[:12],
                "action": "rerun check_property_furniture_style_contract.py --write and keep the five visible style choices, all-tier request-time choice, examples, UI handoff, and style-aware cached rendering contract passing",
            }
        )
    if not bts_methodology_contract_ok:
        blockers.append(
            {
                "area": "bts_methodology",
                "status": bts_methodology_contract.get("status") or "missing",
                "source_section_count": bts_methodology_contract.get("source_section_count"),
                "languages": bts_methodology_contract.get("languages") or [],
                "failures": list(bts_methodology_contract.get("failures") or [])[:12],
                "action": "rerun check_property_bts_methodology_contract.py --write and keep score-PDF provenance, official data-source copy, and selected-district no-reward policy passing",
            }
        )
    if not tour_delivery_contract_ok:
        blockers.append(
            {
                "area": "tour_delivery_contract_shape",
                "status": tour_delivery_contract.get("status") or "missing",
                "matterport_ready_count": tour_delivery_contract.get("matterport_ready_count"),
                "ready_provider_modes": tour_delivery_contract.get("ready_provider_modes") or [],
                "core_required_provider_modes": list(CORE_REQUIRED_TOUR_PROVIDER_MODES),
                "missing_provider_modes": tour_delivery_contract.get("missing_provider_modes") or [],
                "failures": list(tour_delivery_contract.get("failures") or [])[:12],
                "action": "rerun check_property_tour_delivery_contract.py --write and keep public-safe ready_payload, blocked_reason, required_to_send, white-label separation, and topology-verified Matterport readiness passing",
            }
        )
    if not receipt_freshness_ok:
        blockers.append(
            {
                "area": "receipt_freshness",
                "status": "stale_or_missing",
                "max_age_hours": effective_max_receipt_age_hours,
                "stale_receipts": stale_receipts,
                "action": "rerun the stale live smoke, tour, repair, provider, or discovery verifiers before claiming gold",
            }
        )

    next_required_actions = list(tour_controls.get("next_required_actions") or [])
    export_rejection_sample = [
        {
            "slug": str(row.get("slug") or ""),
            "provider": str(row.get("provider") or ""),
            "reason": str(row.get("reason") or ""),
            "action": str(row.get("action") or ""),
            "drop_layout": str(row.get("drop_layout") or ""),
            **({"file_count": row.get("file_count")} if "file_count" in row else {}),
            **({"present_sample": row.get("present_sample")} if "present_sample" in row else {}),
            **({"entry_candidates": row.get("entry_candidates")} if "entry_candidates" in row else {}),
            **({"missing": row.get("missing")} if "missing" in row else {}),
            **({"missing_markers": row.get("missing_markers")} if "missing_markers" in row else {}),
        }
        for row in list(export_discovery.get("rejected") or [])[:6]
        if isinstance(row, dict)
    ]
    export_repair_sample = [
        {
            "slug": str(row.get("slug") or ""),
            "provider": str(row.get("provider") or ""),
            "status": str(row.get("status") or ""),
            "reason": str(row.get("reason") or ""),
            "required_action": str(row.get("required_action") or ""),
            "drop_path": str(row.get("drop_path") or ""),
            "import_command_after_assets_arrive": str(row.get("import_command_after_assets_arrive") or ""),
            **({"file_count": row.get("file_count")} if "file_count" in row else {}),
            **({"present_sample": row.get("present_sample")} if "present_sample" in row else {}),
            **({"entry_candidates": row.get("entry_candidates")} if "entry_candidates" in row else {}),
            **({"missing": row.get("missing")} if "missing" in row else {}),
            **({"missing_markers": row.get("missing_markers")} if "missing_markers" in row else {}),
        }
        for row in list(export_discovery.get("repair_manifest") or [])[:6]
        if isinstance(row, dict)
    ]
    missing_export_rejection_sample = [
        row
        for row in export_rejection_sample
        if str(row.get("provider") or "").strip().lower() in set(missing_provider_modes)
    ]
    if export_discovery.get("status") == "blocked_no_verified_exports" and not import_manifest_not_needed:
        next_required_actions.append(
            {
                "provider": "_".join(missing_provider_modes) if missing_provider_modes else "verified_tour_exports",
                "action": (
                    missing_export_rejection_sample[0]["action"]
                    if missing_export_rejection_sample and missing_export_rejection_sample[0].get("action")
                    else _tour_provider_evidence_action(missing_provider_modes)
                ),
                "rejected_sample": missing_export_rejection_sample,
            }
        )
    if scene_video_provider_action_required:
        if scene_video_next_actions:
            next_required_actions.extend(
                {
                    "area": "scene_video_provider_runtime",
                    **action,
                    "action": str(action.get("action") or "").strip()
                    or "clear the scene-video provider runtime gap and rerun the scene-video readiness report",
                }
                for action in scene_video_next_actions
            )
        else:
            next_required_actions.append(
                {
                    "area": "scene_video_provider_runtime",
                    "provider": "_".join(scene_video_blocked_providers) if scene_video_blocked_providers else "scene_video",
                    "reason": "provider_runtime_not_ready",
                    "missing_required_providers": scene_video_required_provider_gaps,
                    "action": "clear the blocked scene-video providers and rerun property_scene_video_readiness_report.py",
                }
            )
    if "magicfit" in missing_provider_modes and vendor_tooling_receipt_path is not None and not bool(magicfit_renderer.get("ready")):
        next_required_actions.append(
            {
                "provider": "magicfit",
                "area": "magicfit_renderer",
                "renderer_status": magicfit_renderer.get("status"),
                "script_ready": magicfit_renderer.get("script_ready"),
                "credentials_configured": magicfit_renderer.get("credentials_configured"),
                "missing_python_modules": magicfit_renderer.get("missing_python_modules"),
                "action": (
                    str(magicfit_renderer.get("next_action") or "")
                    or "configure the MagicFit render lane before expecting a receipt-backed walkthrough"
                ),
            }
        )
    if bool(omagic_adapter.get("runtime_checked")) and not bool(omagic_adapter.get("ready")):
        next_required_actions.append(
            {
                "provider": "omagic",
                "area": "omagic_model_upload_adapter_deploy",
                "adapter_status": omagic_adapter.get("status"),
                "script_ready": omagic_adapter.get("script_ready"),
                "runtime_script_ready": omagic_adapter.get("runtime_script_ready"),
                "action": (
                    str(omagic_adapter.get("next_action") or "")
                    or "deploy the OMagic model-upload adapter before expecting model-backed walkthrough renders"
                ),
            }
        )

    operator_import_manifest_ok = import_manifest_receipt_path is None or operator_import_manifest_ready
    tour_provider_ownership_ok = (
        tour_provider_ownership_receipt_path is not None
        and tour_provider_ownership.get("status") == "pass"
        and not list(tour_provider_ownership.get("missing_providers") or [])
    )
    pass_areas = [
        {"area": "performance", "status": "pass", "receipt_path": str(performance_receipt_path)}
        if performance_ok
        else None,
        {"area": "analytics_privacy", "status": "pass", "receipt_path": str(performance_receipt_path)}
        if analytics_ok
        else None,
        {"area": "live_mobile_surfaces", "status": "pass", "receipt_path": str(live_mobile_receipt_path)}
        if live_mobile_receipt_path is not None and live_mobile_ok
        else None,
        {"area": "accessibility", "status": "pass", "receipt_path": str(accessibility_receipt_path)}
        if accessibility_receipt_path is not None and flagship_accessibility_ok
        else None,
        {"area": "failure_states", "status": "pass", "receipt_path": str(failure_state_receipt_path)}
        if failure_state_receipt_path is not None and flagship_failure_states_ok
        else None,
        {
            "area": "activation_to_value",
            "status": "pass",
            "receipt_path": str(activation_to_value_receipt_path),
        }
        if activation_to_value_receipt_path is not None and flagship_activation_to_value_ok
        else None,
        {"area": "public_auth_surfaces", "status": "pass", "receipt_path": str(public_smoke_receipt_path)}
        if public_smoke_receipt_path is not None and public_auth_ok
        else None,
        {"area": "authenticated_customer_surfaces", "status": "pass", "receipt_path": str(authenticated_smoke_receipt_path)}
        if authenticated_smoke_receipt_path is not None and authenticated_customer_ok
        else None,
        {
            "area": "tour_provider_ownership",
            "status": "pass",
            "providers": sorted((tour_provider_ownership.get("providers") or {}).keys()),
            "receipt_path": str(tour_provider_ownership_receipt_path),
            "note": "Ownership/config proof only; verified exports are still required for gold tour readiness.",
        }
        if tour_provider_ownership_ok
        else None,
        {
            "area": "provider_targeted_search_matrix",
            "status": "pass",
            "targeted_search_matrix_count": provider_matrix.get("targeted_search_matrix_count"),
            "cross_country_sanitization_case_count": cross_country_sanitization_summary.get("case_count"),
            "receipt_path": str(provider_matrix_receipt_path),
        }
        if provider_matrix_ok
        else None,
        {
            "area": "provider_catalog_smoke",
            "status": "pass",
            "raw_status": provider_catalog_smoke.get("raw_status"),
            "check_count": provider_catalog_smoke.get("check_count"),
            "receipt_path": provider_catalog_smoke.get("receipt_path"),
        }
        if provider_catalog_smoke_ok and provider_catalog_receipt_path is not None
        else None,
        {"area": "self_healing", "status": "pass", "receipt_path": str(repair_canary_receipt_path)}
        if repair_canary_ok
        else None,
        {"area": "whole_project_scope", "status": "pass", "receipt_path": str(whole_project_scope_receipt_path)}
        if whole_project_scope_receipt_path is not None and whole_project_scope_ok
        else None,
        {"area": "production_security_posture", "status": "pass", "receipt_path": str(security_posture_receipt_path)}
        if security_posture_receipt_path is not None and security_posture_ok
        else None,
        {
            "area": "release_hygiene",
            "status": "pass",
            "manifest_runtime_commit": str(release_hygiene.get("manifest_runtime_commit") or ""),
            "head_commit": str(release_hygiene.get("head_commit") or ""),
            "receipt_path": str(release_hygiene_receipt_path),
        }
        if release_hygiene_receipt_path is not None and release_hygiene_ok
        else None,
        {
            "area": "furniture_style_variants",
            "status": "pass",
            "style_count": furniture_style_contract.get("style_count"),
            "plan_caps": furniture_style_contract.get("plan_caps") or {},
            "helper_plan_caps": furniture_style_contract.get("helper_plan_caps") or {},
            "availability_mode": furniture_style_contract.get("availability_mode") or "",
            "pricing_surface_bound": furniture_style_contract.get("pricing_surface_bound") is True,
            "receipt_path": str(furniture_style_contract_receipt_path),
        }
        if furniture_style_contract_receipt_path is not None and furniture_style_contract_ok
        else None,
        {
            "area": "bts_methodology",
            "status": "pass",
            "language_count": bts_methodology_contract.get("language_count"),
            "source_section_count": bts_methodology_contract.get("source_section_count"),
            "receipt_path": str(bts_methodology_contract_receipt_path),
        }
        if bts_methodology_contract_receipt_path is not None and bts_methodology_contract_ok
        else None,
        {
            "area": "tour_delivery_contract_shape",
            "status": "pass",
            "matterport_ready_count": tour_delivery_contract.get("matterport_ready_count"),
            "ready_provider_modes": tour_delivery_contract.get("ready_provider_modes") or [],
            "receipt_path": str(tour_delivery_contract_receipt_path),
        }
        if tour_delivery_contract_receipt_path is not None and tour_delivery_contract_ok
        else None,
        {
            "area": "map_preview_flagship",
            "status": "pass",
            "preview_count": map_preview_flagship.get("preview_count"),
            "receipt_path": str(map_preview_flagship_receipt_path),
        }
        if map_preview_flagship_receipt_path is not None and map_preview_flagship_ok
        else None,
        {
            "area": "browser_rendered_3d",
            "status": "pass",
            "providers": browser_3d_gate.get("providers") or [],
            "receipt_path": str(browser_3d_gate_receipt_path),
        }
        if browser_3d_gate_receipt_path is not None and browser_3d_gate_ok
        else None,
        {
            "area": "generated_reconstruction_glb",
            "status": "pass",
            "viewer_url": str(runtime_reconstruction.get("viewer_url") or ""),
            "browser_shell_ok": runtime_reconstruction.get("browser_shell_ok"),
            "public_route_contract_ok": runtime_reconstruction.get("public_route_contract_ok"),
            "glb_size_bytes": runtime_reconstruction_glb.get("size_bytes"),
            "receipt_path": str(runtime_reconstruction_receipt_path),
        }
        if runtime_reconstruction_receipt_path is not None and runtime_reconstruction_ok
        else None,
        {
            "area": "service_generated_reconstruction",
            "status": "pass",
            "viewer_url": str(service_generated_reconstruction.get("viewer_url") or ""),
            "browser_shell_ok": service_generated_reconstruction.get("browser_shell_ok"),
            "delivery_contract_ok": service_generated_reconstruction.get("delivery_contract_ok"),
            "public_route_contract_ok": service_generated_reconstruction.get("public_route_contract_ok"),
            "receipt_path": str(service_generated_reconstruction_receipt_path),
        }
        if service_generated_reconstruction_receipt_path is not None and service_generated_reconstruction_ok
        else None,
        {
            "area": "walkthrough_quality",
            "status": "pass",
            "video_relpath": str(walkthrough_quality.get("video_relpath") or ""),
            "video_sha256": str(walkthrough_quality.get("video_sha256") or ""),
            "provider_media_binding": walkthrough_quality.get("provider_media_binding") or {},
            "receipt_path": str(walkthrough_quality_receipt_path),
        }
        if walkthrough_quality_receipt_path is not None and walkthrough_quality_ok
        else None,
        {
            "area": "walkthrough_provider_proof",
            "status": "pass",
            "verified_providers": list(walkthrough_provider_proof.get("verified_providers") or []),
            "verified_orchestrators": list(walkthrough_provider_proof.get("verified_orchestrators") or []),
            "indexed_participants": list(walkthrough_provider_proof.get("indexed_participants") or []),
            "receipt_path": str(walkthrough_provider_proof_receipt_path),
            "note": "Hard provider proof: MagicFit and OMagic each produced a non-disqualified, manifest-linked, decodable hosted walkthrough with provider-specific provenance; EA is verified only as governance and verification, never as media author.",
        }
        if walkthrough_provider_proof_receipt_path is not None and walkthrough_provider_proof_ok
        else None,
        {
            "area": "scene_video_readiness",
            "status": "pass",
            "checked_providers": scene_video_readiness_verifier.get("checked_providers") or [],
            "provider_count": scene_video_readiness_verifier.get("provider_count"),
            "provider_runtime_ready": scene_video_provider_runtime_ready,
            "provider_blocked_count": scene_video_blocked_provider_count,
            "provider_action_required": bool(scene_video_blocked_provider_count or scene_video_next_actions),
            "receipt_path": str(scene_video_readiness_receipt_path) if scene_video_readiness_receipt_path is not None else "",
            "verifier_receipt_path": str(scene_video_readiness_verifier_receipt_path),
            "runtime_status_receipt_path": str(scene_video_runtime_status_receipt_path) if scene_video_runtime_status_receipt_path is not None else "",
            "note": "Verifier pass means scene-video routing and actionability invariants hold; provider_runtime_ready shows whether MagicFit/OMagic provider capacity is actually clear from the normalized runtime-status receipt when present.",
        }
        if scene_video_readiness_verifier_receipt_path is not None and scene_video_readiness_verifier_ok
        else None,
        {
            "area": "scene_video_provider_refresh_packet",
            "status": "pass",
            "checked_providers": scene_video_provider_refresh_packet_verifier.get("checked_providers") or [],
            "provider_count": scene_video_provider_refresh_packet_verifier.get("provider_count"),
            "packet_path": str(scene_video_provider_refresh_packet_path) if scene_video_provider_refresh_packet_path is not None else "",
            "verifier_receipt_path": str(scene_video_provider_refresh_packet_verifier_receipt_path),
            "note": "Provider-refresh packet verifier pass means MagicFit and OMagic/Magic account refresh instructions are secret-safe and preserve the 1min no-touch boundary.",
        }
        if scene_video_provider_refresh_packet_verifier_receipt_path is not None and scene_video_provider_refresh_packet_verifier_ok
        else None,
        {
            "area": "advanced_visual_candidate_binding",
            "status": "pass",
            "release_commit_sha": str(
                advanced_visual_binding.get("release_commit_sha") or ""
            ),
            "release_image_digest": str(
                advanced_visual_binding.get("release_image_digest") or ""
            ),
            "receipt_path": str(advanced_visual_binding_receipt_path),
        }
        if advanced_visual_binding_receipt_path is not None
        and advanced_visual_binding_ok
        else None,
        {
            "area": "global_market_envelope",
            "status": "pass",
            "release_identity": dict(
                global_market_envelope_details.get("release_identity") or {}
            ),
            "launch_supported_markets": list(
                global_market_envelope_details.get("launch_supported_markets")
                or []
            ),
            "receipt_path": str(global_market_envelope_receipt_path),
        }
        if global_market_envelope_receipt_path is not None
        and global_market_envelope_ok
        else None,
        {
            "area": "incident_support",
            "status": "pass",
            "release_identity": dict(
                incident_support_details.get("release_identity") or {}
            ),
            "required_markets": list(
                incident_support_details.get("required_markets") or []
            ),
            "live_receipt_age_seconds": incident_support_details.get(
                "live_receipt_age_seconds"
            ),
            "receipt_path": str(incident_support_receipt_path),
        }
        if incident_support_receipt_path is not None and incident_support_ok
        else None,
        {
            "area": "global_experience",
            "status": "pass",
            "release_identity": dict(
                global_experience_details.get("release_identity") or {}
            ),
            "required_markets": list(
                global_experience_details.get("required_markets") or []
            ),
            "live_receipt_age_seconds": global_experience_details.get(
                "live_receipt_age_seconds"
            ),
            "receipt_path": str(global_experience_receipt_path),
        }
        if global_experience_receipt_path is not None and global_experience_ok
        else None,
        {
            "area": "jurisdiction_privacy_rights",
            "status": "pass",
            "release_identity": dict(
                jurisdiction_privacy_rights_details.get("release_identity") or {}
            ),
            "required_markets": list(
                jurisdiction_privacy_rights_details.get("required_markets") or []
            ),
            "source_contract": dict(
                jurisdiction_privacy_rights_details.get("source_contract") or {}
            ),
            "market_envelope": dict(
                jurisdiction_privacy_rights_details.get("market_envelope") or {}
            ),
            "receipt_path": str(jurisdiction_privacy_rights_receipt_path),
        }
        if jurisdiction_privacy_rights_receipt_path is not None
        and jurisdiction_privacy_rights_ok
        else None,
        {
            "area": "slo_evidence",
            "status": "pass",
            "release_commit_sha": slo_evidence_details.get("release_commit_sha"),
            "release_image_digest": slo_evidence_details.get("release_image_digest"),
            "replica_id": slo_evidence_details.get("replica_id"),
            "replica_count": slo_evidence_details.get("replica_count"),
            "receipt_path": str(slo_evidence_receipt_path),
        }
        if slo_evidence_required and slo_evidence_ok
        else None,
        {
            "area": "evidence_overlay_read_model",
            "status": "pass",
            "candidate_sha": evidence_overlay_details.get("candidate_sha"),
            "snapshot_id": evidence_overlay_details.get("snapshot_id"),
            "layer_count": evidence_overlay_details.get("layer_count"),
            "record_count": evidence_overlay_details.get("record_count"),
            "receipt_sha256": evidence_overlay_details.get("receipt_sha256"),
            "receipt_path": str(evidence_overlay_receipt_path),
        }
        if evidence_overlay_required and evidence_overlay_ok
        else None,
        {
            "area": "rybbit_delivery",
            "status": "pass",
            "candidate_sha": rybbit_evidence_details.get("candidate_sha"),
            "collector_status_code": rybbit_evidence_details.get("collector_status_code"),
            "event_name": rybbit_evidence_details.get("event_name"),
            "event_count": rybbit_evidence_details.get("event_count"),
            "receipt_path": str(rybbit_evidence_receipt_path),
        }
        if rybbit_evidence_required and rybbit_evidence_ok
        else None,
        {"area": "receipt_freshness", "status": "pass"}
        if receipt_freshness_ok
        else None,
    ]
    operator_blockers = list(blockers)
    core_blockers = [
        row
        for row in operator_blockers
        if str(row.get("area") or "").strip() not in ADVANCED_VISUAL_BLOCKER_AREAS
        or row.get("customer_claim_blocking") is True
    ]
    selected_profile_blockers = (
        operator_blockers if advanced_visual_profile else core_blockers
    )
    operator_next_required_actions = list(next_required_actions)
    core_next_required_actions = [
        row
        for row in operator_next_required_actions
        if str(row.get("area") or "").strip() not in ADVANCED_VISUAL_BLOCKER_AREAS
        and (
            not str(row.get("provider") or "").strip()
            or str(row.get("provider") or "").strip().lower()
            in set(CORE_REQUIRED_TOUR_PROVIDER_MODES)
        )
    ]
    selected_next_required_actions = (
        operator_next_required_actions
        if advanced_visual_profile
        else core_next_required_actions
    )
    core_conditions_ok = (
        not core_blockers
        and performance_ok
        and analytics_ok
        and live_mobile_ok
        and accessibility_ok
        and failure_states_ok
        and activation_to_value_ok
        and public_auth_ok
        and id_austria_ok
        and authenticated_customer_ok
        and tour_controls_ok
        and export_discovery_ok
        and operator_import_manifest_ok
        and billing_ok
        and repair_canary_ok
        and provider_catalog_smoke_ok
        and provider_matrix_ok
        and whole_project_scope_ok
        and security_posture_ok
        and release_hygiene_ok
        and furniture_style_contract_ok
        and bts_methodology_contract_ok
        and tour_delivery_contract_ok
        and map_preview_flagship_ok
        and browser_3d_gate_ok
        and runtime_reconstruction_ok
        and service_generated_reconstruction_ok
        and (
            walkthrough_quality_ok
            if walkthrough_quality_receipt_path is not None
            else True
        )
        and (
            walkthrough_provider_proof_ok
            if (
                walkthrough_provider_proof_receipt_path is not None
                or walkthrough_provider_binding_claimed
            )
            else True
        )
        and (not slo_evidence_required or slo_evidence_ok)
        and (not evidence_overlay_required or evidence_overlay_ok)
        and (not rybbit_evidence_required or rybbit_evidence_ok)
        and (
            not core_launch_governance_required
            or global_market_envelope_ok
        )
        and (not core_launch_governance_required or incident_support_ok)
        and (not core_launch_governance_required or global_experience_ok)
        and (
            not core_launch_governance_required
            or jurisdiction_privacy_rights_ok
        )
        and receipt_freshness_ok
    )
    status = (
        "pass"
        if (
            core_conditions_ok
            and not selected_profile_blockers
            and (not advanced_visual_profile or advanced_visual_evidence_ok)
        )
        else "blocked"
    )
    billing_provider_status = str(
        billing_receipt.get("status") or ("not_configured" if billing_receipt_path is None else "missing")
    )
    billing_handoff_status = (
        "ready"
        if billing_readiness.get("ready") is True
        else "not_checked_compatible"
        if billing_receipt_path is None and not flagship_profile
        else billing_provider_status
    )
    notes: list[str] = []
    if status == "pass":
        notes.extend(
            [
                "Current gold gate is green on the active proof set.",
                (
                    f"Provider E2E is current: {provider_matrix_case_count} targeted strict/soft cases passed across every "
                    "search-ready provider mode with wrong-country selections sanitized before dispatch."
                ),
                "Self-healing canary is current and proves repair-or-quarantine behavior for failed provider sources.",
                (
                    "Prepared operator import folders can still wait for future verified asset drops without blocking the current release."
                    if operator_import_manifest_ready and export_repair_sample
                    else "Prepared operator import folders are aligned with the active verified release."
                ),
            ]
        )
    else:
        notes.append("Gold remains blocked until every failing gate below is repaired.")
        if not core_missing_provider_modes:
            notes.append("Tour provider coverage is complete in the active verifier output.")
        if not repair_canary_ok:
            notes.append("Self-healing is proven only when the repair canary repairs or safely quarantines a failed provider source.")
        if not provider_matrix_ok:
            notes.append(
                "Provider E2E is proven only when every search-ready provider has executed strict and soft-filter targeted search cases."
            )
        if core_missing_provider_modes:
            notes.append("Every Core Gold tour provider mode must stay backed by verified evidence.")
        if not release_hygiene_ok:
            notes.append(
                "Release-manifest authority is blocked until /version matches the candidate commit; PropertyQuarry API and render containers run image-baked /app code, so host worktree changes do not count as runtime proof until rebuild/recreate."
            )
        if evidence_overlay_required and not evidence_overlay_ok:
            notes.append(
                "Evidence overlays are blocked until a fresh candidate-bound Teable export is atomically materialized and benchmarked through the exact eight-layer Postgres read model."
            )
        if rybbit_evidence_required and not rybbit_evidence_ok:
            notes.append(
                "Rybbit is blocked until the protected browser event is accepted by the collector and appears through the authenticated site/data/events APIs."
            )
        if core_launch_governance_required and not global_market_envelope_ok:
            notes.append(
                "Core launch is blocked until AT, DE, and CR are all launch-supported by a fresh market-envelope receipt bound to the exact release SHA and image."
            )
        if core_launch_governance_required and not incident_support_ok:
            notes.append(
                "Core launch is blocked until fresh independently attested incident/support coverage for AT, DE, and CR is bound to the exact release SHA and image."
            )
        if core_launch_governance_required and not global_experience_ok:
            notes.append(
                "Core launch is blocked until fresh independently attested native-language, WCAG 2.2 AA, manual accessibility, tri-engine/mobile, field-CWV, degraded-network recovery, and localized-SEO evidence for AT, DE, and CR is bound to the exact release SHA and image."
            )
        if core_launch_governance_required and not jurisdiction_privacy_rights_ok:
            notes.append(
                "Core launch is blocked until fresh independently attested jurisdiction, privacy-residency, consumer-rights, and provider-permission evidence for AT, DE, and CR is bound to the current contract, current global market envelope, and exact release SHA and image."
            )
        if billing_readiness.get("provider_disabled") is True:
            notes.append("Paid-persona billing is intentionally disabled, so flagship launch readiness remains blocked until a no-second-login account handoff is enabled and verified live.")
        if (
            advanced_visual_profile
            and "magicfit" in advanced_visual_missing_provider_modes
            and vendor_tooling_receipt_path is not None
            and not bool(magicfit_renderer.get("ready"))
        ):
            notes.append("MagicFit is still blocked on renderer configuration, not just a missing imported walkthrough asset.")
        if not browser_3d_gate_ok:
            notes.append("3D browser readiness is blocked until the viewer renders in Chromium, not merely until a tour route exists.")
        if not runtime_reconstruction_ok:
            notes.append(
                "Generated reconstruction readiness is blocked until a rebuilt/restarted live runtime matching the release manifest emits walkthrough video/sidecar proof alongside any required GLB export and keeps generated public routes fail-closed to shell/control surfaces instead of pretending a ready 3D tour exists."
            )
        if not service_generated_reconstruction_ok:
            notes.append("Service-generated reconstruction readiness is blocked until the app bundle writer emits the first-party walkthrough contract, human route labels, delivery metadata, and a browser-proven public-safe launch shell in a live smoke.")
        if not map_preview_flagship_ok:
            notes.append("Map preview readiness is blocked until generated thumbnails pass the visual-asset gate, not merely until the PNG route exists.")
        if not walkthrough_quality_ok:
            notes.append("Walkthrough readiness is blocked until room coverage and frame-continuity proof pass.")
        if not scene_video_readiness_verifier_ok:
            notes.append("Scene-video readiness is blocked until the verifier proves Mootion BrowserAct, Telegram, 1min isolation, and MagicFit/OMagic actionability invariants.")
        if scene_video_provider_action_required:
            notes.append("Scene-video provider runtime is blocked until MagicFit/Magic/OMagic account, credit, credential, and adapter gaps are cleared in the readiness receipt.")
        if bool(omagic_adapter.get("runtime_checked")) and not bool(omagic_adapter.get("ready")):
            notes.append("OMagic model-upload adapter deployment is blocked until the checked runtime contains the packaged adapter script.")
    advanced_visual_binding_state = str(
        advanced_visual_binding.get("binding_state") or ""
    ).strip()
    advanced_visual_unbound_producers = (
        advanced_visual_binding_state == UNBOUND_PRODUCER_STATE
        or UNBOUND_PRODUCER_STATE in advanced_visual_binding_errors
    )
    if advanced_visual_missing_provider_modes or advanced_visual_missing_receipts:
        notes.append(
            "Advanced Visual Gold is unavailable until its governed provider and receipt set is complete; this does not block Core Gold unless the advanced profile or a customer-facing walkthrough claim is selected."
        )
    if advanced_visual_unbound_producers:
        notes.append(
            "Advanced Visual Gold is unavailable_unbound_producer_receipts: the aggregate will not relabel producer receipts that lack exact source-side candidate/image identities or upstream receipt hashes."
        )
    notes.append(_tour_provider_missing_note(core_missing_provider_modes))
    advanced_visual_gold_status = (
        "pass"
        if advanced_visual_evidence_ok
        else "unavailable"
        if advanced_visual_missing_provider_modes
        or advanced_visual_missing_receipts
        or advanced_visual_unbound_producers
        else "blocked"
    )
    ready_for_notification = (
        status == "pass"
        and not selected_profile_blockers
        and not selected_next_required_actions
    )
    flagship_customer_ux_ready = (
        flagship_profile
        and not missing_flagship_customer_ux_receipts
        and continuous_ux_ok
        and live_mobile_ok
        and accessibility_ok
        and failure_states_ok
        and activation_to_value_ok
        and public_auth_ok
        and authenticated_customer_ok
        and billing_ok
        and browser_3d_gate_ok
        and map_preview_flagship_ok
        and (
            walkthrough_quality_ok
            if walkthrough_quality_receipt_path is not None
            else True
        )
        and receipt_freshness_ok
    )
    normalized_operator_blockers: list[dict[str, Any]] = []
    for row in operator_blockers:
        normalized_row = dict(row)
        normalized_area = str(normalized_row.get("area") or "").strip()
        if normalized_area and not str(normalized_row.get("key") or "").strip():
            normalized_row["key"] = normalized_area
        raw_status = str(normalized_row.get("status") or "").strip()
        if raw_status.lower() == "pass":
            normalized_row["receipt_status"] = raw_status
            normalized_row["status"] = "blocked"
            normalized_row.setdefault("blocking_reason", "required_checks_incomplete")
        normalized_operator_blockers.append(normalized_row)
    normalized_blockers = (
        normalized_operator_blockers
        if advanced_visual_profile
        else [
            row
            for row in normalized_operator_blockers
            if str(row.get("area") or "").strip()
            not in ADVANCED_VISUAL_BLOCKER_AREAS
            or row.get("customer_claim_blocking") is True
        ]
    )

    return {
        "schema": GOLD_STATUS_SCHEMA,
        "release_identity": {
            "commit_sha": normalized_expected_release_commit_sha,
            "image_digest": normalized_expected_release_image_digest,
        },
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "status": status,
        "ready_for_notification": ready_for_notification,
        "readiness_profile": normalized_readiness_profile,
        "requested_readiness_profile": requested_readiness_profile,
        "evidence_tier": normalized_readiness_profile,
        "claim_scope": normalized_claim_scope,
        "core_gold_status": "pass" if core_conditions_ok else "blocked",
        "advanced_visual_gold_status": advanced_visual_gold_status,
        "core_required_provider_modes": list(CORE_REQUIRED_TOUR_PROVIDER_MODES),
        "advanced_visual_required_provider_modes": list(
            ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES
        ),
        "core_missing_provider_modes": core_missing_provider_modes,
        "advanced_visual_missing_provider_modes": advanced_visual_missing_provider_modes,
        "operator_missing_provider_modes": sorted(
            set(core_missing_provider_modes)
            | set(advanced_visual_missing_provider_modes)
        ),
        "advanced_visual_gold": {
            "status": advanced_visual_gold_status,
            "selected": advanced_visual_profile,
            "required_provider_modes": list(
                ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES
            ),
            "missing_provider_modes": advanced_visual_missing_provider_modes,
            "missing_receipts": advanced_visual_missing_receipts,
            "provider_evidence_ready": advanced_visual_evidence_ok,
            "candidate_binding": {
                "ready": advanced_visual_binding_ok,
                "state": advanced_visual_binding_state,
                "errors": advanced_visual_binding_errors,
                "release_commit_sha": str(
                    advanced_visual_binding.get("release_commit_sha") or ""
                ),
                "release_image_digest": str(
                    advanced_visual_binding.get("release_image_digest") or ""
                ),
                "source_receipts": advanced_visual_binding.get("source_receipts")
                or {},
                "source_links": advanced_visual_binding.get("source_links") or {},
                "source_artifact_hashes": advanced_visual_binding.get(
                    "source_artifact_hashes"
                )
                or {},
                "receipt_path": str(advanced_visual_binding_receipt_path)
                if advanced_visual_binding_receipt_path is not None
                else "",
            },
            "note": "Advanced Visual Gold is additive and fail-closed; it cannot substitute configured adapters or provider availability for exact receipt, playback, quota, privacy, and isolation evidence.",
        },
        "flagship_customer_ux_evidence": {
            "required": flagship_profile,
            "ready": flagship_customer_ux_ready if flagship_profile else None,
            "required_receipts": list(core_customer_ux_receipt_areas),
            "missing_receipts": missing_flagship_customer_ux_receipts,
            "research_detail_required": flagship_profile,
            "browser_all_mobile_proof_required": flagship_profile,
            "browser_all_mobile_proof_ready": flagship_live_mobile_browser_ok if flagship_profile else None,
            "continuous_ux_proof_required": flagship_profile,
            "continuous_ux_proof_ready": flagship_continuous_ux_ok if flagship_profile else None,
            "accessibility_proof_required": flagship_profile,
            "accessibility_proof_ready": flagship_accessibility_ok if flagship_profile else None,
            "activation_to_value_proof_required": flagship_profile,
            "activation_to_value_proof_ready": flagship_activation_to_value_ok if flagship_profile else None,
            "required_browser_engines": list(configured_browser_engines) if flagship_profile else [],
            "live_mobile_billing_available": live_mobile_billing_availability.get("available") if flagship_profile else None,
            "authenticated_billing_available": authenticated_billing_availability.get("available") if flagship_profile else None,
            "max_receipt_age_hours": effective_max_receipt_age_hours if flagship_profile else max_receipt_age_hours,
        },
        "launch_product_data_evidence": {
            "required": launch_profile,
            "ready": (
                evidence_overlay_ok and rybbit_evidence_ok
                if launch_profile
                else None
            ),
            "evidence_overlay_read_model": {
                **evidence_overlay_details,
                "required": evidence_overlay_required,
                "max_age_hours": effective_evidence_overlay_max_age_hours,
                "receipt_path": str(evidence_overlay_receipt_path)
                if evidence_overlay_receipt_path is not None
                else "",
            },
            "rybbit_delivery": {
                **rybbit_evidence_details,
                "required": rybbit_evidence_required,
                "max_age_minutes": effective_rybbit_evidence_max_age_minutes,
                "receipt_path": str(rybbit_evidence_receipt_path)
                if rybbit_evidence_receipt_path is not None
                else "",
            },
            "note": "Launch requires a candidate-bound Teable-to-Postgres eight-layer read model and real Rybbit collector/dashboard delivery proof; registry fixtures and markup checks are supplemental only.",
        },
        "performance": {
            "schema": performance.get("schema"),
            "status": performance.get("status"),
            "flagship_status": performance.get("flagship_status"),
            "failed_count": performance.get("failed_count"),
            "route_count": performance.get("route_count"),
            "research_detail_path": research_performance_path,
            "research_detail_checks_ok": research_performance_ok,
            "missing_research_detail_checks": missing_research_performance_checks,
            "search_path": search_performance_path,
            "search_checks_ok": search_performance_ok,
            "missing_search_checks": missing_search_performance_checks,
            "flagship_proof_required": flagship_profile,
            "flagship_proof_ok": (
                flagship_authenticated_performance_ok
                if flagship_profile
                else None
            ),
            "flagship_proof": flagship_authenticated_performance_details,
            "receipt_path": str(performance_receipt_path),
        },
        "continuous_ux": {
            "status": continuous_ux.get("status")
            or (
                "not_configured"
                if continuous_ux_receipt_path is None
                else "missing"
            ),
            "required": flagship_profile,
            "supplemental_only": True,
            "production_claim": continuous_ux.get("production_claim"),
            "deployed_or_live_proof": continuous_ux.get("deployed_or_live_proof"),
            "flagship_proof_ok": flagship_continuous_ux_ok if flagship_profile else None,
            "flagship_proof": flagship_continuous_ux_details if flagship_profile else None,
            "receipt_path": (
                str(continuous_ux_receipt_path)
                if continuous_ux_receipt_path is not None
                else ""
            ),
            "note": "This additive isolated-loopback receipt cannot replace deployed mobile, accessibility, failure-state, activation, SLO, security, or release-hygiene evidence.",
        },
        "analytics": {
            "status": "pass" if analytics_ok else "fail",
            "route_count": analytics_route_count,
            "required_checks": list(REQUIRED_RYBBIT_PERFORMANCE_CHECKS),
            "missing_checks": missing_analytics_checks,
            "failed_checks": failed_analytics_checks,
            "receipt_path": str(performance_receipt_path),
            "note": "This proves app-side Rybbit privacy and taxonomy hygiene, not external dashboard delivery.",
        },
        "live_mobile_surfaces": {
            "status": live_mobile.get("status") or ("not_configured" if live_mobile_receipt_path is None else "missing"),
            "failed_count": live_mobile.get("failed_count"),
            "route_count": live_mobile.get("route_count"),
            "required_route_count": len(REQUIRED_LIVE_MOBILE_ROUTES),
            "missing_routes": missing_live_mobile_routes,
            "required_detail_prefixes": list(REQUIRED_LIVE_MOBILE_DETAIL_PREFIXES),
            "missing_detail_routes": missing_live_mobile_detail_routes,
            "failed_coverage_checks": failed_live_mobile_coverage_checks,
            "viewport": live_mobile.get("viewport"),
            "proof_mode": live_mobile.get("proof_mode"),
            "flagship_browser_proof_ok": flagship_live_mobile_browser_ok if flagship_profile else None,
            "flagship_browser_proof": flagship_live_mobile_browser_details if flagship_profile else None,
            "billing_availability": live_mobile_billing_availability,
            "receipt_path": str(live_mobile_receipt_path) if live_mobile_receipt_path is not None else "",
        },
        "accessibility": {
            "status": accessibility.get("status")
            or ("not_configured" if accessibility_receipt_path is None else "missing"),
            "failed_count": accessibility.get("failed_count"),
            "route_count": accessibility.get("route_count"),
            "axe_core_version": str(accessibility.get("axe_core_version") or ""),
            "flagship_proof_ok": flagship_accessibility_ok if flagship_profile else None,
            "flagship_proof": flagship_accessibility_details if flagship_profile else None,
            "receipt_path": str(accessibility_receipt_path) if accessibility_receipt_path is not None else "",
        },
        "failure_states": {
            "status": failure_states.get("status")
            or ("not_configured" if failure_state_receipt_path is None else "missing"),
            "failed_count": failure_states.get("failed_count"),
            "proof_mode": str(failure_states.get("proof_mode") or ""),
            "flagship_proof_ok": flagship_failure_states_ok if flagship_profile else None,
            "flagship_proof": flagship_failure_states_details if flagship_profile else None,
            "receipt_path": str(failure_state_receipt_path) if failure_state_receipt_path is not None else "",
        },
        "activation_to_value": {
            "status": activation_to_value.get("status")
            or ("not_configured" if activation_to_value_receipt_path is None else "missing"),
            "failed_count": activation_to_value.get("failed_count"),
            "release_commit_sha": str(activation_to_value.get("release_commit_sha") or ""),
            "auth_mode": str(activation_to_value.get("auth_mode") or ""),
            "browser_engine": str(activation_to_value.get("browser_engine") or ""),
            "proof_mode": str(activation_to_value.get("proof_mode") or ""),
            "flagship_proof_ok": flagship_activation_to_value_ok if flagship_profile else None,
            "flagship_proof": flagship_activation_to_value_details if flagship_profile else None,
            "receipt_path": (
                str(activation_to_value_receipt_path)
                if activation_to_value_receipt_path is not None
                else ""
            ),
        },
        "public_auth_surfaces": {
            "status": public_smoke.get("status") or ("not_configured" if public_smoke_receipt_path is None else "missing"),
            "failed_count": public_smoke.get("failed_count"),
            "route_count": public_smoke.get("route_count"),
            "sign_in_checks_ok": public_sign_in_ok if public_smoke_receipt_path is not None else None,
            "missing_sign_in_checks": missing_public_sign_in_checks if public_smoke_receipt_path is not None else [],
            "failed_sign_in_checks": failed_public_sign_in_checks if public_smoke_receipt_path is not None else [],
            "receipt_path": str(public_smoke_receipt_path) if public_smoke_receipt_path is not None else "",
        },
        "authenticated_customer_surfaces": {
            "status": authenticated_smoke.get("status") or ("not_configured" if authenticated_smoke_receipt_path is None else "missing"),
            "failed_count": authenticated_smoke.get("failed_count"),
            "route_count": authenticated_smoke.get("route_count"),
            "billing_checks_ok": authenticated_billing_ok if authenticated_smoke_receipt_path is not None else None,
            "billing_status_code": authenticated_billing_status_code,
            "missing_billing_checks": missing_authenticated_billing_checks if authenticated_smoke_receipt_path is not None else [],
            "failed_billing_checks": failed_authenticated_billing_checks if authenticated_smoke_receipt_path is not None else [],
            "billing_availability": authenticated_billing_availability,
            "notification_checks_ok": authenticated_notifications_ok if authenticated_smoke_receipt_path is not None else None,
            "missing_notification_checks": missing_authenticated_notification_checks if authenticated_smoke_receipt_path is not None else [],
            "failed_notification_checks": failed_authenticated_notification_checks if authenticated_smoke_receipt_path is not None else [],
            "receipt_path": str(authenticated_smoke_receipt_path) if authenticated_smoke_receipt_path is not None else "",
        },
        "tour_controls": {
            "status": tour_controls.get("status"),
            "provider_counts": tour_controls.get("provider_counts"),
            "provider_blockers": tour_controls.get("provider_blockers"),
            "delivery_contracts": tour_controls.get("delivery_contracts") or {},
            "magicfit_playback": magicfit_playback,
            "magicfit_playback_ok": magicfit_playback_ok,
            "ready_provider_modes": tour_controls.get("ready_provider_modes"),
            "core_required_provider_modes": list(CORE_REQUIRED_TOUR_PROVIDER_MODES),
            "advanced_visual_required_provider_modes": list(
                ADVANCED_VISUAL_REQUIRED_PROVIDER_MODES
            ),
            "core_missing_provider_modes": core_missing_provider_modes,
            "advanced_visual_missing_provider_modes": advanced_visual_missing_provider_modes,
            "operator_missing_provider_modes": sorted(
                set(core_missing_provider_modes)
                | set(advanced_visual_missing_provider_modes)
            ),
            "required_provider_modes": list(REQUIRED_TOUR_PROVIDER_MODES),
            "optional_provider_modes": list(OPTIONAL_TOUR_PROVIDER_MODES),
            "receipt_required_provider_modes": tour_controls.get("required_provider_modes") or [],
            "receipt_optional_provider_modes": tour_controls.get("optional_provider_modes") or [],
            "missing_provider_modes": missing_provider_modes,
            "receipt_path": str(tour_control_receipt_path),
        },
        "browser_rendered_3d": {
            "status": browser_3d_gate.get("status") or ("not_configured" if browser_3d_gate_receipt_path is None else "missing"),
            "failed_count": browser_3d_gate.get("failed_count") if browser_3d_gate_receipt_path is not None else None,
            "providers": browser_3d_gate.get("providers") or [],
            "provider_results": browser_3d_gate.get("provider_results") or [],
            "failed_checks": _failed_receipt_checks(browser_3d_gate) if browser_3d_gate_receipt_path is not None else [],
            "ready": browser_3d_gate_ok if browser_3d_gate_receipt_path is not None else None,
            "receipt_path": str(browser_3d_gate_receipt_path) if browser_3d_gate_receipt_path is not None else "",
            "note": "Hard browser gate: route existence or static labels do not prove 3D readiness.",
        },
        "generated_reconstruction_glb": {
            "status": runtime_reconstruction.get("status") or ("not_configured" if runtime_reconstruction_receipt_path is None else "missing"),
            "ready": runtime_reconstruction_ok if runtime_reconstruction_receipt_path is not None else None,
            "glb_required": runtime_reconstruction.get("glb_required") if runtime_reconstruction_receipt_path is not None else None,
            "glb_non_empty": runtime_reconstruction.get("glb_non_empty") if runtime_reconstruction_receipt_path is not None else None,
            "glb_manifest_ok": runtime_reconstruction.get("glb_manifest_ok") if runtime_reconstruction_receipt_path is not None else None,
            "glb_capability_ok": runtime_reconstruction.get("glb_capability_ok") if runtime_reconstruction_receipt_path is not None else None,
            "required_paths_ok": runtime_reconstruction.get("required_paths_ok") if runtime_reconstruction_receipt_path is not None else None,
            "route_label_quality_ok": runtime_reconstruction.get("route_label_quality_ok") if runtime_reconstruction_receipt_path is not None else None,
            "walkthrough_label_quality_ok": runtime_reconstruction.get("walkthrough_label_quality_ok") if runtime_reconstruction_receipt_path is not None else None,
            "walkthrough_generated_ok": runtime_reconstruction.get("walkthrough_generated_ok") if runtime_reconstruction_receipt_path is not None else None,
            "walkthrough_status": runtime_reconstruction_walkthrough_status if runtime_reconstruction_receipt_path is not None else "",
            "browser_shell_ok": runtime_reconstruction.get("browser_shell_ok") if runtime_reconstruction_receipt_path is not None else None,
            "browser_shell_status": runtime_reconstruction_browser_shell.get("status") if runtime_reconstruction_receipt_path is not None else "",
            "browser_shell_failures": runtime_reconstruction_browser_failures if runtime_reconstruction_receipt_path is not None else [],
            "public_route_contract_ok": runtime_reconstruction.get("public_route_contract_ok") if runtime_reconstruction_receipt_path is not None else None,
            "public_contract_failures": runtime_reconstruction_public_failures if runtime_reconstruction_receipt_path is not None else [],
            "honest_disclosure_ok": runtime_reconstruction.get("honest_disclosure_ok") if runtime_reconstruction_receipt_path is not None else None,
            "viewer_url": str(runtime_reconstruction.get("viewer_url") or ""),
            "glb_size_bytes": runtime_reconstruction_glb.get("size_bytes"),
            "manifest_runtime_commit": release_hygiene_manifest_runtime_commit if release_hygiene_receipt_path is not None else "",
            "head_commit": release_hygiene_head_commit if release_hygiene_receipt_path is not None else "",
            "tracked_dirty_path_count": release_hygiene_tracked_dirty_path_count if release_hygiene_receipt_path is not None else None,
            "receipt_path": str(runtime_reconstruction_receipt_path) if runtime_reconstruction_receipt_path is not None else "",
            "note": "Hard generated-reconstruction gate: first-party browser shell, viewer readiness, human route quality, walkthrough coverage, and public-route safety are required; PropertyQuarry runtime containers use image-baked /app code, so repo-only changes do not count as runtime proof until rebuild/recreate.",
        },
        "service_generated_reconstruction": {
            "status": service_generated_reconstruction.get("status")
            or ("not_configured" if service_generated_reconstruction_receipt_path is None else "missing"),
            "ready": service_generated_reconstruction_ok if service_generated_reconstruction_receipt_path is not None else None,
            "required_paths_ok": service_generated_reconstruction.get("required_paths_ok") if service_generated_reconstruction_receipt_path is not None else None,
            "top_level_video_contract_ok": service_generated_reconstruction.get("top_level_video_contract_ok") if service_generated_reconstruction_receipt_path is not None else None,
            "route_label_quality_ok": service_generated_reconstruction.get("route_label_quality_ok") if service_generated_reconstruction_receipt_path is not None else None,
            "walkthrough_generated_ok": service_generated_reconstruction.get("walkthrough_generated_ok") if service_generated_reconstruction_receipt_path is not None else None,
            "delivery_contract_ok": service_generated_reconstruction.get("delivery_contract_ok") if service_generated_reconstruction_receipt_path is not None else None,
            "public_route_contract_ok": service_generated_reconstruction.get("public_route_contract_ok") if service_generated_reconstruction_receipt_path is not None else None,
            "browser_shell_ok": service_generated_reconstruction.get("browser_shell_ok") if service_generated_reconstruction_receipt_path is not None else None,
            "viewer_url": str(service_generated_reconstruction.get("viewer_url") or ""),
            "receipt_path": str(service_generated_reconstruction_receipt_path) if service_generated_reconstruction_receipt_path is not None else "",
            "note": "Hard service-owned generated-reconstruction gate: the app bundle writer must emit the first-party walkthrough contract, human route labels, delivery metadata, and a browser-proven public-safe launch shell without relying on a stale static bundle.",
        },
        "map_preview_flagship": {
            "status": map_preview_flagship.get("status") or ("not_configured" if map_preview_flagship_receipt_path is None else "missing"),
            "failed_count": map_preview_flagship.get("failed_count") if map_preview_flagship_receipt_path is not None else None,
            "preview_count": map_preview_flagship.get("preview_count") if map_preview_flagship_receipt_path is not None else None,
            "failed_checks": _failed_receipt_checks(map_preview_flagship) if map_preview_flagship_receipt_path is not None else [],
            "preview_results": list(map_preview_flagship.get("preview_results") or [])[:6] if map_preview_flagship_receipt_path is not None else [],
            "ready": map_preview_flagship_ok if map_preview_flagship_receipt_path is not None else None,
            "receipt_path": str(map_preview_flagship_receipt_path) if map_preview_flagship_receipt_path is not None else "",
            "note": "Hard visual-asset gate: image availability does not prove a flagship map thumbnail.",
        },
        "walkthrough_quality": {
            "status": walkthrough_quality.get("status") or ("not_configured" if walkthrough_quality_receipt_path is None else "missing"),
            "failed_count": walkthrough_quality.get("failed_count") if walkthrough_quality_receipt_path is not None else None,
            "video_relpath": str(walkthrough_quality.get("video_relpath") or ""),
            "video_sha256": str(walkthrough_quality.get("video_sha256") or ""),
            "provider_media_binding": walkthrough_quality.get("provider_media_binding") or {},
            "provider_binding": walkthrough_provider_binding_details,
            "failed_checks": _failed_receipt_checks(walkthrough_quality) if walkthrough_quality_receipt_path is not None else [],
            "ready": walkthrough_quality_ok if walkthrough_quality_receipt_path is not None else None,
            "receipt_path": str(walkthrough_quality_receipt_path) if walkthrough_quality_receipt_path is not None else "",
            "note": "Hard walkthrough gate: video existence does not prove room coverage or continuity.",
        },
        "walkthrough_provider_proof": {
            "status": walkthrough_provider_proof.get("status")
            or ("not_configured" if walkthrough_provider_proof_receipt_path is None else "missing"),
            "ready": walkthrough_provider_proof_ok if walkthrough_provider_proof_required else None,
            "required": walkthrough_provider_proof_required,
            "required_providers": ["magicfit", "omagic"],
            "required_orchestrators": ["ea"],
            "verified_providers": list(walkthrough_provider_proof.get("verified_providers") or []),
            "verified_orchestrators": list(walkthrough_provider_proof.get("verified_orchestrators") or []),
            "indexed_participants": list(walkthrough_provider_proof.get("indexed_participants") or []),
            "provenance_index": list(walkthrough_provider_proof.get("provenance_index") or []),
            "missing_providers": list(walkthrough_provider_proof.get("missing_providers") or []),
            "failed_count": walkthrough_provider_proof.get("failed_count")
            if walkthrough_provider_proof_receipt_path is not None
            else None,
            "provider_results": list(walkthrough_provider_proof.get("provider_results") or [])[:4],
            "receipt_path": str(walkthrough_provider_proof_receipt_path)
            if walkthrough_provider_proof_receipt_path is not None
            else "",
            "note": "Hard provider proof: readiness and configured adapters do not prove that MagicFit or OMagic rendered an accepted hosted walkthrough, and EA orchestration is not media authorship.",
        },
        "scene_video_readiness": {
            "status": scene_video_readiness_verifier.get("status") or ("not_configured" if scene_video_readiness_verifier_receipt_path is None else "missing"),
            "ready": (
                scene_video_readiness_verifier_ok
                and scene_video_provider_refresh_packet_verifier_ok
                and scene_video_provider_runtime_ready
                if scene_video_readiness_verifier_receipt_path is not None
                else None
            ),
            "actionability_ready": (
                scene_video_readiness_verifier_ok and scene_video_provider_refresh_packet_verifier_ok
                if scene_video_readiness_verifier_receipt_path is not None
                else None
            ),
            "provider_runtime_ready": scene_video_provider_runtime_ready if scene_video_readiness_receipt_path is not None else None,
            "provider_action_required": bool(
                scene_video_blocked_provider_count or scene_video_next_actions or scene_video_required_provider_gaps
            )
            if scene_video_readiness_receipt_path is not None
            else None,
            "provider_blocked_count": scene_video_blocked_provider_count if scene_video_readiness_receipt_path is not None else None,
            "blocked_providers": scene_video_blocked_providers,
            "required_providers": list(REQUIRED_SCENE_VIDEO_PARITY_PROVIDERS),
            "missing_required_providers": scene_video_required_provider_gaps,
            "runtime_missing_required_providers": scene_video_runtime_missing_required_providers,
            "runtime_not_ready_required_providers": scene_video_runtime_not_ready_required_providers,
            "verifier_missing_required_providers": scene_video_verifier_missing_required_providers,
            "provider_summary": scene_video_provider_summary,
            "telegram_delivery_readiness": scene_video_readiness.get("telegram_delivery_readiness") or {},
            "next_actions": scene_video_next_actions,
            "verifier_blockers": list(scene_video_readiness_verifier.get("blockers") or []) if scene_video_readiness_verifier_receipt_path is not None else [],
            "checked_providers": scene_video_readiness_verifier.get("checked_providers") or [],
            "receipt_path": str(scene_video_readiness_receipt_path) if scene_video_readiness_receipt_path is not None else "",
            "verifier_receipt_path": str(scene_video_readiness_verifier_receipt_path) if scene_video_readiness_verifier_receipt_path is not None else "",
            "runtime_status": {
                "contract_name": str(scene_video_runtime_status.get("contract_name") or "").strip(),
                "source_kind": str(scene_video_runtime_status.get("source_kind") or "").strip(),
                "source_ref": str(scene_video_runtime_status.get("source_ref") or "").strip(),
                "summary": scene_video_runtime_status_summary,
                "providers": scene_video_runtime_status_provider_rows,
                "receipt_path": str(scene_video_runtime_status_receipt_path) if scene_video_runtime_status_receipt_path is not None else "",
            },
            "provider_refresh_packet": {
                "status": (
                    scene_video_provider_refresh_packet_verifier.get("status")
                    or ("not_configured" if scene_video_provider_refresh_packet_verifier_receipt_path is None else "missing")
                ),
                "ready": (
                    scene_video_provider_refresh_packet_verifier_ok
                    if scene_video_provider_refresh_packet_verifier_receipt_path is not None
                    else None
                ),
                "checked_providers": scene_video_provider_refresh_packet_verifier.get("checked_providers") or [],
                "verifier_blockers": list(scene_video_provider_refresh_packet_verifier.get("blockers") or [])
                if scene_video_provider_refresh_packet_verifier_receipt_path is not None
                else [],
                "packet_provider_count": len(list(scene_video_provider_refresh_packet.get("providers") or []))
                if scene_video_provider_refresh_packet_path is not None
                else None,
                "packet_path": str(scene_video_provider_refresh_packet_path) if scene_video_provider_refresh_packet_path is not None else "",
                "verifier_receipt_path": str(scene_video_provider_refresh_packet_verifier_receipt_path)
                if scene_video_provider_refresh_packet_verifier_receipt_path is not None
                else "",
            },
            "note": "Scene-video verifier guards Mootion BrowserAct, Telegram delivery readiness, 1min isolation, and MagicFit/OMagic actionability without embedding secrets; the nested runtime_status view keeps current provider blockers machine-readable.",
        },
        "export_discovery": {
            "status": export_discovery.get("status"),
            "import_count": export_discovery.get("import_count"),
            "rejected_count": export_discovery.get("rejected_count"),
            "repair_count": export_discovery.get("repair_count"),
            "rejected_sample": export_rejection_sample,
            "repair_sample": export_repair_sample,
            "receipt_path": str(export_discovery_receipt_path),
        },
        "operator_import_manifest": {
            "status": import_manifest.get("status") or ("not_configured" if import_manifest_receipt_path is None else "missing"),
            "ready_for_exports": operator_import_manifest_ready,
            "import_count": import_manifest.get("import_count"),
            "drop_status_summary": import_manifest.get("drop_status_summary") or {},
            "providers": sorted(manifest_providers),
            "prepared_drop_provider_count": len(prepared_drop_providers),
            "missing_prepared_providers": sorted(expected_import_providers - prepared_drop_providers),
            "hardened_readmes_ok": hardened_readmes_ok,
            "hardened_readme_provider_count": hardened_readme_provider_count,
            "missing_hardened_readme_providers": missing_hardened_readme_providers,
            "hardened_readme_failures": hardened_readme_failures,
            "next_command": import_manifest.get("next_command"),
            "receipt_path": str(import_manifest_receipt_path) if import_manifest_receipt_path is not None else "",
            "note": "Prepared operator drop lanes are progress only; gold still requires real imported assets and verified playable controls.",
        },
        "billing_handoff": {
            "status": billing_handoff_status,
            "provider_status": billing_provider_status,
            "error": billing_receipt.get("error") or "",
            "configured": bool((billing_receipt.get("billing_handoff") or {}).get("configured")) if isinstance(billing_receipt.get("billing_handoff"), dict) else False,
            "host": str((billing_receipt.get("billing_handoff") or {}).get("host") or "") if isinstance(billing_receipt.get("billing_handoff"), dict) else "",
            "host_resolves": bool((billing_receipt.get("billing_handoff") or {}).get("host_resolves")) if isinstance(billing_receipt.get("billing_handoff"), dict) else False,
            "account_handoff_usable": (billing_receipt.get("billing_handoff") or {}).get("account_handoff_usable") if isinstance(billing_receipt.get("billing_handoff"), dict) else None,
            "direct_account_handoff_usable": billing_readiness.get("direct_account_handoff_usable"),
            "signed_handoff_usable": bool(billing_readiness.get("signed_handoff_usable")),
            "ready_via": str(billing_readiness.get("ready_via") or ""),
            "live_smoke_external_handoff_usable": bool(billing_readiness.get("live_smoke_external_handoff_usable")),
            "live_smoke_no_second_login": bool(billing_readiness.get("live_smoke_no_second_login")),
            "live_smoke_bridge_launch": bool(billing_readiness.get("live_smoke_bridge_launch")),
            "bridge_guided_login_assist": bool(billing_readiness.get("bridge_guided_login_assist")),
            "pricing_placeholder": bool(billing_readiness.get("pricing_placeholder")),
            "provider_disabled": bool(billing_readiness.get("provider_disabled")),
            "strict_live_proof_required": bool(billing_readiness.get("strict_live_proof_required")),
            "account_handoff_error": str((billing_receipt.get("billing_handoff") or {}).get("account_handoff_error") or "") if isinstance(billing_receipt.get("billing_handoff"), dict) else "",
            "required_dns_record": (billing_receipt.get("billing_handoff") or {}).get("required_dns_record") if isinstance(billing_receipt.get("billing_handoff"), dict) and isinstance((billing_receipt.get("billing_handoff") or {}).get("required_dns_record"), dict) else {},
            "next_action": str((billing_receipt.get("billing_handoff") or {}).get("next_action") or "") if isinstance(billing_receipt.get("billing_handoff"), dict) else "",
            "sso_bridge": {
                "ready": (billing_receipt.get("billing_sso_bridge") or {}).get("ready") if isinstance(billing_receipt.get("billing_sso_bridge"), dict) else None,
                "config_ready": (billing_receipt.get("billing_sso_bridge") or {}).get("config_ready") if isinstance(billing_receipt.get("billing_sso_bridge"), dict) else None,
                "exchange_checked": (billing_receipt.get("billing_sso_bridge") or {}).get("exchange_checked") if isinstance(billing_receipt.get("billing_sso_bridge"), dict) else None,
                "exchange_usable": (billing_receipt.get("billing_sso_bridge") or {}).get("exchange_usable") if isinstance(billing_receipt.get("billing_sso_bridge"), dict) else None,
                "error": str((billing_receipt.get("billing_sso_bridge") or {}).get("error") or "") if isinstance(billing_receipt.get("billing_sso_bridge"), dict) else "",
                "next_action": str((billing_receipt.get("billing_sso_bridge") or {}).get("next_action") or "") if isinstance(billing_receipt.get("billing_sso_bridge"), dict) else "",
            },
            "member_login_token": {
                "enabled": (billing_receipt.get("member_login_token_handoff") or {}).get("enabled") if isinstance(billing_receipt.get("member_login_token_handoff"), dict) else None,
                "configured": (billing_receipt.get("member_login_token_handoff") or {}).get("configured") if isinstance(billing_receipt.get("member_login_token_handoff"), dict) else None,
                "ready": (billing_receipt.get("member_login_token_handoff") or {}).get("ready") if isinstance(billing_receipt.get("member_login_token_handoff"), dict) else None,
                "error": str((billing_receipt.get("member_login_token_handoff") or {}).get("error") or "") if isinstance(billing_receipt.get("member_login_token_handoff"), dict) else "",
                "next_action": str((billing_receipt.get("member_login_token_handoff") or {}).get("next_action") or "") if isinstance(billing_receipt.get("member_login_token_handoff"), dict) else "",
                "required_env": list(BILLING_MEMBER_TOKEN_REQUIRED_ENV),
            },
            "ready": bool(billing_readiness.get("ready")),
            "compatibility_ok": billing_ok,
            "receipt_path": str(billing_receipt_path) if billing_receipt_path is not None else "",
        },
        "id_austria": {
            "status": id_austria_details.get("status") if id_austria_receipt_path is not None else "not_checked",
            "required": bool(id_austria_details.get("required")) if id_austria_receipt_path is not None else False,
            "configured": bool(id_austria_details.get("configured")) if id_austria_receipt_path is not None else False,
            "error": str(id_austria_details.get("error") or "") if id_austria_receipt_path is not None else "",
            "missing_env": list(id_austria_details.get("missing_env") or []) if id_austria_receipt_path is not None else [],
            "redirect_uri": str(id_austria_details.get("redirect_uri") or "") if id_austria_receipt_path is not None else "",
            "issuer": str(id_austria_details.get("issuer") or "") if id_austria_receipt_path is not None else "",
            "ready": bool(id_austria_details.get("ready")) if id_austria_receipt_path is not None else True,
            "receipt_path": str(id_austria_receipt_path) if id_austria_receipt_path is not None else "",
        },
        "vendor_tooling": {
            "status": vendor_tooling.get("status") or ("not_configured" if vendor_tooling_receipt_path is None else "missing"),
            "mode": str(vendor_tooling.get("mode") or "") if vendor_tooling_receipt_path is not None else "",
            "host_ready": vendor_tooling.get("host_ready") if vendor_tooling_receipt_path is not None else None,
            "generated_tour_ready": bool(vendor_tooling.get("generated_tour_ready")) if vendor_tooling_receipt_path is not None else None,
            "generated_tour_tools": vendor_tooling.get("generated_tour_tools") or {},
            "runtime_generated_tour_ready": vendor_tooling.get("runtime_generated_tour_ready") if vendor_tooling_receipt_path is not None else None,
            "runtime_generated_tour_tools": vendor_tooling.get("runtime_generated_tour_tools") or {},
            "wine_runtime_ready": bool(vendor_tooling.get("wine_runtime_ready")) if vendor_tooling_receipt_path is not None else None,
            "installer_count": vendor_tooling.get("installer_count") if vendor_tooling_receipt_path is not None else None,
            "installer_counts": vendor_tooling.get("installer_counts") or {},
            "installed_app_count": vendor_tooling.get("installed_app_count") if vendor_tooling_receipt_path is not None else None,
            "installed_app_counts": vendor_tooling.get("installed_app_counts") or {},
            "installed_apps": vendor_tooling.get("installed_apps") or [],
            "verified_export_ready_counts": vendor_tooling.get("verified_export_ready_counts") or {},
            "missing_verified_exports": vendor_tooling.get("missing_verified_exports") or [],
            "magicfit_renderer": magicfit_renderer,
            "omagic_adapter": omagic_adapter,
            "next_actions": vendor_tooling.get("next_actions") or [],
            "receipt_path": str(vendor_tooling_receipt_path) if vendor_tooling_receipt_path is not None else "",
            "note": "Host tooling readiness is tracked separately from verified 3D-tour export evidence and walkthrough render-lane configuration.",
        },
        "tour_provider_ownership": {
            "status": tour_provider_ownership.get("status") or ("not_configured" if tour_provider_ownership_receipt_path is None else "missing"),
            "missing_providers": tour_provider_ownership.get("missing_providers") or [],
            "providers": sorted((tour_provider_ownership.get("providers") or {}).keys()) if isinstance(tour_provider_ownership.get("providers"), dict) else [],
            "ready": tour_provider_ownership_ok,
            "receipt_path": str(tour_provider_ownership_receipt_path) if tour_provider_ownership_receipt_path is not None else "",
            "note": "This does not satisfy 3D-tour gold without verified polished 3D-tour exports or allowlisted hosted controls.",
        },
        "self_healing": {
            "status": repair_canary.get("status"),
            "run_status": repair_canary.get("run_status"),
            "source_repair_status": repair_canary.get("source_repair_status"),
            "receipt_resolution": repair_canary.get("receipt_resolution"),
            "receipt_path": str(repair_canary_receipt_path),
        },
        "provider_matrix": {
            "status": provider_matrix.get("status"),
            "country_scope": provider_matrix.get("country_scope"),
            "targeted_search_matrix_status": provider_matrix.get("targeted_search_matrix_status"),
            "targeted_search_matrix_executed": provider_matrix.get("targeted_search_matrix_executed"),
            "targeted_search_matrix_count": provider_matrix.get("targeted_search_matrix_count"),
            "strict_case_count": provider_matrix_summary.get("strict_case_count"),
            "soft_filter_case_count": provider_matrix_summary.get("soft_filter_case_count"),
            "passed_case_count": provider_matrix_summary.get("passed_case_count"),
            "failed_case_count": provider_matrix_summary.get("failed_case_count"),
            "agent_unlimited_results_ok": provider_matrix_summary.get("agent_unlimited_results_ok"),
            "strict_without_soft_filters_ok": provider_matrix_summary.get("strict_without_soft_filters_ok"),
            "soft_filters_present_ok": provider_matrix_summary.get("soft_filters_present_ok"),
            "dispatch_acceptance_complete": provider_matrix_summary.get("dispatch_acceptance_complete"),
            "status_readback_complete": provider_matrix_summary.get("status_readback_complete"),
            "payload_contracts_ok": provider_matrix_summary.get("payload_contracts_ok"),
            "all_search_ready_provider_modes_passed": provider_matrix_modes_ok,
            "provider_country_scope_ok": provider_matrix_country_scope_ok,
            "target_context_country_scope_ok": provider_matrix_target_context_ok,
            "catalog_scope_ok": provider_matrix_catalog_scope_ok,
            "catalog_scope": provider_matrix_catalog_scope,
            "cross_country_sanitization_ok": cross_country_sanitization_ok,
            "cross_country_sanitization_case_count": cross_country_sanitization_summary.get("case_count"),
            "receipt_path": str(provider_matrix_receipt_path),
        },
        "provider_catalog_smoke": provider_catalog_smoke,
        "whole_project_scope": {
            "status": whole_project_scope.get("status") or ("not_configured" if whole_project_scope_receipt_path is None else "missing"),
            "schema": str(whole_project_scope.get("schema") or "") if whole_project_scope_receipt_path is not None else "",
            "required_overlay_layers": whole_project_scope.get("required_overlay_layers") or [],
            "failure_count": len(list(whole_project_scope.get("failures") or [])) if whole_project_scope_receipt_path is not None else None,
            "failures": list(whole_project_scope.get("failures") or [])[:12] if whole_project_scope_receipt_path is not None else [],
            "receipt_path": str(whole_project_scope_receipt_path) if whole_project_scope_receipt_path is not None else "",
            "note": "Whole-project scope binds the overlay registry, authenticated Teable ingestion producer, atomic Postgres cached read model, real Rybbit delivery producer, Gold receipt consumption, and whole-product boundary language.",
        },
        "production_security_posture": {
            "status": security_posture.get("status") or ("not_configured" if security_posture_receipt_path is None else "missing"),
            "schema": str(security_posture.get("schema") or "") if security_posture_receipt_path is not None else "",
            "required_checks": security_posture.get("required_checks") or [],
            "failure_count": len(list(security_posture.get("failures") or [])) if security_posture_receipt_path is not None else None,
            "failures": list(security_posture.get("failures") or [])[:12] if security_posture_receipt_path is not None else [],
            "receipt_path": str(security_posture_receipt_path) if security_posture_receipt_path is not None else "",
            "note": "Static security gate for isolated runtime/container posture, public-tour privacy, and locked Python dependency posture.",
        },
        "release_hygiene": {
            "status": release_hygiene.get("status") or ("not_configured" if release_hygiene_receipt_path is None else "missing"),
            "schema": str(release_hygiene.get("schema") or "") if release_hygiene_receipt_path is not None else "",
            "required_checks": release_hygiene.get("required_checks") or [],
            "failure_count": len(list(release_hygiene.get("failures") or [])) if release_hygiene_receipt_path is not None else None,
            "failures": list(release_hygiene.get("failures") or [])[:12] if release_hygiene_receipt_path is not None else [],
            "manifest_runtime_commit": str(release_hygiene.get("manifest_runtime_commit") or "") if release_hygiene_receipt_path is not None else "",
            "head_commit": str(release_hygiene.get("head_commit") or "") if release_hygiene_receipt_path is not None else "",
            "parent_commit": str(release_hygiene.get("parent_commit") or "") if release_hygiene_receipt_path is not None else "",
            "receipt_path": str(release_hygiene_receipt_path) if release_hygiene_receipt_path is not None else "",
            "note": "Repository hygiene and release-manifest authority gate.",
        },
        "furniture_style_variants": {
            "status": furniture_style_contract.get("status") or ("not_configured" if furniture_style_contract_receipt_path is None else "missing"),
            "schema": str(furniture_style_contract.get("schema") or "") if furniture_style_contract_receipt_path is not None else "",
            "style_count": furniture_style_contract.get("style_count") if furniture_style_contract_receipt_path is not None else None,
            "style_values": furniture_style_contract.get("style_values") or [],
            "plan_caps": furniture_style_contract.get("plan_caps") or {},
            "helper_plan_caps": furniture_style_contract.get("helper_plan_caps") or {},
            "availability_mode": str(furniture_style_contract.get("availability_mode") or ""),
            "pricing_surface_bound": furniture_style_contract.get("pricing_surface_bound") is True,
            "failure_count": len(list(furniture_style_contract.get("failures") or [])) if furniture_style_contract_receipt_path is not None else None,
            "failures": list(furniture_style_contract.get("failures") or [])[:12] if furniture_style_contract_receipt_path is not None else [],
            "receipt_path": str(furniture_style_contract_receipt_path) if furniture_style_contract_receipt_path is not None else "",
            "note": "Furniture styles are chosen per visual request and remain style-aware across caching; this does not replace verified 3D-tour provider evidence.",
        },
        "bts_methodology": {
            "status": bts_methodology_contract.get("status") or ("not_configured" if bts_methodology_contract_receipt_path is None else "missing"),
            "schema": str(bts_methodology_contract.get("schema") or "") if bts_methodology_contract_receipt_path is not None else "",
            "language_count": bts_methodology_contract.get("language_count") if bts_methodology_contract_receipt_path is not None else None,
            "languages": bts_methodology_contract.get("languages") or [],
            "source_section_count": bts_methodology_contract.get("source_section_count") if bts_methodology_contract_receipt_path is not None else None,
            "failure_count": len(list(bts_methodology_contract.get("failures") or [])) if bts_methodology_contract_receipt_path is not None else None,
            "failures": list(bts_methodology_contract.get("failures") or [])[:12] if bts_methodology_contract_receipt_path is not None else [],
            "receipt_path": str(bts_methodology_contract_receipt_path) if bts_methodology_contract_receipt_path is not None else "",
            "note": "BTS score-PDF methodology gate for data-source provenance and selected-district no-reward scoring policy.",
        },
        "tour_delivery_contract_shape": {
            "status": tour_delivery_contract.get("status") or ("not_configured" if tour_delivery_contract_receipt_path is None else "missing"),
            "schema": str(tour_delivery_contract.get("schema") or "") if tour_delivery_contract_receipt_path is not None else "",
            "required_provider_modes": tour_delivery_contract.get("required_provider_modes") or tour_delivery_contract.get("required_providers") or [],
            "optional_provider_modes": tour_delivery_contract.get("optional_provider_modes") or [],
            "ready_provider_modes": tour_delivery_contract.get("ready_provider_modes") or [],
            "missing_provider_modes": tour_delivery_contract.get("missing_provider_modes") or [],
            "matterport_ready_count": tour_delivery_contract.get("matterport_ready_count") if tour_delivery_contract_receipt_path is not None else None,
            "failure_count": len(list(tour_delivery_contract.get("failures") or [])) if tour_delivery_contract_receipt_path is not None else None,
            "failures": list(tour_delivery_contract.get("failures") or [])[:12] if tour_delivery_contract_receipt_path is not None else [],
            "receipt_path": str(tour_delivery_contract_receipt_path) if tour_delivery_contract_receipt_path is not None else "",
            "note": "Public-safe tour delivery contract shape gate: Core Gold requires topology-verified Matterport; advanced walkthrough providers remain a separately governed claim.",
        },
        "global_market_envelope": {
            **global_market_envelope_details,
            "required": core_launch_governance_required,
            "ready": (
                global_market_envelope_ok
                if global_market_envelope_receipt_path is not None
                or core_launch_governance_required
                else None
            ),
            "max_age_hours": effective_governance_max_age_hours,
            "receipt_path": (
                str(global_market_envelope_receipt_path)
                if global_market_envelope_receipt_path is not None
                else ""
            ),
            "note": "Launch/Core requires exact AT/DE/CR launch classifications, zero market blockers, freshness, and exact Gold release SHA/image binding; standard and flagship only surface optional evidence.",
        },
        "incident_support": {
            **incident_support_details,
            "required": core_launch_governance_required,
            "ready": (
                incident_support_ok
                if incident_support_receipt_path is not None
                or core_launch_governance_required
                else None
            ),
            "max_age_hours": effective_governance_max_age_hours,
            "receipt_path": (
                str(incident_support_receipt_path)
                if incident_support_receipt_path is not None
                else ""
            ),
            "note": "Launch/Core requires a passing independently attested incident/support gate for exact AT/DE/CR scope, fresh live coverage, and exact Gold release SHA/image binding; standard and flagship only surface optional evidence.",
        },
        "global_experience": {
            **global_experience_details,
            "required": core_launch_governance_required,
            "ready": (
                global_experience_ok
                if global_experience_receipt_path is not None
                or core_launch_governance_required
                else None
            ),
            "max_age_hours": effective_governance_max_age_hours,
            "receipt_path": (
                str(global_experience_receipt_path)
                if global_experience_receipt_path is not None
                else ""
            ),
            "note": "Launch/Core requires a passing independently attested global-experience gate for exact AT/de-AT, DE/de-DE, and CR/es-CR scope, fresh native/content, WCAG 2.2 AA, manual accessibility, browser/device, field-CWV, recovery, and localized-SEO evidence, and exact Gold release SHA/image binding; standard and flagship only surface optional health.",
        },
        "jurisdiction_privacy_rights": {
            **jurisdiction_privacy_rights_details,
            "required": core_launch_governance_required,
            "ready": (
                jurisdiction_privacy_rights_ok
                if jurisdiction_privacy_rights_receipt_path is not None
                or core_launch_governance_required
                else None
            ),
            "max_age_hours": effective_governance_max_age_hours,
            "receipt_path": (
                str(jurisdiction_privacy_rights_receipt_path)
                if jurisdiction_privacy_rights_receipt_path is not None
                else ""
            ),
            "note": "Launch/Core requires a passing independently attested jurisdiction/privacy/provider-rights gate for exact AT/DE/CR scope, a fresh live receipt, exact Gold release SHA/image binding, and digests of the current legal contract and global market envelope; standard and flagship only surface optional health.",
        },
        "slo_evidence": {
            **slo_evidence_details,
            "required": slo_evidence_required,
            "receipt_path": str(slo_evidence_receipt_path)
            if slo_evidence_receipt_path is not None
            else "",
            "note": "Launch SLO evidence must be a fresh authenticated private no-store capture bound to the exact release SHA, image digest, and API replica set, with offline rule injection passing.",
        },
        "receipt_freshness": {
            "status": "pass" if receipt_freshness_ok else "fail",
            "max_age_hours": effective_max_receipt_age_hours,
            "stale_receipts": stale_receipts,
        },
        "blockers": normalized_blockers,
        "operator_blockers": normalized_operator_blockers,
        "pass_areas": [row for row in pass_areas if row is not None],
        "next_required_actions": selected_next_required_actions,
        "operator_next_required_actions": operator_next_required_actions,
        "notes": notes,
    }


def _secure_launch_input_bytes(
    path: Path,
    *,
    field: str,
    _test_allow_insecure: bool,
) -> bytes:
    resolved = path.expanduser()
    if not resolved.is_absolute():
        raise ValueError(f"{field} path must be absolute")
    if not _test_allow_insecure:
        evidence_contract.assert_secure_external_parent(resolved, field=field)
    try:
        path_before = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{field} could not be securely inspected") from exc
    if not stat.S_ISREG(path_before.st_mode):
        raise ValueError(f"{field} must be a non-symlink regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(resolved, flags)
    except OSError as exc:
        raise ValueError(f"{field} could not be securely opened") from exc
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_dev != path_before.st_dev
            or before.st_ino != path_before.st_ino
            or (not _test_allow_insecure and before.st_uid != 0)
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size <= 0
            or before.st_size > 128 * 1024 * 1024
        ):
            raise ValueError(f"{field} ownership, mode, or size is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(fd)
        if len(raw) != before.st_size or any(
            getattr(before, name) != getattr(after, name)
            for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        ):
            raise ValueError(f"{field} changed while it was snapshotted")
        try:
            path_after = os.stat(resolved, follow_symlinks=False)
        except OSError as exc:
            raise ValueError(f"{field} changed while it was snapshotted") from exc
        if (
            not stat.S_ISREG(path_after.st_mode)
            or path_after.st_dev != before.st_dev
            or path_after.st_ino != before.st_ino
            or path_after.st_size != before.st_size
            or path_after.st_mtime_ns != before.st_mtime_ns
            or path_after.st_ctime_ns != before.st_ctime_ns
        ):
            raise ValueError(f"{field} changed while it was snapshotted")
        return raw
    finally:
        os.close(fd)


def _write_pinned_input(path: Path, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags, 0o400)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write while pinning launch input")
            view = view[written:]
        os.fsync(fd)
        os.fchmod(fd, 0o400)
    finally:
        os.close(fd)


def _pin_launch_input_set(
    *,
    inputs: dict[str, Path],
    destination: Path,
    _test_allow_insecure: bool,
) -> tuple[dict[str, Path], dict[str, str]]:
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    pinned: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    raw_by_name: dict[str, bytes] = {}
    for name, source in inputs.items():
        raw = _secure_launch_input_bytes(
            source,
            field=f"canonical launch input {name}",
            _test_allow_insecure=_test_allow_insecure,
        )
        target = destination / f"{name}.artifact"
        _write_pinned_input(target, raw)
        pinned[name] = target
        hashes[name] = hashlib.sha256(raw).hexdigest()
        raw_by_name[name] = raw

    companions: set[tuple[Path, str]] = set()
    for bundle_name in ("metrics_snapshot", "metrics_probe"):
        try:
            payload = loads_strict_json_object(
                raw_by_name[bundle_name],
                field=f"canonical launch input {bundle_name}",
                maximum_bytes=128 * 1024 * 1024,
            )
        except StrictJsonError as exc:
            raise ValueError(f"{bundle_name} is not strict JSON") from exc
        rows = payload.get("replicas") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError(f"{bundle_name} replica inventory is invalid")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"{bundle_name} replica reference is invalid")
            references = (
                (row.get("start"), row.get("end"))
                if bundle_name == "metrics_snapshot"
                else (row,)
            )
            for reference in references:
                if not isinstance(reference, dict):
                    raise ValueError(f"{bundle_name} companion reference is invalid")
                relative = str(reference.get("path") or "")
                if not relative or Path(relative).name != relative:
                    raise ValueError(f"{bundle_name} companion path is unsafe")
                companions.add((inputs[bundle_name].parent / relative, relative))
    pinned_companion_hashes: dict[str, str] = {}
    for source, relative in companions:
        raw = _secure_launch_input_bytes(
            source,
            field=f"canonical launch companion {relative}",
            _test_allow_insecure=_test_allow_insecure,
        )
        target = destination / relative
        companion_hash = hashlib.sha256(raw).hexdigest()
        if relative in pinned_companion_hashes:
            if pinned_companion_hashes[relative] != companion_hash:
                raise ValueError("canonical launch companion name collision")
            continue
        _write_pinned_input(target, raw)
        pinned_companion_hashes[relative] = companion_hash
    return pinned, hashes


def _run_canonical_launch_validators(
    *,
    release_commit_sha: str,
    release_image_digest: str,
    metrics_snapshot_path: Path,
    metrics_probe_path: Path,
    monitoring_receipt_path: Path,
    prometheus_range_receipt_path: Path,
    prometheus_range_response_path: Path,
    alert_delivery_receipt_path: Path,
    dashboard_render_receipt_path: Path,
    structured_log_query_receipt_path: Path,
    distributed_trace_query_receipt_path: Path,
    output_directory: Path,
    slo_path: Path = DEFAULT_SLO_PATH,
    rules_path: Path = DEFAULT_RULES_PATH,
    rule_tests_path: Path = DEFAULT_RULE_TESTS_PATH,
    prometheus_config_path: Path = DEFAULT_PROMETHEUS_CONFIG_PATH,
    alertmanager_config_path: Path = DEFAULT_ALERTMANAGER_CONFIG_PATH,
    slo_runner: Any | None = None,
    now: datetime | None = None,
    _test_allow_insecure_inputs: bool = False,
) -> tuple[dict[str, Any], dict[str, Any], Path, Path, list[str]]:
    """Run canonical validators from raw artifacts for a gold launch claim."""

    selected_policy_paths = {
        slo_path: DEFAULT_SLO_PATH,
        rules_path: DEFAULT_RULES_PATH,
        rule_tests_path: DEFAULT_RULE_TESTS_PATH,
        prometheus_config_path: DEFAULT_PROMETHEUS_CONFIG_PATH,
        alertmanager_config_path: DEFAULT_ALERTMANAGER_CONFIG_PATH,
    }
    if any(selected.resolve() != canonical.resolve() for selected, canonical in selected_policy_paths.items()):
        raise ValueError("canonical launch policy path override is forbidden")

    output_directory.mkdir(parents=False, exist_ok=False, mode=0o700)
    os.chmod(output_directory, 0o700)
    output_stat = os.stat(output_directory, follow_symlinks=False)
    if (
        not stat.S_ISDIR(output_stat.st_mode)
        or output_stat.st_uid != os.geteuid()
        or stat.S_IMODE(output_stat.st_mode) != 0o700
    ):
        raise ValueError("canonical launch output directory is unsafe")
    pinned_inputs, shared_input_hashes = _pin_launch_input_set(
        inputs={
            "metrics_snapshot": metrics_snapshot_path,
            "metrics_probe": metrics_probe_path,
            "monitoring_receipt": monitoring_receipt_path,
            "prometheus_range_receipt": prometheus_range_receipt_path,
            "prometheus_range_response": prometheus_range_response_path,
            "alert_delivery_receipt": alert_delivery_receipt_path,
            "dashboard_render_receipt": dashboard_render_receipt_path,
            "structured_log_query_receipt": structured_log_query_receipt_path,
            "distributed_trace_query_receipt": distributed_trace_query_receipt_path,
        },
        destination=output_directory / "pinned-inputs",
        _test_allow_insecure=_test_allow_insecure_inputs,
    )
    metrics_snapshot_path = pinned_inputs["metrics_snapshot"]
    metrics_probe_path = pinned_inputs["metrics_probe"]
    monitoring_receipt_path = pinned_inputs["monitoring_receipt"]
    prometheus_range_receipt_path = pinned_inputs["prometheus_range_receipt"]
    prometheus_range_response_path = pinned_inputs["prometheus_range_response"]
    alert_delivery_receipt_path = pinned_inputs["alert_delivery_receipt"]
    dashboard_render_receipt_path = pinned_inputs["dashboard_render_receipt"]
    structured_log_query_receipt_path = pinned_inputs["structured_log_query_receipt"]
    distributed_trace_query_receipt_path = pinned_inputs[
        "distributed_trace_query_receipt"
    ]
    slo_input_names = (
        "metrics_snapshot",
        "metrics_probe",
        "monitoring_receipt",
        "prometheus_range_receipt",
        "prometheus_range_response",
        "alert_delivery_receipt",
    )
    slo_shared_input_hashes = {
        name: shared_input_hashes[name] for name in slo_input_names
    }
    slo_shared_input_paths = {
        name: pinned_inputs[name] for name in slo_input_names
    }
    slo_output = output_directory / "slo-revalidated.json"
    observability_output = output_directory / "observability-revalidated.json"
    errors: list[str] = []
    slo_receipt: dict[str, Any] = {}
    observability_receipt: dict[str, Any] = {}
    try:
        slo_arguments: dict[str, Any] = {
            "config": EvidenceConfig(
                release_commit_sha=release_commit_sha,
                release_image_digest=release_image_digest,
                metrics_snapshot_path=metrics_snapshot_path,
                metrics_probe_path=metrics_probe_path,
                prometheus_range_path=prometheus_range_response_path,
                prometheus_range_receipt_path=prometheus_range_receipt_path,
                slo_path=slo_path,
                rules_path=rules_path,
                rule_tests_path=rule_tests_path,
                prometheus_config_path=prometheus_config_path,
                alertmanager_config_path=alertmanager_config_path,
                receipt_path=slo_output,
                flagship=True,
                overwrite_receipt=True,
                shared_input_hashes=slo_shared_input_hashes,
                shared_input_paths=slo_shared_input_paths,
            )
        }
        if slo_runner is not None:
            slo_arguments["runner"] = slo_runner
        if now is not None:
            slo_arguments["now"] = now
        slo_receipt_raw, slo_exit_code = run_evidence_gate(**slo_arguments)
        slo_receipt = dict(slo_receipt_raw)
        if slo_exit_code != 0:
            error = slo_receipt.get("error")
            message = str(error.get("message") or "canonical SLO validator failed") if isinstance(error, dict) else "canonical SLO validator failed"
            errors.append(message)
    except Exception as exc:
        errors.append(f"canonical SLO validator could not complete: {type(exc).__name__}")

    try:
        observability_arguments: dict[str, Any] = {
            "release_commit_sha": release_commit_sha,
            "release_image_digest": release_image_digest,
            "monitoring_receipt_path": monitoring_receipt_path,
            "metrics_snapshot_path": metrics_snapshot_path,
            "metrics_probe_path": metrics_probe_path,
            "prometheus_range_receipt_path": prometheus_range_receipt_path,
            "prometheus_range_response_path": prometheus_range_response_path,
            "alert_delivery_receipt_path": alert_delivery_receipt_path,
            "dashboard_render_receipt_path": dashboard_render_receipt_path,
            "structured_log_query_receipt_path": structured_log_query_receipt_path,
            "distributed_trace_query_receipt_path": distributed_trace_query_receipt_path,
            "expected_input_hashes": shared_input_hashes,
        }
        if now is not None:
            observability_arguments["now"] = now
        observability_receipt = dict(
            verify_receipt_bundle(
                **observability_arguments,
            )
        )
        write_observability_verification(
            observability_output,
            observability_receipt,
            overwrite=True,
        )
    except ObservabilityReceiptValidationError as exc:
        errors.append(str(exc))
    except Exception as exc:
        errors.append(f"canonical observability validator could not complete: {type(exc).__name__}")

    if slo_receipt.get("shared_input_hashes") != slo_shared_input_hashes:
        errors.append("canonical SLO validator shared input hash set differs")
    if observability_receipt.get("shared_input_hashes") != shared_input_hashes:
        errors.append("canonical observability validator shared input hash set differs")
    try:
        canonical_policy_hashes = evidence_contract.canonical_policy_hashes()
    except evidence_contract.EvidenceContractError:
        errors.append("canonical launch policy hashes could not be recomputed")
    else:
        slo_policy_hashes = (
            dict(slo_receipt.get("authenticated_evidence") or {}).get(
                "policy_hashes"
            )
            if isinstance(slo_receipt.get("authenticated_evidence"), dict)
            else None
        )
        if slo_policy_hashes != canonical_policy_hashes:
            errors.append("canonical SLO validator policy hash set differs")
        if observability_receipt.get("policy_hashes") != canonical_policy_hashes:
            errors.append("canonical observability validator policy hash set differs")
    try:
        canonical_monitoring = evidence_contract.load_canonical_monitoring_identity()
    except evidence_contract.EvidenceContractError:
        errors.append("canonical monitoring identity could not be recomputed")
    else:
        canonical_identity = canonical_monitoring.get("identity")
        canonical_tools = canonical_monitoring.get("monitoring_tools")
        if slo_receipt.get("canonical_monitoring_identity") != canonical_identity:
            errors.append("canonical SLO monitoring identity differs")
        if observability_receipt.get("canonical_monitoring_identity") != canonical_identity:
            errors.append("canonical observability monitoring identity differs")
        if slo_receipt.get("monitoring_tools") != canonical_tools:
            errors.append("canonical SLO pinned tool identities differ")
        if observability_receipt.get("monitoring_tools") != canonical_tools:
            errors.append("canonical observability pinned tool identities differ")
    for name, pinned_path in pinned_inputs.items():
        try:
            pinned_hash = hashlib.sha256(
                _secure_launch_input_bytes(
                    pinned_path,
                    field=f"pinned canonical launch input {name}",
                    _test_allow_insecure=_test_allow_insecure_inputs,
                )
            ).hexdigest()
        except ValueError:
            errors.append(f"pinned canonical launch input changed: {name}")
            continue
        if pinned_hash != shared_input_hashes[name]:
            errors.append(f"pinned canonical launch input changed: {name}")

    return slo_receipt, observability_receipt, slo_output, observability_output, errors


def _apply_canonical_launch_evidence(
    receipt: dict[str, Any],
    *,
    slo_receipt: dict[str, Any],
    observability_receipt: dict[str, Any],
    slo_receipt_path: Path,
    observability_receipt_path: Path,
    validation_errors: list[str],
) -> None:
    """Attach validator results and fail gold closed unless every proof is canonical."""

    effective_errors = list(validation_errors)
    try:
        canonical_monitoring = evidence_contract.load_canonical_monitoring_identity()
    except evidence_contract.EvidenceContractError:
        effective_errors.append("Gold could not recompute canonical monitoring identity")
    else:
        if (
            slo_receipt.get("canonical_monitoring_identity")
            != canonical_monitoring.get("identity")
            or observability_receipt.get("canonical_monitoring_identity")
            != canonical_monitoring.get("identity")
            or slo_receipt.get("monitoring_tools")
            != canonical_monitoring.get("monitoring_tools")
            or observability_receipt.get("monitoring_tools")
            != canonical_monitoring.get("monitoring_tools")
        ):
            effective_errors.append(
                "Gold canonical monitoring or pinned tool identity differs"
            )
    slo_ok = slo_receipt.get("status") == "pass" and slo_receipt.get("gate_passed") is True
    operations_evidence_raw = observability_receipt.get("operations_evidence")
    operations_evidence = (
        operations_evidence_raw
        if isinstance(operations_evidence_raw, dict)
        else {}
    )
    operations_shared_input_hashes = operations_evidence.get(
        "shared_input_hashes"
    )
    operations_receipts = operations_evidence.get("receipts")
    operations_replica_ids = operations_evidence.get("replica_ids")
    observability_replica_ids = observability_receipt.get("replica_ids")
    operations_replicas_ok = (
        isinstance(operations_replica_ids, list)
        and bool(operations_replica_ids)
        and operations_replica_ids == observability_replica_ids
        and all(
            isinstance(replica_id, str)
            and REPLICA_ID_RE.fullmatch(replica_id) is not None
            and replica_id != "UNCONFIGURED"
            for replica_id in operations_replica_ids
        )
        and operations_replica_ids == sorted(set(operations_replica_ids))
    )
    observability_shared_input_hashes = observability_receipt.get(
        "shared_input_hashes"
    )
    if not isinstance(observability_shared_input_hashes, dict):
        observability_shared_input_hashes = {}
    observability_policy_hashes = observability_receipt.get("policy_hashes")
    if not isinstance(observability_policy_hashes, dict):
        observability_policy_hashes = {}
    operations_hashes_ok = (
        isinstance(operations_shared_input_hashes, dict)
        and set(operations_shared_input_hashes) == set(OPERATIONS_SHARED_INPUT_NAMES)
        and all(
            isinstance(value, str)
            and re.fullmatch(r"[0-9a-f]{64}", value) is not None
            for value in operations_shared_input_hashes.values()
        )
    )
    operations_receipt_names = {
        "dashboard_render_receipt": "dashboard_render",
        "structured_log_query_receipt": "structured_log_query",
        "distributed_trace_query_receipt": "distributed_trace_query",
    }
    operations_receipts_ok = (
        operations_hashes_ok
        and isinstance(operations_receipts, dict)
        and all(
            isinstance(operations_receipts.get(receipt_name), dict)
            and operations_receipts[receipt_name].get("file_sha256")
            == operations_shared_input_hashes[input_name]
            for input_name, receipt_name in operations_receipt_names.items()
        )
    )
    operations_ok = (
        operations_evidence.get("schema_version")
        == OPERATIONS_VERIFICATION_SCHEMA
        and operations_evidence.get("producer")
        == OPERATIONS_VERIFICATION_PRODUCER
        and operations_evidence.get("status") == "verified"
        and operations_evidence.get("source_contract_status")
        == "defined_not_live_evidence"
        and operations_evidence.get("cross_receipt_links_verified") is True
        and operations_replicas_ok
        and operations_evidence.get("release")
        == observability_receipt.get("release")
        and operations_evidence.get("deployment_id")
        == observability_receipt.get("deployment_id")
        and operations_evidence.get("challenge_sha256")
        == observability_receipt.get("challenge_sha256")
        and operations_evidence.get("policy_sha256")
        == observability_policy_hashes.get(
            "flagship_operations_sha256"
        )
        and operations_hashes_ok
        and all(
            observability_shared_input_hashes.get(name)
            == operations_shared_input_hashes[name]
            for name in OPERATIONS_SHARED_INPUT_NAMES
        )
        and operations_receipts_ok
    )
    observability_ok = (
        observability_receipt.get("schema_version")
        == "propertyquarry.observability-receipt-verification.v2"
        and observability_receipt.get("producer")
        == "propertyquarry-observability-receipt-verifier"
        and observability_receipt.get("status") == "verified"
        and observability_receipt.get("cross_receipt_links_verified") is True
        and operations_ok
    )
    launch_ok = bool(slo_ok and observability_ok and not effective_errors)
    receipt["canonical_launch_evidence"] = {
        "required": True,
        "status": "pass" if launch_ok else "blocked",
        "validators_invoked": [
            "scripts.propertyquarry_slo_evidence.run_evidence_gate",
            "scripts.propertyquarry_observability_receipts.verify_receipt_bundle",
            "scripts.propertyquarry_flagship_operations_evidence.verify_operations_evidence",
        ],
        "validation_errors": effective_errors,
        "slo": {
            "status": slo_receipt.get("status") or "missing",
            "receipt_path": str(slo_receipt_path),
            "inputs": slo_receipt.get("inputs") or {},
            "probe": slo_receipt.get("probe") or {},
            "metrics": slo_receipt.get("metrics") or {},
            "rules": slo_receipt.get("rules") or {},
            "monitoring_config": slo_receipt.get("monitoring_config") or {},
            "promtool": slo_receipt.get("promtool") or {},
            "amtool": slo_receipt.get("amtool") or {},
            "authenticated_evidence": slo_receipt.get("authenticated_evidence") or {},
            "canonical_monitoring_identity": slo_receipt.get(
                "canonical_monitoring_identity"
            )
            or {},
            "monitoring_tools": slo_receipt.get("monitoring_tools") or {},
        },
        "observability": {
            "status": observability_receipt.get("status") or "missing",
            "receipt_path": str(observability_receipt_path),
            "release": observability_receipt.get("release") or {},
            "deployment_id": observability_receipt.get("deployment_id") or "",
            "challenge_sha256": observability_receipt.get("challenge_sha256")
            or "",
            "replica_ids": (
                observability_replica_ids
                if isinstance(observability_replica_ids, list)
                else []
            ),
            "receipts": observability_receipt.get("receipts") or {},
            "cross_receipt_links_verified": observability_receipt.get("cross_receipt_links_verified"),
            "payload_sha256": observability_receipt.get("payload_sha256") or "",
            "policy_hashes": observability_receipt.get("policy_hashes") or {},
            "shared_input_hashes": observability_shared_input_hashes,
            "canonical_monitoring_identity": observability_receipt.get(
                "canonical_monitoring_identity"
            )
            or {},
            "monitoring_tools": observability_receipt.get("monitoring_tools") or {},
            "operations_evidence": operations_evidence,
        },
        "note": "Gold invoked canonical validators from raw artifacts; stored producer pass booleans were not used as launch authority.",
    }
    pass_areas = receipt.setdefault("pass_areas", [])
    if launch_ok:
        pass_areas.append(
            {
                "area": "canonical_launch_evidence",
                "status": "pass",
                "slo_receipt_path": str(slo_receipt_path),
                "observability_receipt_path": str(observability_receipt_path),
            }
        )
        return

    receipt["status"] = "blocked"
    receipt["ready_for_notification"] = False
    blockers = receipt.setdefault("blockers", [])
    blockers.append(
        {
            "area": "canonical_launch_evidence",
            "key": "canonical_launch_evidence",
            "status": "blocked",
            "slo_validated": slo_ok,
            "observability_validated": observability_ok,
            "validation_errors": effective_errors,
            "operations_evidence_validated": operations_ok,
            "action": "supply fresh raw metrics, monitoring-runtime, Prometheus 30-day range response/receipt, alert-delivery, dashboard-render, structured-log-query, and distributed-trace-query artifacts for this exact release, then rerun gold",
        }
    )
    receipt.setdefault("next_required_actions", []).append(
        "Re-run canonical SLO, observability, and flagship operations validation from fresh raw release-bound artifacts."
    )
    receipt.setdefault("notes", []).append(
        "Gold launch authority is withheld because canonical raw-artifact validation did not pass."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize current PropertyQuarry gold-readiness receipts.")
    parser.add_argument("--performance-receipt", default="")
    parser.add_argument("--continuous-ux-receipt", default="")
    parser.add_argument("--live-mobile-receipt", default="")
    parser.add_argument("--accessibility-receipt", default="")
    parser.add_argument("--failure-state-receipt", default="")
    parser.add_argument("--activation-to-value-receipt", default="")
    parser.add_argument("--public-smoke-receipt", default="")
    parser.add_argument("--authenticated-smoke-receipt", default="")
    parser.add_argument("--tour-control-receipt", default="")
    parser.add_argument("--export-discovery-receipt", default="")
    parser.add_argument("--import-manifest-receipt", default="")
    parser.add_argument("--billing-receipt", default="")
    parser.add_argument("--tour-provider-ownership-receipt", default="")
    parser.add_argument("--vendor-tooling-receipt", default="")
    parser.add_argument("--whole-project-scope-receipt", default="")
    parser.add_argument("--security-posture-receipt", default="")
    parser.add_argument("--release-hygiene-receipt", default="")
    parser.add_argument("--furniture-style-contract-receipt", default="")
    parser.add_argument("--bts-methodology-contract-receipt", default="")
    parser.add_argument("--tour-delivery-contract-receipt", default="")
    parser.add_argument("--map-preview-flagship-receipt", default="")
    parser.add_argument("--browser-3d-gate-receipt", default="")
    parser.add_argument("--runtime-reconstruction-receipt", default="")
    parser.add_argument("--service-generated-reconstruction-receipt", default="")
    parser.add_argument("--walkthrough-quality-receipt", default="")
    parser.add_argument("--walkthrough-provider-proof-receipt", default="")
    parser.add_argument("--scene-video-readiness-receipt", default="")
    parser.add_argument("--scene-video-readiness-verifier-receipt", default="")
    parser.add_argument("--scene-video-runtime-status-receipt", default="")
    parser.add_argument("--scene-video-provider-refresh-packet", default="")
    parser.add_argument("--scene-video-provider-refresh-packet-verifier-receipt", default="")
    parser.add_argument("--advanced-visual-binding-receipt", default="")
    parser.add_argument("--slo-evidence-receipt", default="")
    parser.add_argument("--evidence-overlay-receipt", default="")
    parser.add_argument("--rybbit-evidence-receipt", default="")
    parser.add_argument(
        "--global-market-envelope-receipt",
        default="",
        help="Versioned AT/DE/CR global-market-envelope receipt; mandatory and exact-release-bound for launch/Core.",
    )
    parser.add_argument(
        "--incident-support-receipt",
        default="",
        help="Incident/support gate receipt; mandatory and exact-release-bound for launch/Core.",
    )
    parser.add_argument(
        "--global-experience-receipt",
        default="",
        help="Global-experience gate receipt; mandatory, fresh, independently attested, and exact-release-bound for launch/Core.",
    )
    parser.add_argument(
        "--jurisdiction-privacy-rights-receipt",
        default="",
        help="Jurisdiction/privacy/provider-rights gate receipt; mandatory, current-contract-bound, and exact-release-bound for launch/Core.",
    )
    parser.add_argument("--slo-metrics-snapshot", default="")
    parser.add_argument("--slo-metrics-probe", default="")
    parser.add_argument("--monitoring-runtime-receipt", default="")
    parser.add_argument("--prometheus-range-receipt", default="")
    parser.add_argument("--prometheus-range-response", default="")
    parser.add_argument("--alert-delivery-receipt", default="")
    parser.add_argument("--dashboard-render-receipt", default="")
    parser.add_argument("--structured-log-query-receipt", default="")
    parser.add_argument("--distributed-trace-query-receipt", default="")
    parser.add_argument(
        "--require-launch-evidence",
        action="store_true",
        help="Invoke all canonical validators from every raw launch artifact and fail gold closed.",
    )
    parser.add_argument(
        "--launch-evidence-dir",
        default="_completion/property_gold_status/launch_evidence",
    )
    parser.add_argument("--slo-definition", default=str(DEFAULT_SLO_PATH))
    parser.add_argument("--alert-rules", default=str(DEFAULT_RULES_PATH))
    parser.add_argument("--alert-rule-tests", default=str(DEFAULT_RULE_TESTS_PATH))
    parser.add_argument("--prometheus-config", default=str(DEFAULT_PROMETHEUS_CONFIG_PATH))
    parser.add_argument("--alertmanager-config", default=str(DEFAULT_ALERTMANAGER_CONFIG_PATH))
    parser.add_argument(
        "--expected-release-sha",
        default=os.environ.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA", ""),
    )
    parser.add_argument(
        "--expected-image-digest",
        default=os.environ.get("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST", ""),
    )
    parser.add_argument(
        "--expected-release-deployment-id",
        default=os.environ.get("PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID", ""),
        help="Exact deployment identity expected from the live /version surface.",
    )
    parser.add_argument(
        "--expected-release-manifest-sha256",
        default=os.environ.get(
            "PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256", ""
        ),
        help="Independent controller-provided lowercase SHA-256 of the canonical runtime manifest.",
    )
    parser.add_argument(
        "--expected-performance-chromium-executable-path",
        default=os.environ.get(
            "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_PATH", ""
        ),
        help="Controller-bound absolute Chromium executable path used by the performance lane.",
    )
    parser.add_argument(
        "--expected-performance-chromium-executable-sha256",
        default=os.environ.get(
            "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_SHA256", ""
        ),
        help="Controller-bound unprefixed SHA-256 of that Chromium executable.",
    )
    parser.add_argument(
        "--expected-public-origin",
        default=(
            os.environ.get("PROPERTYQUARRY_PUBLIC_ORIGIN", "")
            or os.environ.get("PROPERTYQUARRY_EXPECTED_RELEASE_PUBLIC_ORIGIN", "")
        ),
    )
    parser.add_argument(
        "--expected-rybbit-origin",
        default=os.environ.get("PROPERTYQUARRY_RYBBIT_ORIGIN", ""),
    )
    parser.add_argument(
        "--expected-teable-origin",
        default=os.environ.get("PROPERTYQUARRY_EXPECTED_TEABLE_ORIGIN", ""),
    )
    parser.add_argument(
        "--expected-teable-base-id-sha256",
        default=os.environ.get("PROPERTYQUARRY_EXPECTED_TEABLE_BASE_ID_SHA256", ""),
    )
    parser.add_argument(
        "--expected-evidence-overlay-phase",
        choices=("staged", "active"),
        default="staged",
    )
    parser.add_argument(
        "--expected-rybbit-site-id-sha256",
        default=os.environ.get("PROPERTYQUARRY_RYBBIT_SITE_ID_SHA256", ""),
    )
    parser.add_argument(
        "--slo-evidence-max-age-seconds",
        type=int,
        default=DEFAULT_SLO_EVIDENCE_MAX_AGE_SECONDS,
        help="Maximum private metrics probe age; values above 900 remain capped at 900.",
    )
    parser.add_argument(
        "--evidence-overlay-max-age-hours",
        type=float,
        default=DEFAULT_EVIDENCE_OVERLAY_MAX_AGE_HOURS,
        help="Maximum Teable/Postgres proof age; values above 48 remain capped at 48.",
    )
    parser.add_argument(
        "--rybbit-evidence-max-age-minutes",
        type=float,
        default=DEFAULT_RYBBIT_EVIDENCE_MAX_AGE_MINUTES,
        help="Maximum real Rybbit delivery proof age; values above 15 remain capped at 15.",
    )
    parser.add_argument("--id-austria-receipt", default="")
    parser.add_argument("--repair-canary-receipt", default="")
    parser.add_argument("--provider-catalog-receipt", default="")
    parser.add_argument("--provider-matrix-receipt", default="")
    parser.add_argument("--write", default="_completion/property_gold_status/latest.json")
    parser.add_argument("--max-receipt-age-hours", type=float, default=24.0)
    parser.add_argument(
        "--profile",
        choices=(
            "standard",
            "core_gold",
            "advanced_visual_gold",
            "flagship",
            "launch",
        ),
        default="standard",
        help="Select the evidence tier. core_gold and advanced_visual_gold are strict launch-tier compatibility aliases; standard preserves operator-summary semantics.",
    )
    parser.add_argument(
        "--claim-scope",
        choices=("core", "advanced_visual"),
        default=os.environ.get("PROPERTYQUARRY_GOLD_SCOPE") or None,
        help="Select the Core claim or additive Advanced Visual claim independently of the evidence tier.",
    )
    parser.add_argument(
        "--required-browser-engines",
        default=os.environ.get(
            "PROPERTYQUARRY_FLAGSHIP_REQUIRED_BROWSER_ENGINES",
            ",".join(DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES),
        ),
        help="Comma-separated browser engines that flagship gold must prove (default: chromium,firefox,webkit).",
    )
    parser.add_argument("--fail-on-blocked", action="store_true")
    args = parser.parse_args()
    try:
        cli_evidence_tier, cli_claim_scope, _ = _resolve_readiness_request(
            args.profile,
            args.claim_scope,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.require_launch_evidence and cli_evidence_tier != "launch":
        parser.error("--require-launch-evidence requires --profile launch")
    for option, value in (
        ("--max-receipt-age-hours", args.max_receipt_age_hours),
        ("--evidence-overlay-max-age-hours", args.evidence_overlay_max_age_hours),
        ("--rybbit-evidence-max-age-minutes", args.rybbit_evidence_max_age_minutes),
    ):
        if not math.isfinite(value) or value <= 0:
            parser.error(f"{option} must be a finite positive number")
    try:
        configured_browser_engines = _normalize_required_browser_engines(
            tuple(engine.strip() for engine in str(args.required_browser_engines or "").split(",") if engine.strip())
        )
    except ValueError as exc:
        parser.error(str(exc))

    if cli_evidence_tier == "launch":
        launch_product_arguments = {
            "--evidence-overlay-receipt": args.evidence_overlay_receipt,
            "--rybbit-evidence-receipt": args.rybbit_evidence_receipt,
            "--expected-public-origin": args.expected_public_origin,
            "--expected-teable-origin": args.expected_teable_origin,
            "--expected-teable-base-id-sha256": args.expected_teable_base_id_sha256,
            "--expected-rybbit-origin": args.expected_rybbit_origin,
            "--expected-rybbit-site-id-sha256": args.expected_rybbit_site_id_sha256,
        }
        missing_launch_product_arguments = [
            name for name, value in launch_product_arguments.items() if not str(value or "").strip()
        ]
        if missing_launch_product_arguments:
            parser.error(
                "launch product-data evidence requires "
                + ", ".join(missing_launch_product_arguments)
            )

    raw_launch_arguments = {
        "--slo-metrics-snapshot": args.slo_metrics_snapshot,
        "--slo-metrics-probe": args.slo_metrics_probe,
        "--monitoring-runtime-receipt": args.monitoring_runtime_receipt,
        "--prometheus-range-receipt": args.prometheus_range_receipt,
        "--prometheus-range-response": args.prometheus_range_response,
        "--alert-delivery-receipt": args.alert_delivery_receipt,
        "--dashboard-render-receipt": args.dashboard_render_receipt,
        "--structured-log-query-receipt": args.structured_log_query_receipt,
        "--distributed-trace-query-receipt": args.distributed_trace_query_receipt,
    }
    launch_evidence_requested = (
        args.require_launch_evidence
        or cli_evidence_tier in {"flagship", "launch"}
        or any(raw_launch_arguments.values())
    )
    launch_slo_receipt: dict[str, Any] = {}
    launch_observability_receipt: dict[str, Any] = {}
    launch_slo_receipt_path = Path(args.slo_evidence_receipt) if args.slo_evidence_receipt else Path()
    launch_observability_receipt_path = Path()
    launch_validation_errors: list[str] = []
    if launch_evidence_requested:
        missing_launch_arguments = [name for name, value in raw_launch_arguments.items() if not value]
        if missing_launch_arguments:
            parser.error(
                "canonical launch evidence requires all raw inputs; missing "
                + ", ".join(missing_launch_arguments)
            )
        if (
            not args.expected_release_sha
            or not args.expected_image_digest
            or not args.expected_release_deployment_id
            or not args.expected_release_manifest_sha256
            or not args.expected_performance_chromium_executable_path
            or not args.expected_performance_chromium_executable_sha256
        ):
            parser.error(
                "canonical launch evidence requires --expected-release-sha, "
                "--expected-image-digest, --expected-release-deployment-id, "
                "--expected-release-manifest-sha256, and "
                "controller-bound Chromium executable path/SHA-256"
            )
        (
            launch_slo_receipt,
            launch_observability_receipt,
            launch_slo_receipt_path,
            launch_observability_receipt_path,
            launch_validation_errors,
        ) = _run_canonical_launch_validators(
            release_commit_sha=args.expected_release_sha,
            release_image_digest=args.expected_image_digest,
            metrics_snapshot_path=Path(args.slo_metrics_snapshot),
            metrics_probe_path=Path(args.slo_metrics_probe),
            monitoring_receipt_path=Path(args.monitoring_runtime_receipt),
            prometheus_range_receipt_path=Path(args.prometheus_range_receipt),
            prometheus_range_response_path=Path(args.prometheus_range_response),
            alert_delivery_receipt_path=Path(args.alert_delivery_receipt),
            dashboard_render_receipt_path=Path(args.dashboard_render_receipt),
            structured_log_query_receipt_path=Path(
                args.structured_log_query_receipt
            ),
            distributed_trace_query_receipt_path=Path(
                args.distributed_trace_query_receipt
            ),
            output_directory=Path(args.launch_evidence_dir),
            slo_path=Path(args.slo_definition),
            rules_path=Path(args.alert_rules),
            rule_tests_path=Path(args.alert_rule_tests),
            prometheus_config_path=Path(args.prometheus_config),
            alertmanager_config_path=Path(args.alertmanager_config),
        )
        args.slo_evidence_receipt = str(launch_slo_receipt_path)

    receipt = build_gold_status_receipt(
        performance_receipt_path=Path(args.performance_receipt) if args.performance_receipt else _default_receipt_path("performance"),
        continuous_ux_receipt_path=(
            Path(args.continuous_ux_receipt)
            if args.continuous_ux_receipt
            else _default_receipt_path_if_exists("continuous_ux")
        ),
        live_mobile_receipt_path=Path(args.live_mobile_receipt) if args.live_mobile_receipt else _default_receipt_path("live_mobile"),
        accessibility_receipt_path=(
            Path(args.accessibility_receipt)
            if args.accessibility_receipt
            else _default_receipt_path_if_exists("accessibility")
        ),
        failure_state_receipt_path=(
            Path(args.failure_state_receipt)
            if args.failure_state_receipt
            else _default_receipt_path_if_exists("failure_states")
        ),
        activation_to_value_receipt_path=(
            Path(args.activation_to_value_receipt)
            if args.activation_to_value_receipt
            else _default_receipt_path_if_exists("activation_to_value")
        ),
        public_smoke_receipt_path=Path(args.public_smoke_receipt) if args.public_smoke_receipt else _default_receipt_path("public_smoke"),
        authenticated_smoke_receipt_path=Path(args.authenticated_smoke_receipt) if args.authenticated_smoke_receipt else _default_receipt_path("authenticated_smoke"),
        tour_control_receipt_path=Path(args.tour_control_receipt) if args.tour_control_receipt else _default_receipt_path("tour_control"),
        export_discovery_receipt_path=(
            Path(args.export_discovery_receipt)
            if args.export_discovery_receipt
            else _default_receipt_path("export_discovery")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        import_manifest_receipt_path=(
            Path(args.import_manifest_receipt)
            if args.import_manifest_receipt
            else _default_receipt_path("import_manifest")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        billing_receipt_path=Path(args.billing_receipt) if args.billing_receipt else _default_receipt_path("billing"),
        tour_provider_ownership_receipt_path=Path(args.tour_provider_ownership_receipt) if args.tour_provider_ownership_receipt else _default_receipt_path("tour_provider_ownership"),
        vendor_tooling_receipt_path=(
            Path(args.vendor_tooling_receipt)
            if args.vendor_tooling_receipt
            else _default_receipt_path("vendor_tooling")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        whole_project_scope_receipt_path=Path(args.whole_project_scope_receipt) if args.whole_project_scope_receipt else _default_receipt_path("whole_project_scope"),
        security_posture_receipt_path=Path(args.security_posture_receipt) if args.security_posture_receipt else _default_receipt_path("security_posture"),
        release_hygiene_receipt_path=Path(args.release_hygiene_receipt) if args.release_hygiene_receipt else _default_receipt_path("release_hygiene"),
        furniture_style_contract_receipt_path=Path(args.furniture_style_contract_receipt) if args.furniture_style_contract_receipt else _default_receipt_path("furniture_style_contract"),
        bts_methodology_contract_receipt_path=Path(args.bts_methodology_contract_receipt) if args.bts_methodology_contract_receipt else _default_receipt_path("bts_methodology_contract"),
        tour_delivery_contract_receipt_path=Path(args.tour_delivery_contract_receipt) if args.tour_delivery_contract_receipt else _default_receipt_path("tour_delivery_contract"),
        map_preview_flagship_receipt_path=Path(args.map_preview_flagship_receipt) if args.map_preview_flagship_receipt else _default_receipt_path("map_preview_flagship"),
        browser_3d_gate_receipt_path=Path(args.browser_3d_gate_receipt) if args.browser_3d_gate_receipt else _default_receipt_path("browser_3d_gate"),
        runtime_reconstruction_receipt_path=(
            Path(args.runtime_reconstruction_receipt)
            if args.runtime_reconstruction_receipt
            else _default_receipt_path("runtime_reconstruction")
        ),
        service_generated_reconstruction_receipt_path=(
            Path(args.service_generated_reconstruction_receipt)
            if args.service_generated_reconstruction_receipt
            else _default_receipt_path("service_generated_reconstruction")
        ),
        walkthrough_quality_receipt_path=(
            Path(args.walkthrough_quality_receipt)
            if args.walkthrough_quality_receipt
            else _default_receipt_path("walkthrough_quality")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        walkthrough_provider_proof_receipt_path=(
            Path(args.walkthrough_provider_proof_receipt)
            if args.walkthrough_provider_proof_receipt
            else _default_receipt_path("walkthrough_provider_proof")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        scene_video_readiness_receipt_path=(
            Path(args.scene_video_readiness_receipt)
            if args.scene_video_readiness_receipt
            else _default_receipt_path("scene_video_readiness")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        scene_video_readiness_verifier_receipt_path=(
            Path(args.scene_video_readiness_verifier_receipt)
            if args.scene_video_readiness_verifier_receipt
            else _default_receipt_path("scene_video_readiness_verifier")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        scene_video_runtime_status_receipt_path=(
            Path(args.scene_video_runtime_status_receipt)
            if args.scene_video_runtime_status_receipt
            else _default_receipt_path("scene_video_runtime_status")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        scene_video_provider_refresh_packet_path=(
            Path(args.scene_video_provider_refresh_packet)
            if args.scene_video_provider_refresh_packet
            else _default_receipt_path("scene_video_provider_refresh_packet")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        scene_video_provider_refresh_packet_verifier_receipt_path=(
            Path(args.scene_video_provider_refresh_packet_verifier_receipt)
            if args.scene_video_provider_refresh_packet_verifier_receipt
            else _default_receipt_path("scene_video_provider_refresh_packet_verifier")
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        advanced_visual_binding_receipt_path=(
            Path(args.advanced_visual_binding_receipt)
            if args.advanced_visual_binding_receipt
            else None
        )
        if cli_claim_scope == "advanced_visual"
        else None,
        slo_evidence_receipt_path=(
            Path(args.slo_evidence_receipt) if args.slo_evidence_receipt else None
        ),
        evidence_overlay_receipt_path=(
            Path(args.evidence_overlay_receipt)
            if args.evidence_overlay_receipt
            else None
        ),
        rybbit_evidence_receipt_path=(
            Path(args.rybbit_evidence_receipt)
            if args.rybbit_evidence_receipt
            else None
        ),
        global_market_envelope_receipt_path=(
            Path(args.global_market_envelope_receipt)
            if args.global_market_envelope_receipt
            else _default_receipt_path_if_exists("global_market_envelope")
        ),
        incident_support_receipt_path=(
            Path(args.incident_support_receipt)
            if args.incident_support_receipt
            else _default_receipt_path_if_exists("incident_support")
        ),
        global_experience_receipt_path=(
            Path(args.global_experience_receipt)
            if args.global_experience_receipt
            else _default_receipt_path_if_exists("global_experience")
        ),
        jurisdiction_privacy_rights_receipt_path=(
            Path(args.jurisdiction_privacy_rights_receipt)
            if args.jurisdiction_privacy_rights_receipt
            else _default_receipt_path_if_exists("jurisdiction_privacy_rights")
        ),
        expected_release_commit_sha=args.expected_release_sha,
        expected_release_image_digest=args.expected_image_digest,
        expected_release_deployment_id=args.expected_release_deployment_id,
        expected_release_manifest_sha256=args.expected_release_manifest_sha256,
        expected_performance_chromium_executable_path=(
            args.expected_performance_chromium_executable_path
        ),
        expected_performance_chromium_executable_sha256=(
            args.expected_performance_chromium_executable_sha256
        ),
        expected_public_origin=args.expected_public_origin,
        expected_teable_origin=args.expected_teable_origin,
        expected_teable_base_id_sha256=args.expected_teable_base_id_sha256,
        expected_evidence_overlay_phase=args.expected_evidence_overlay_phase,
        expected_rybbit_origin=args.expected_rybbit_origin,
        expected_rybbit_site_id_sha256=args.expected_rybbit_site_id_sha256,
        slo_evidence_max_age_seconds=args.slo_evidence_max_age_seconds,
        evidence_overlay_max_age_hours=args.evidence_overlay_max_age_hours,
        rybbit_evidence_max_age_minutes=args.rybbit_evidence_max_age_minutes,
        id_austria_receipt_path=Path(args.id_austria_receipt) if args.id_austria_receipt else _default_receipt_path("id_austria"),
        repair_canary_receipt_path=Path(args.repair_canary_receipt) if args.repair_canary_receipt else _default_receipt_path("repair_canary"),
        provider_catalog_receipt_path=(
            Path(args.provider_catalog_receipt)
            if args.provider_catalog_receipt
            else _default_receipt_path_if_exists("provider_catalog")
        ),
        provider_matrix_receipt_path=Path(args.provider_matrix_receipt) if args.provider_matrix_receipt else _default_receipt_path("provider_matrix"),
        max_receipt_age_hours=args.max_receipt_age_hours,
        readiness_profile=args.profile,
        claim_scope=cli_claim_scope,
        required_browser_engines=configured_browser_engines,
    )
    if launch_evidence_requested:
        _apply_canonical_launch_evidence(
            receipt,
            slo_receipt=launch_slo_receipt,
            observability_receipt=launch_observability_receipt,
            slo_receipt_path=launch_slo_receipt_path,
            observability_receipt_path=launch_observability_receipt_path,
            validation_errors=launch_validation_errors,
        )
    output = json.dumps(receipt, indent=2, sort_keys=True)
    if args.write:
        out_path = Path(args.write)
        _write_gold_status_output(out_path, output)
    try:
        print(output)
    except BrokenPipeError:
        with contextlib.suppress(Exception):
            sys.stdout.close()
    if receipt.get("status") == "pass":
        return 0
    return 2 if args.fail_on_blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
