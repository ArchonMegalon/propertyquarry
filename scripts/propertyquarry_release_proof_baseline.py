#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from typing import Any


APPROVED_BASELINE_ID = "propertyquarry.release-proof.v2"
APPROVED_BASELINE_SHA256 = "9ba1ca801fbe687b4e26a5d64716f9ff18cf42acedad6aeded2f8352d60d4620"
APPROVED_PRODUCT = "propertyquarry"
APPROVED_SURFACE = "propertyquarry_flagship_release_control"
APPROVED_PROOF_TARGET = "propertyquarry"
GLOBAL_LAUNCH_CONTRACT_SCHEMA = "propertyquarry.global_flagship_goal.v1"
GLOBAL_LAUNCH_MARKET_ENVELOPE_AUTHORITY = (
    "_completion/property_global_market_envelope/release-gate.json"
)
GLOBAL_LAUNCH_TERMINAL_COMMAND = (
    "/usr/libexec/propertyquarry/propertyquarry-global-launch-terminal --manifest "
    "/run/propertyquarry/release-evidence/global-launch-core-manifest.v1.json"
)
GLOBAL_LAUNCH_TERMINAL_MANIFEST_SCHEMA = (
    "propertyquarry.global_launch_terminal_manifest.v1"
)
GLOBAL_LAUNCH_TERMINAL_MANIFEST_PATH = (
    "/run/propertyquarry/release-evidence/global-launch-core-manifest.v1.json"
)
GLOBAL_LAUNCH_TERMINAL_RESULT_SCHEMA = (
    "propertyquarry.global_launch_terminal_result.v1"
)
GLOBAL_LAUNCH_TERMINAL_BUNDLE_SCHEMA = (
    "propertyquarry.global_launch_terminal_bundle.v1"
)
GLOBAL_LAUNCH_TERMINAL_AUTHORITIES = (
    "release_preflight",
    "disaster_recovery",
    "capacity",
    "observability_operations",
    "controller_attestation",
)
GLOBAL_LAUNCH_GOVERNANCE_RECEIPTS = (
    "global_market_envelope",
    "incident_support",
    "global_experience",
    "jurisdiction_privacy_rights",
)
GLOBAL_LAUNCH_REQUIRED_BROWSER_ENGINES = ("chromium", "firefox", "webkit")
GLOBAL_LAUNCH_REQUIRED_EVIDENCE_LEVELS = (
    "source_contract",
    "candidate_proof",
    "production_like_proof",
    "protected_live_proof",
)
GLOBAL_LAUNCH_REQUIRED_DOMAINS = (
    "exact_release_identity_and_provenance",
    "complete_customer_value_loop",
    "supported_market_and_locale_envelope",
    "wcag_2_2_aa_accessibility",
    "performance_and_network_resilience",
    "privacy_security_and_regional_controls",
    "reliability_capacity_and_operations",
    "protected_live_launch_authority",
)
GLOBAL_LAUNCH_REQUIRED_MARKET_DIMENSIONS = (
    "country_code",
    "ui_locales",
    "content_languages",
    "currencies",
    "measurement_system",
    "timezone_policy",
    "address_model",
    "provider_set",
    "listing_modes",
    "privacy_region",
    "support_window",
)
GLOBAL_LAUNCH_REQUIRED_PERFORMANCE_METRICS = {
    "lcp_p75_ms_max": 2500,
    "inp_p75_ms_max": 200,
    "cls_p75_max": 0.1,
}
PRIMARY_SOURCE_TEST_FILE = "tests/test_propertyquarry_workspace_redesign.py"
PRIMARY_SOURCE_CASES = (
    "test_propertyquarry_workspace_routes_render_greenfield_surfaces",
    "test_propertyquarry_failed_run_stays_on_activity_surface",
    "test_property_workspace_sign_out_clears_workspace_session_cookie",
    "test_property_saved_shortlist_candidates_persist_across_runs",
    "test_propertyquarry_account_exposes_working_lifecycle_controls",
    "test_propertyquarry_pricing_checkout_failure_copy_is_safe_and_accessible",
    "test_propertyquarry_public_home_survives_unreadable_optional_tour_media",
)
REAL_BROWSER_TEST_FILE = "tests/e2e/test_propertyquarry_greenfield_browser.py"
PACKETS_TOURS_REAL_BROWSER_CASES = (
    "test_propertyquarry_flagship_operating_loop_in_browser",
    "test_propertyquarry_best_match_opens_hosted_3d_tour_and_flythrough_in_real_browser",
    "test_propertyquarry_blocked_3d_tour_can_be_retried_from_research_packet_in_real_browser",
    "test_propertyquarry_research_detail_never_shows_fake_open_tour_for_generated_reconstruction_status",
    "test_propertyquarry_generated_reconstruction_public_launch_renders_honest_shell_in_real_browser",
    "test_propertyquarry_generated_reconstruction_public_launch_is_mobile_safe",
    "test_propertyquarry_expired_flat_preview_explains_3d_unavailable_in_real_browser",
)
REAL_BROWSER_CASES = (
    "test_propertyquarry_greenfield_workspace_in_real_browser",
    "test_propertyquarry_greenfield_workspace_is_mobile_usable",
    "test_propertyquarry_expired_session_next_action_moves_keyboard_focus_to_sign_in_options",
    "test_propertyquarry_workbench_candidate_history_stays_in_place",
    *PACKETS_TOURS_REAL_BROWSER_CASES,
    "test_propertyquarry_generates_private_opportunity_brief_in_real_browser",
    "test_propertyquarry_packet_tracks_followup_state_in_browser",
    "test_propertyquarry_account_notifications_save_multi_channel_preferences_in_real_browser",
    "test_propertyquarry_browser_alert_button_toggles_enabled_state",
    "test_propertyquarry_research_evidence_states_and_links_render_in_real_browser",
)
EVIDENCE_OVERLAY_TEST_FILE = "tests/test_property_evidence_overlays.py"
EVIDENCE_OVERLAY_CASES = (
    "test_property_research_rows_preserve_evidence_states_and_original_article_link",
)

APPROVED_EVIDENCE_SOURCES = (
    (PRIMARY_SOURCE_TEST_FILE, PRIMARY_SOURCE_CASES),
    (REAL_BROWSER_TEST_FILE, REAL_BROWSER_CASES),
    (EVIDENCE_OVERLAY_TEST_FILE, EVIDENCE_OVERLAY_CASES),
)
APPROVED_JOURNEY_EVIDENCE = (
    (
        "public_entry",
        (
            (
                PRIMARY_SOURCE_TEST_FILE,
                ("test_propertyquarry_public_home_survives_unreadable_optional_tour_media",),
            ),
        ),
    ),
    (
        "onboarding_auth",
        (
            (
                PRIMARY_SOURCE_TEST_FILE,
                ("test_property_workspace_sign_out_clears_workspace_session_cookie",),
            ),
            (
                REAL_BROWSER_TEST_FILE,
                ("test_propertyquarry_expired_session_next_action_moves_keyboard_focus_to_sign_in_options",),
            ),
        ),
    ),
    (
        "search_ranking",
        (
            (
                PRIMARY_SOURCE_TEST_FILE,
                (
                    "test_propertyquarry_workspace_routes_render_greenfield_surfaces",
                    "test_propertyquarry_failed_run_stays_on_activity_surface",
                ),
            ),
            (
                REAL_BROWSER_TEST_FILE,
                ("test_propertyquarry_greenfield_workspace_in_real_browser",),
            ),
        ),
    ),
    (
        "shortlist_research_revisit",
        (
            (
                PRIMARY_SOURCE_TEST_FILE,
                ("test_property_saved_shortlist_candidates_persist_across_runs",),
            ),
            (
                REAL_BROWSER_TEST_FILE,
                (
                    "test_propertyquarry_greenfield_workspace_is_mobile_usable",
                    "test_propertyquarry_workbench_candidate_history_stays_in_place",
                    "test_propertyquarry_research_evidence_states_and_links_render_in_real_browser",
                ),
            ),
            (EVIDENCE_OVERLAY_TEST_FILE, EVIDENCE_OVERLAY_CASES),
        ),
    ),
    (
        "account_pricing_privacy_recovery",
        (
            (
                PRIMARY_SOURCE_TEST_FILE,
                (
                    "test_propertyquarry_account_exposes_working_lifecycle_controls",
                    "test_propertyquarry_pricing_checkout_failure_copy_is_safe_and_accessible",
                ),
            ),
        ),
    ),
    (
        "packets_tours",
        ((REAL_BROWSER_TEST_FILE, PACKETS_TOURS_REAL_BROWSER_CASES),),
    ),
    (
        "feedback",
        (
            (
                REAL_BROWSER_TEST_FILE,
                (
                    "test_propertyquarry_generates_private_opportunity_brief_in_real_browser",
                    "test_propertyquarry_packet_tracks_followup_state_in_browser",
                ),
            ),
        ),
    ),
    (
        "notifications",
        (
            (
                REAL_BROWSER_TEST_FILE,
                (
                    "test_propertyquarry_account_notifications_save_multi_channel_preferences_in_real_browser",
                    "test_propertyquarry_browser_alert_button_toggles_enabled_state",
                ),
            ),
        ),
    ),
)
APPROVED_REQUIRED_JOURNEY_IDS = tuple(journey_id for journey_id, _sources in APPROVED_JOURNEY_EVIDENCE)


def approved_evidence_sources() -> list[dict[str, Any]]:
    return [
        {"file": test_file, "cases": list(cases)}
        for test_file, cases in APPROVED_EVIDENCE_SOURCES
    ]


def approved_journey_evidence() -> dict[str, list[dict[str, Any]]]:
    return {
        journey_id: [
            {"file": test_file, "cases": list(cases)}
            for test_file, cases in sources
        ]
        for journey_id, sources in APPROVED_JOURNEY_EVIDENCE
    }


def _baseline_payload() -> dict[str, Any]:
    return {
        "id": APPROVED_BASELINE_ID,
        "evidence_sources": approved_evidence_sources(),
        "journeys": [
            {
                "journey_id": journey_id,
                "evidence_sources": approved_journey_evidence()[journey_id],
            }
            for journey_id in APPROVED_REQUIRED_JOURNEY_IDS
        ],
    }


def _computed_baseline_sha256() -> str:
    return hashlib.sha256(
        json.dumps(_baseline_payload(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def approved_baseline_binding() -> dict[str, str]:
    return {
        "id": APPROVED_BASELINE_ID,
        "sha256": APPROVED_BASELINE_SHA256,
    }


def _normalized_evidence_nodes(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    nodes: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        test_file = item.get("file")
        raw_cases = item.get("cases")
        if (
            not isinstance(test_file, str)
            or not test_file
            or test_file != test_file.strip()
            or not isinstance(raw_cases, list)
            or not raw_cases
            or any(
                not isinstance(case, str) or not case or case != case.strip()
                for case in raw_cases
            )
            or len(raw_cases) != len(set(raw_cases))
        ):
            return None
        nodes.append({"file": test_file, "cases": list(raw_cases)})
    return nodes


def approved_baseline_integrity_blockers() -> list[str]:
    blockers: list[str] = []
    computed_sha256 = _computed_baseline_sha256()
    if computed_sha256 != APPROVED_BASELINE_SHA256:
        blockers.append(
            "immutable approved baseline payload fingerprint does not match the pinned baseline: "
            f"computed={computed_sha256}; expected={APPROVED_BASELINE_SHA256}"
        )
    if len(APPROVED_REQUIRED_JOURNEY_IDS) != len(set(APPROVED_REQUIRED_JOURNEY_IDS)):
        blockers.append("immutable approved baseline journey IDs must be distinct and ordered")
    approved_sources = approved_evidence_sources()
    source_files = [str(source["file"]) for source in approved_sources]
    source_backed = [test_file for test_file in source_files if "/e2e/" not in test_file]
    real_browser = [test_file for test_file in source_files if "/e2e/" in test_file]
    if len(source_files) != len(set(source_files)) or not source_backed or len(real_browser) != 1:
        blockers.append(
            "immutable approved baseline must contain distinct ordered sources with at least one "
            "source-backed lane and exactly one real-browser lane"
        )

    allowed_cases = {
        str(source["file"]): set(str(case) for case in source["cases"])
        for source in approved_sources
    }
    mapped_cases = {test_file: set() for test_file in allowed_cases}
    for journey_id, sources in approved_journey_evidence().items():
        if not sources:
            blockers.append(f"immutable approved baseline journey {journey_id} has no evidence")
            continue
        for source in sources:
            test_file = str(source["file"])
            cases = [str(case) for case in source["cases"]]
            if test_file not in allowed_cases or not cases:
                blockers.append(f"immutable approved baseline journey {journey_id} has invalid evidence")
                continue
            unapproved = set(cases) - allowed_cases[test_file]
            duplicate = mapped_cases[test_file].intersection(cases)
            if unapproved or duplicate or len(cases) != len(set(cases)):
                blockers.append(
                    f"immutable approved baseline journey {journey_id} has unapproved or duplicate cases"
                )
            mapped_cases[test_file].update(cases)
    for test_file, expected_cases in allowed_cases.items():
        if mapped_cases[test_file] != expected_cases:
            blockers.append(
                f"immutable approved baseline journeys do not exactly cover source {test_file}"
            )
    return list(dict.fromkeys(blockers))


def approved_evidence_source_blockers(value: object) -> list[str]:
    blockers = approved_baseline_integrity_blockers()
    actual = _normalized_evidence_nodes(value)
    if actual != approved_evidence_sources():
        blockers.append("browser evidence sources do not match the immutable approved baseline")
    return list(dict.fromkeys(blockers))


def approved_journey_matrix_blockers(value: object) -> list[str]:
    integrity_blockers = approved_baseline_integrity_blockers()
    if not isinstance(value, dict):
        return list(
            dict.fromkeys(
                [*integrity_blockers, "journey evidence matrix does not match the immutable approved baseline"]
            )
        )
    blockers: list[str] = list(integrity_blockers)
    raw_required_ids = value.get("required_journey_ids")
    if (
        not isinstance(raw_required_ids, list)
        or raw_required_ids != list(APPROVED_REQUIRED_JOURNEY_IDS)
    ):
        blockers.append("journey evidence matrix required IDs do not match the immutable approved baseline")

    raw_rows = value.get("rows")
    if not isinstance(raw_rows, list) or any(not isinstance(row, dict) for row in raw_rows):
        blockers.append("journey evidence matrix rows do not match the immutable approved baseline")
        return blockers
    actual_ids = [row.get("journey_id") for row in raw_rows]
    if actual_ids != list(APPROVED_REQUIRED_JOURNEY_IDS):
        blockers.append("journey evidence matrix rows do not match the immutable approved journey order")

    rows_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for row in raw_rows:
        journey_id = row.get("journey_id")
        if not isinstance(journey_id, str) or not journey_id:
            continue
        if journey_id in rows_by_id:
            duplicate_ids.add(journey_id)
        else:
            rows_by_id[journey_id] = row
    if duplicate_ids:
        blockers.append("journey evidence matrix has duplicate journey IDs outside the immutable approved baseline")

    for journey_id, expected_sources in approved_journey_evidence().items():
        row = rows_by_id.get(journey_id)
        actual_sources = _normalized_evidence_nodes(row.get("evidence_sources")) if row else None
        if actual_sources != expected_sources:
            blockers.append(
                f"journey {journey_id} evidence sources do not match the immutable approved baseline"
            )
    return list(dict.fromkeys(blockers))


def approved_global_launch_contract_blockers(value: object) -> list[str]:
    if not isinstance(value, dict):
        return ["flagship gate seed lacks the governed global launch contract"]

    blockers: list[str] = []
    if value.get("schema") != GLOBAL_LAUNCH_CONTRACT_SCHEMA:
        blockers.append("global launch contract schema is not the approved version")
    if value.get("claim") != "global_grade_with_explicit_market_envelope":
        blockers.append("global launch contract has an unsupported claim")
    if value.get("universal_market_claim") is not False:
        blockers.append("global launch contract must reject an unproved universal-market claim")
    if value.get("terminal_profile") != "launch" or value.get("terminal_claim_scope") != "core":
        blockers.append("global launch contract must terminate only on launch-tier Core Gold")
    if value.get("terminal_command") != GLOBAL_LAUNCH_TERMINAL_COMMAND:
        blockers.append("global launch contract terminal command is not canonical")
    if (
        value.get("terminal_manifest_schema")
        != GLOBAL_LAUNCH_TERMINAL_MANIFEST_SCHEMA
        or value.get("terminal_manifest_path")
        != GLOBAL_LAUNCH_TERMINAL_MANIFEST_PATH
        or value.get("terminal_result_schema")
        != GLOBAL_LAUNCH_TERMINAL_RESULT_SCHEMA
        or value.get("terminal_installed_bundle_schema")
        != GLOBAL_LAUNCH_TERMINAL_BUNDLE_SCHEMA
    ):
        blockers.append("global launch contract terminal manifest/result/bundle ABI is not canonical")
    if value.get("terminal_required_authorities") != list(
        GLOBAL_LAUNCH_TERMINAL_AUTHORITIES
    ):
        blockers.append("global launch contract terminal authorities are incomplete")
    if value.get("terminal_required_governance_receipts") != list(
        GLOBAL_LAUNCH_GOVERNANCE_RECEIPTS
    ):
        blockers.append("global launch contract governance receipts are incomplete")
    if value.get("terminal_requires_signed_invocation_contract") is not True:
        blockers.append("global launch contract must require a signed installed invocation contract")
    if value.get("required_browser_engines") != list(GLOBAL_LAUNCH_REQUIRED_BROWSER_ENGINES):
        blockers.append("global launch contract must require Chromium, Firefox, and WebKit")
    if value.get("required_evidence_levels") != list(GLOBAL_LAUNCH_REQUIRED_EVIDENCE_LEVELS):
        blockers.append("global launch contract evidence levels are incomplete or out of order")
    if value.get("required_domains") != list(GLOBAL_LAUNCH_REQUIRED_DOMAINS):
        blockers.append("global launch contract control domains are incomplete or out of order")
    if value.get("accessibility_standard") != "WCAG 2.2 Level AA":
        blockers.append("global launch contract accessibility target must be WCAG 2.2 Level AA")
    if value.get("performance_field_thresholds") != GLOBAL_LAUNCH_REQUIRED_PERFORMANCE_METRICS:
        blockers.append("global launch contract field performance thresholds are incomplete")

    envelope = value.get("market_envelope")
    if not isinstance(envelope, dict):
        blockers.append("global launch contract lacks a governed market envelope")
    else:
        if envelope.get("authority") != GLOBAL_LAUNCH_MARKET_ENVELOPE_AUTHORITY:
            blockers.append("global launch market envelope authority is not canonical")
        if envelope.get("required_status_for_launch") != "pass":
            blockers.append("global launch market envelope must pass before launch")
        if envelope.get("only_full_e2e_markets_are_launch_supported") is not True:
            blockers.append("global launch contract must exclude catalog-only markets")
        if envelope.get("required_dimensions") != list(GLOBAL_LAUNCH_REQUIRED_MARKET_DIMENSIONS):
            blockers.append("global launch market dimensions are incomplete or out of order")

    separation = value.get("core_advanced_visual_separation")
    if not isinstance(separation, dict):
        blockers.append("global launch contract lacks Core/Advanced Visual separation")
    else:
        if separation.get("core_must_not_require_paid_advanced_visuals") is not True:
            blockers.append("Core Gold must not require paid advanced visuals")
        if separation.get("advanced_visual_claim_requires_additive_live_binding") is not True:
            blockers.append("Advanced Visual Gold must require an additive live binding")
    return list(dict.fromkeys(blockers))


def approved_seed_baseline_blockers(seed: object) -> list[str]:
    if not isinstance(seed, dict):
        return ["flagship gate seed does not match the immutable approved release-proof baseline"]
    proof_contract = seed.get("browser_workflow_proof")
    evidence_sources = proof_contract.get("evidence_sources") if isinstance(proof_contract, dict) else None
    blockers = [
        *approved_evidence_source_blockers(evidence_sources),
        *approved_journey_matrix_blockers(seed.get("journey_evidence_matrix")),
        *approved_global_launch_contract_blockers(seed.get("global_launch_contract")),
    ]
    if seed.get("product") != APPROVED_PRODUCT:
        blockers.append(
            f"flagship gate seed product must be the exact standalone target {APPROVED_PRODUCT}"
        )
    if seed.get("surface") != APPROVED_SURFACE:
        blockers.append(
            f"flagship gate seed surface must be the exact standalone surface {APPROVED_SURFACE}"
        )
    proof_target = proof_contract.get("proof_target") if isinstance(proof_contract, dict) else None
    if proof_target != APPROVED_PROOF_TARGET:
        blockers.append(
            f"flagship gate seed proof target must be the exact standalone target {APPROVED_PROOF_TARGET}"
        )
    return list(dict.fromkeys(blockers))
