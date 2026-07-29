from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import propertyquarry_gold_status as gold_status
from scripts import propertyquarry_advanced_visual_gold_binding as advanced_binding
from scripts import propertyquarry_continuous_ux_gate as continuous_ux_gate
from scripts import property_evidence_overlay_read_model as overlay_read_model
from scripts import propertyquarry_rybbit_evidence as rybbit_evidence
from scripts.propertyquarry_gold_status import _latest_receipt_path, build_gold_status_receipt


ROOT = Path(__file__).resolve().parents[1]
_CONTINUOUS_UX_MISSING = object()
_PERFORMANCE_RELEASE_COMMIT_SHA = "a" * 40
_PERFORMANCE_RELEASE_IMAGE_DIGEST = "sha256:" + "b" * 64
_PERFORMANCE_RELEASE_DEPLOYMENT_ID = "propertyquarry-production"
_PERFORMANCE_MANIFEST_SHA256 = "c123456789abcdef" * 4


def _existing_python_executable_identity() -> dict[str, object]:
    executable_path = Path(sys.executable).resolve(strict=True)
    digest = hashlib.sha256()
    with executable_path.open("rb") as executable:
        while chunk := executable.read(1024 * 1024):
            digest.update(chunk)
    return {
        "engine": "chromium",
        "browser_version": "145.0.7632.6",
        "playwright_version": "1.60.0",
        "executable_path": str(executable_path),
        "executable_sha256": digest.hexdigest(),
        "executable_bytes": executable_path.stat().st_size,
    }


_PYTHON_EXECUTABLE_IDENTITY = _existing_python_executable_identity()


def _secure_test_chromium_executable_identity(tmp_path: Path) -> dict[str, object]:
    chromium_dir = tmp_path / "controller-browser" / "chromium-145"
    chromium_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    executable_path = chromium_dir / "chrome"
    prefix = b"\x7fELF\x02\x01\x01\x00"
    marker = b"PropertyQuarry Chromium controller-bound test executable"
    executable_bytes = (
        prefix
        + b"\x00"
        * (gold_status.AUTHENTICATED_PERFORMANCE_MIN_EXECUTABLE_BYTES - len(prefix))
        + marker
    )
    executable_path.write_bytes(executable_bytes)
    executable_path.chmod(0o700)
    return {
        "engine": "chromium",
        "browser_version": "145.0.7632.6",
        "playwright_version": "1.60.0",
        "executable_path": str(executable_path),
        "executable_sha256": hashlib.sha256(executable_bytes).hexdigest(),
        "executable_bytes": len(executable_bytes),
    }


def test_gold_cli_requires_launch_profile_for_canonical_launch_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["propertyquarry_gold_status.py", "--require-launch-evidence", "--profile", "standard"],
    )

    with pytest.raises(SystemExit) as exc_info:
        gold_status.main()

    assert exc_info.value.code == 2


@pytest.mark.parametrize(
    ("option", "value"),
    (
        ("--max-receipt-age-hours", "nan"),
        ("--evidence-overlay-max-age-hours", "inf"),
        ("--rybbit-evidence-max-age-minutes", "-inf"),
    ),
)
def test_gold_cli_rejects_nonfinite_age_policies(
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    value: str,
) -> None:
    monkeypatch.setattr(sys, "argv", ["propertyquarry_gold_status.py", option, value])

    with pytest.raises(SystemExit) as exc_info:
        gold_status.main()

    assert exc_info.value.code == 2


def _write_json(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _magicfit_playback_payload() -> dict[str, object]:
    media_identity = "/tours/files/magicfit-proof-tour/walkthrough.mp4"
    return {
        "playback_ok": True,
        "playable_count": 1,
        "ready_count": 1,
        "evidence": [
            {
                "slug": "magicfit-proof-tour",
                "provider": "magicfit",
                "control_path": media_identity,
                "media_identity": media_identity,
            }
        ],
    }


def _minimal_gold_receipt_args(tmp_path: Path, *, generated_at: str) -> dict[str, object]:
    executable_identity = _secure_test_chromium_executable_identity(tmp_path)
    performance_payload = _performance_payload(
        executable_identity=executable_identity,
    )
    performance_payload["generated_at"] = generated_at
    provider_matrix_payload = _provider_matrix_payload()
    provider_matrix_payload["generated_at"] = generated_at
    return {
        "performance_receipt_path": _write_json(tmp_path / "performance.json", performance_payload),
        "tour_control_receipt_path": _write_json(
            tmp_path / "tour-controls.json",
            {
                "generated_at": generated_at,
                "status": "pass",
                "ready_provider_modes": ["matterport", "3dvista", "magicfit"],
                "missing_provider_modes": [],
                "magicfit_playback": _magicfit_playback_payload(),
            },
        ),
        "export_discovery_receipt_path": _write_json(
            tmp_path / "discovery.json",
            {"generated_at": generated_at, "status": "ready", "import_count": 1, "rejected_count": 0},
        ),
        "repair_canary_receipt_path": _write_json(
            tmp_path / "repair.json",
            {
                "generated_at": generated_at,
                "status": "pass",
                "run_status": "completed_partial",
                "source_repair_status": "returned",
                "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
            },
        ),
        "provider_matrix_receipt_path": _write_json(
            tmp_path / "provider-matrix.json",
            provider_matrix_payload,
        ),
        "expected_performance_chromium_executable_path": executable_identity[
            "executable_path"
        ],
        "expected_performance_chromium_executable_sha256": executable_identity[
            "executable_sha256"
        ],
    }


def test_gold_status_identifies_schema_and_exact_evaluated_release(tmp_path: Path) -> None:
    generated_at = "2026-07-19T12:00:00+00:00"
    release_sha = "a" * 40
    image_digest = "sha256:" + "b" * 64

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        expected_release_commit_sha=release_sha.upper(),
        expected_release_image_digest=image_digest.upper(),
        expected_release_deployment_id=_PERFORMANCE_RELEASE_DEPLOYMENT_ID,
        now=datetime(2026, 7, 19, 12, 5, tzinfo=timezone.utc),
    )

    assert receipt["schema"] == "propertyquarry.gold_status.v1"
    assert receipt["release_identity"] == {
        "commit_sha": release_sha,
        "image_digest": image_digest,
    }


def test_gold_status_builder_rejects_nan_receipt_age_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_receipt_age_hours_must_be_finite"):
        build_gold_status_receipt(
            **_minimal_gold_receipt_args(
                tmp_path,
                generated_at="2026-07-19T12:00:00+00:00",
            ),
            max_receipt_age_hours=float("nan"),
        )


def _flagship_customer_ux_receipt_args(tmp_path: Path, *, generated_at: str) -> dict[str, object]:
    release_sha = "a" * 40
    continuous_rows: list[dict[str, object]] = []
    for browser_engine in gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES:
        for route in gold_status.REQUIRED_CONTINUOUS_UX_ROUTES:
            first_value_samples = (
                [220.0, 240.0, 260.0]
                if browser_engine
                == gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_ENGINE
                else [240.0]
            )
            state_metrics: dict[str, object] = {}
            checks = [
                {"name": name, "ok": True}
                for name in gold_status.REQUIRED_CONTINUOUS_UX_ROW_CHECKS
            ]
            if route == "/app/search":
                state_metrics.update(
                    {
                        "loading_action_available": True,
                        "loading_state_visible": True,
                        "loading_state_semantic": True,
                    }
                )
                checks.extend(
                    [
                        {"name": "loading_action_available", "ok": True},
                        {"name": "loading_state_visible", "ok": True},
                        {"name": "loading_state_semantic", "ok": True},
                    ]
                )
            elif route == "/app/search?continuous_ux_state=offline":
                state_metrics.update(
                    {
                        "error_state_visible": True,
                        "error_state_semantic": True,
                        "error_state_recovered_online": True,
                    }
                )
                checks.extend(
                    [
                        {"name": "error_state_visible", "ok": True},
                        {"name": "error_state_semantic", "ok": True},
                        {"name": "error_state_recovers_online", "ok": True},
                    ]
                )
            continuous_rows.append(
                {
                    "route": route,
                    "browser_engine": browser_engine,
                    "status_code": 200,
                    "ok": True,
                    "error": "",
                    "checks": checks,
                    "metrics": {
                        "document_ready_state": "complete",
                        "final_route": route,
                        "main_visible": route != "/app/search?continuous_ux_state=offline",
                        "navigation_visible": True,
                        "body_text_length": 120,
                        "visible_interactive_count": 4,
                        "horizontal_overflow": False,
                        "visible_image_count": 0,
                        "terminal_visible_image_count": 0,
                        "broken_visible_image_count": 0,
                        "zoom_400_percent": 400,
                        "zoom_400_viewport_width": 320,
                        "zoom_400_scroll_width": 320,
                        "zoom_400_reflow_without_horizontal_scroll": True,
                        "zoom_400_clipped_interactive_count": 0,
                        "first_value_ms": 240.0,
                        "first_value_cold_ms": 280.0,
                        "first_value_initial_samples_ms": first_value_samples,
                        "first_value_samples_ms": first_value_samples,
                        "first_value_sample_count": len(first_value_samples),
                        "first_value_retry_used": False,
                        "first_value_gated": (
                            browser_engine
                            == gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_ENGINE
                        ),
                        "first_value_basis": gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BASIS,
                        "provider_response_mocked": False,
                        "request_interception_mode": "origin_scoped_headers_continue_only",
                        "route_fulfill_count": 0,
                        **state_metrics,
                    },
                }
            )
    visual_case_ids = list(continuous_ux_gate.VISUAL_BASELINE_REQUIRED_CASE_IDS)
    visual_capture = dict(continuous_ux_gate.VISUAL_BASELINE_CAPTURE_CONTRACT)
    visual_browser_version = "Chromium 140.0.7339.16"
    visual_playwright_version = "1.54.0"
    expected_actual_pngs = sorted(f"{case_id}.png" for case_id in visual_case_ids)
    visual_source_binding = {
        "schema": continuous_ux_gate.SOURCE_BINDING_SCHEMA,
        "generated_at": generated_at,
        "status": "pass",
        "required_checks": list(continuous_ux_gate.SOURCE_BINDING_REQUIRED_CHECKS),
        "failure_count": 0,
        "failures": [],
        "manifest_runtime_commit": release_sha,
        "head_commit": release_sha,
        "parent_commit": "2" * 40,
        "merge_commit_required": False,
        "merge_base_parent_commit": "",
        "merge_parent_commits": ["2" * 40],
        "head_tree": "3" * 40,
        "reviewed_envelope_commit": "",
        "reviewed_envelope_parent_commits": [],
        "reviewed_envelope_tree": "",
        "merge_tree_matches_reviewed_envelope": False,
        "manifest_descendant_paths": [],
        "manifest_metadata_only_ancestor": False,
        "tracked_dirty_path_count": 0,
        "untracked_release_source_count": 0,
        "note": "Repository hygiene and release-manifest authority gate.",
    }
    visual_outcomes = [
        {
            "case_id": case_id,
            "status": "pass",
            "reasons": [],
            "baseline_path": f"images/{case_id}.png",
            "actual_path": f"{case_id}.png",
            "diff_path": f"{case_id}.diff.png",
            "expected_dimensions": {"width": width, "height": height},
            "baseline_dimensions": {"width": width, "height": height},
            "actual_dimensions": {"width": width, "height": height},
            "baseline_sha256": "b" * 64,
            "expected_baseline_sha256": "b" * 64,
            "actual_sha256": "c" * 64,
            "diff_sha256": "d" * 64,
            "changed_pixel_count": 0,
            "total_pixel_count": width * height,
            "changed_pixel_ratio": 0.0,
            "maximum_yiq_delta": 0.0,
        }
        for case_id, width, height in continuous_ux_gate.VISUAL_BASELINE_REQUIRED_CASES
    ]
    visual_baseline = {
        "schema": continuous_ux_gate.VISUAL_BASELINE_SCHEMA,
        "generated_at": generated_at,
        "status": "pass",
        "release_commit_sha": release_sha,
        "expected_release_commit_sha": release_sha,
        "proof_mode": continuous_ux_gate.VISUAL_BASELINE_PROOF_MODE,
        "screenshot_pixel_comparison": True,
        "update_mode": False,
        "receipt_written": True,
        "source_binding_receipt_sha256": (
            continuous_ux_gate.source_binding_payload_sha256(
                visual_source_binding
            )
        ),
        "source_binding": visual_source_binding,
        "manifest": {
            "schema": continuous_ux_gate.VISUAL_BASELINE_MANIFEST_SCHEMA,
            "sha256": "e" * 64,
            "git_blob_sha1": "f" * 40,
            "case_count": len(visual_case_ids),
            "error": "",
        },
        "browser": {
            "name": "chromium",
            "version": visual_browser_version,
            "playwright_version": visual_playwright_version,
            "fingerprint_sha256": continuous_ux_gate.visual_baseline_payload_sha256(
                {
                    "browser_engine": "chromium",
                    "browser_version": visual_browser_version,
                    "playwright_version": visual_playwright_version,
                    "capture": visual_capture,
                }
            ),
            "capture": visual_capture,
        },
        "comparison": {
            "algorithm": continuous_ux_gate.VISUAL_BASELINE_ALGORITHM,
            "pixel_threshold": 0.1,
            "max_changed_pixel_ratio": 0.005,
        },
        "expected_case_ids": visual_case_ids,
        "observed_case_ids": visual_case_ids,
        "preflight": {
            "errors": [],
            "expected_actual_pngs": expected_actual_pngs,
            "observed_actual_pngs": expected_actual_pngs,
            "missing_actual_pngs": [],
            "extra_actual_pngs": [],
            "path_graph_safe": True,
            "actual_workspace_safe": True,
            "diff_workspace_safe": True,
        },
        "outcome_count": len(visual_case_ids),
        "failed_count": 0,
        "checks": [
            {"name": name, "ok": True}
            for name in continuous_ux_gate.VISUAL_BASELINE_REQUIRED_CHECKS
        ],
        "outcomes": visual_outcomes,
    }
    continuous_ux = {
        "schema": gold_status.REQUIRED_CONTINUOUS_UX_SCHEMA,
        "generated_at": generated_at,
        "status": "pass",
        "failed_count": 0,
        "release_commit_sha": release_sha,
        "proof_scope": gold_status.REQUIRED_CONTINUOUS_UX_PROOF_SCOPE,
        "proof_mode": gold_status.REQUIRED_CONTINUOUS_UX_PROOF_MODE,
        "production_claim": False,
        "deployed_or_live_proof": False,
        "storage_backend": "memory",
        "base_origin_kind": "loopback",
        "provider_response_mocking": False,
        "screenshot_pixel_comparison": True,
        "visual_baseline_receipt_sha256": (
            continuous_ux_gate.visual_baseline_payload_sha256(visual_baseline)
        ),
        "visual_baseline": visual_baseline,
        "first_value_budget_ms": gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BUDGET_MS,
        "first_value_basis": gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BASIS,
        "first_value_max_attempts": gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_MAX_ATTEMPTS,
        "required_browser_engines": list(
            gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES
        ),
        "required_routes": list(gold_status.REQUIRED_CONTINUOUS_UX_ROUTES),
        "required_state_kinds": ["loading", "error"],
        "expected_sample_count": len(continuous_rows),
        "observed_sample_count": len(continuous_rows),
        "passed_sample_count": len(continuous_rows),
        "missing_sample_count": 0,
        "duplicate_sample_count": 0,
        "checks": [
            {"name": name, "ok": True}
            for name in gold_status.REQUIRED_CONTINUOUS_UX_TOP_CHECKS
        ],
        "rows": continuous_rows,
    }
    configured_live_routes = (
        *gold_status.REQUIRED_LIVE_MOBILE_ROUTES,
        "/app/research/current-result?run_id=run-flagship",
        "/app/shortlist/run/run-flagship",
        "/tours/tour-flagship",
    )
    live_routes = []
    for browser_engine in gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES:
        for route in configured_live_routes:
            for width, height in gold_status.REQUIRED_FLAGSHIP_MOBILE_VIEWPORTS:
                route_checks = [
                    {"name": name, "ok": True}
                    for name in gold_status.REQUIRED_FLAGSHIP_BROWSER_CHECKS
                ]
                if route == "/app/billing":
                    route_checks.extend(
                        {"name": name, "ok": True}
                        for name in gold_status.REQUIRED_FLAGSHIP_BILLING_HANDOFF_CHECKS
                    )
                live_routes.append(
                    {
                        "route": route,
                        "browser_engine": browser_engine,
                        "status_code": 200,
                        "ok": True,
                        "viewport": {"width": width, "height": height},
                        "proof_mode": "playwright",
                        "checks": route_checks,
                        "metrics": {
                            "status_code": 200,
                            "browser_engine": browser_engine,
                            "viewport_width": width,
                            "viewport_height": height,
                            "body_width": width,
                            "min_action_height": 48,
                            "proof_mode": "playwright",
                            "browser_probe": True,
                            "navigation_committed": True,
                            "touch_capable": True,
                            "focus_navigation_ok": True,
                            **(
                                {"billing_readiness_state": "available"}
                                if route == "/app/billing"
                                else {}
                            ),
                        },
                    }
                )
    live_mobile = {
        "generated_at": generated_at,
        "status": "pass",
        "failed_count": 0,
        "route_count": len(live_routes),
        "configured_route_count": len(configured_live_routes),
        "proof_mode": "playwright_browser_all",
        "browser_engine": "matrix",
        "browser_engines": list(gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES),
        "required_browser_engines": list(gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES),
        "supported_viewports": [
            {"width": width, "height": height}
            for width, height in gold_status.REQUIRED_FLAGSHIP_MOBILE_VIEWPORTS
        ],
        "browser_proof": {
            "mode": "playwright_browser_all",
            "ready": True,
            "required_browser_engines": list(gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES),
            "observed_browser_engines": list(gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES),
            "missing_browser_engines": [],
            "configured_route_count": len(configured_live_routes),
            "expected_sample_count": len(live_routes),
            "proven_sample_count": len(live_routes),
            "missing_samples": [],
            "static_fallbacks": [],
        },
        "viewport": {"width": 390, "height": 844},
        "routes": live_routes,
        "coverage_checks": [
            {"name": name, "ok": True}
            for name in gold_status.REQUIRED_LIVE_MOBILE_COVERAGE_CHECKS
        ],
    }
    accessibility_routes = (
        *gold_status.REQUIRED_FLAGSHIP_ACCESSIBILITY_ROUTES,
        "/app/research/current-result?run_id=run-flagship",
        "/app/shortlist/run/run-flagship",
        "/tours/tour-flagship",
    )
    accessibility_rows = [
        {
            "route": route,
            "browser_engine": browser_engine,
            "ok": True,
            "checks": [
                {"name": name, "ok": True}
                for name in gold_status.REQUIRED_FLAGSHIP_ACCESSIBILITY_CHECKS
            ],
            "metrics": {
                "browser_engine": browser_engine,
                "axe_core_version": gold_status.REQUIRED_AXE_CORE_VERSION,
                "axe_serious_critical_count": 0,
                "axe_moderate_or_higher_wcag_count": 0,
            },
        }
        for browser_engine in gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES
        for route in accessibility_routes
    ]
    accessibility = {
        "generated_at": generated_at,
        "status": "pass",
        "failed_count": 0,
        "route_count": len(accessibility_rows),
        "axe_core_version": gold_status.REQUIRED_AXE_CORE_VERSION,
        "required_browser_engines": list(gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES),
        "configured_routes": list(accessibility_routes),
        "checks": [
            {"name": "axe_core_pinned_input", "ok": True},
            {"name": "accessibility_route_engine_matrix_complete", "ok": True},
            {"name": "public_information_route_matrix_configured", "ok": True},
            {"name": "flagship_static_route_matrix_configured", "ok": True},
            {"name": "literal_route_placeholders_absent", "ok": True},
            {"name": "research_detail_route_configured", "ok": True},
            {"name": "shortlist_run_route_configured", "ok": True},
            {"name": "public_tour_route_configured", "ok": True},
            {"name": "dialog_focus_interaction_sampled", "ok": True},
        ],
        "routes": accessibility_rows,
    }
    failure_state_rows = [
        {
            "state": state,
            "browser_engine": browser_engine,
            "ok": True,
            "customer_data_preserved": True,
            "preservation_probe": {
                "before": {
                    "ok": True,
                    "status_code": 200,
                    "body_bytes": 512,
                    "canonical_bytes": 480,
                    "sha256": "a" * 64,
                    "error": "",
                },
                "after": {
                    "ok": True,
                    "status_code": 200,
                    "body_bytes": 512,
                    "canonical_bytes": 480,
                    "sha256": "a" * 64,
                    "error": "",
                },
                "same_digest": True,
            },
            "checks": [
                {"name": name, "ok": True}
                for name in gold_status.REQUIRED_FLAGSHIP_FAILURE_STATE_CHECKS
            ],
        }
        for browser_engine in gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES
        for state in gold_status.REQUIRED_FLAGSHIP_FAILURE_STATES
    ]
    failure_states = {
        "generated_at": generated_at,
        "status": "pass",
        "failed_count": 0,
        "proof_mode": "playwright_browser_all",
        "required_browser_engines": list(gold_status.DEFAULT_REQUIRED_FLAGSHIP_BROWSER_ENGINES),
        "required_failure_states": list(gold_status.REQUIRED_FLAGSHIP_FAILURE_STATES),
        "preservation_probe_route": gold_status.REQUIRED_FLAGSHIP_FAILURE_PRESERVATION_ROUTE,
        "checks": [
            {"name": "required_failure_scenarios_configured", "ok": True},
            {"name": "browser_state_engine_matrix_complete", "ok": True},
            {"name": "no_provider_response_mocking", "ok": True},
            {"name": "customer_data_preservation_matrix_complete", "ok": True},
        ],
        "rows": failure_state_rows,
    }
    activation_steps = []
    for name in gold_status.REQUIRED_ACTIVATION_TO_VALUE_STEPS:
        step: dict[str, object] = {"name": name, "ok": True}
        if name == "account_create_or_reopen":
            step["outcome"] = "reopened"
        elif name == "real_provider_results":
            step.update({"provider_count": 2, "result_count": 3})
        elif name == "walkthrough_request_or_reuse":
            step["mode"] = "reused_ready"
        elif name == "safe_cleanup":
            step["session_cleared"] = True
        activation_steps.append(step)
    activation_to_value = {
        "generated_at": generated_at,
        "status": "pass",
        "failed_count": 0,
        "release_commit_sha": release_sha,
        "auth_mode": "google",
        "browser_engine": "chromium",
        "proof_mode": "deployed_playwright",
        "persona_digest": "flagship-persona-digest",
        "run_key": "flagship-activation-20260713-01",
        "live_contract": {
            "explicit_persona": True,
            "principal_headers_forbidden": True,
            "session_injection_forbidden": True,
            "provider_response_mocking_forbidden": True,
            "local_execution_forbidden": True,
            "deployed_playwright_runner": True,
        },
        "checks": [
            {"name": "protected_live_configuration", "ok": True},
            {"name": "idempotent_run_reservation", "ok": True},
            {"name": "activation_step_matrix_complete", "ok": True},
            {"name": "safe_cleanup_complete", "ok": True},
        ],
        "steps": activation_steps,
    }
    public_smoke = {
        "generated_at": generated_at,
        "status": "pass",
        "failed_count": 0,
        "route_count": 1,
        "checks": [
            {
                "path": "/sign-in",
                "status_code": 200,
                "ok": True,
                "checks": [
                    {"name": name, "ok": True}
                    for name in gold_status.REQUIRED_PUBLIC_AUTH_CHECKS
                ],
            }
        ],
    }
    authenticated_smoke = {
        "generated_at": generated_at,
        "status": "pass",
        "failed_count": 0,
        "route_count": 2,
        "checks": [
            {
                "path": "/app/billing",
                "status_code": 303,
                "ok": True,
                "checks": [
                    {"name": "billing_local_board_deleted", "ok": True},
                    {"name": "billing_external_handoff", "ok": True},
                    {"name": "billing_external_handoff_resolves", "ok": True},
                    {"name": "billing_external_handoff_usable", "ok": True},
                    {"name": "billing_no_second_login", "ok": True},
                ],
            },
            {
                "path": "/app/account",
                "status_code": 200,
                "ok": True,
                "checks": [
                    {"name": name, "ok": True}
                    for name in gold_status.REQUIRED_ACCOUNT_NOTIFICATION_CHECKS
                ],
            },
        ],
    }
    billing = {
        "generated_at": generated_at,
        "status": "pass",
        "billing_handoff": {
            "configured": True,
            "host": "billing.propertyquarry.com",
            "host_resolves": True,
            "account_handoff_usable": True,
            "url": "https://billing.propertyquarry.com/account",
            "pricing_surface_probe": {"placeholder": False},
        },
    }
    browser_3d = _browser_3d_gate_payload()
    browser_3d["generated_at"] = generated_at
    walkthrough_quality = _walkthrough_quality_gate_payload()
    walkthrough_quality["generated_at"] = generated_at
    walkthrough_provider_proof = _walkthrough_provider_proof_payload()
    walkthrough_provider_proof["generated_at"] = generated_at
    map_preview = {
        "generated_at": generated_at,
        "status": "pass",
        "failed_count": 0,
        "preview_count": 1,
        "checks": [{"name": "flagship_map_preview", "ok": True}],
        "preview_results": [{"status": "pass", "placeholder": False}],
    }
    image_digest = "sha256:" + "b" * 64
    probe_window_end = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    probe_window_start = probe_window_end - timedelta(seconds=60)
    range_window_start = probe_window_end - timedelta(days=30)
    replica_ids = ["propertyquarry-api-1"]
    slo_evidence = {
        "schema": "propertyquarry.slo_evidence_receipt.v2",
        "generated_at": generated_at,
        "mode": "flagship",
        "status": "pass",
        "gate_passed": True,
        "release_commit_sha": release_sha,
        "release_image_digest": image_digest,
        "live_monitoring_contacted": False,
        "probe": {
            "schema": "propertyquarry.metrics_snapshot_bundle.v2",
            "probe_schema": "propertyquarry.metrics_probe_bundle.v2",
            "release_commit_sha": release_sha,
            "release_image_digest": image_digest,
            "window_start": probe_window_start.isoformat(),
            "window_end": probe_window_end.isoformat(),
            "window_seconds": 60.0,
            "replica_count": 1,
            "replica_ids": replica_ids,
            "snapshot_bundle_sha256": "c" * 64,
            "probe_bundle_sha256": "d" * 64,
            "credential_persisted": False,
        },
        "prometheus_range": {
            "schema": gold_status.RANGE_RECEIPT_SCHEMA,
            "producer": "propertyquarry-prometheus-range-capture",
            "window_start": range_window_start.isoformat(),
            "window_end": probe_window_end.isoformat(),
            "window_seconds": 30 * 24 * 60 * 60,
            "authenticated": True,
            "tls_verified": True,
            "credential_persisted": False,
            "replica_ids": replica_ids,
            "range_response_sha256": "e" * 64,
            "receipt_sha256": "f" * 64,
            "slo": {"status": "pass"},
        },
        "promtool": {
            "available": True,
            "version_pinned": True,
            "rule_check_passed": True,
            "config_check_passed": True,
            "injection_test_passed": True,
        },
        "amtool": {
            "available": True,
            "version_pinned": True,
            "routing_check_passed": True,
        },
    }
    return {
        "continuous_ux_receipt_path": _write_json(
            tmp_path / "continuous-ux.json",
            continuous_ux,
        ),
        "live_mobile_receipt_path": _write_json(tmp_path / "live-mobile.json", live_mobile),
        "accessibility_receipt_path": _write_json(tmp_path / "accessibility.json", accessibility),
        "failure_state_receipt_path": _write_json(tmp_path / "failure-states.json", failure_states),
        "activation_to_value_receipt_path": _write_json(
            tmp_path / "activation-to-value.json",
            activation_to_value,
        ),
        "public_smoke_receipt_path": _write_json(tmp_path / "public-smoke.json", public_smoke),
        "authenticated_smoke_receipt_path": _write_json(
            tmp_path / "authenticated-smoke.json",
            authenticated_smoke,
        ),
        "billing_receipt_path": _write_json(tmp_path / "billing.json", billing),
        "browser_3d_gate_receipt_path": _write_json(tmp_path / "browser-3d.json", browser_3d),
        "map_preview_flagship_receipt_path": _write_json(tmp_path / "map-preview.json", map_preview),
        "walkthrough_quality_receipt_path": _write_json(
            tmp_path / "walkthrough-quality.json",
            walkthrough_quality,
        ),
        "walkthrough_provider_proof_receipt_path": _write_json(
            tmp_path / "walkthrough-provider-proof.json",
            walkthrough_provider_proof,
        ),
        "slo_evidence_receipt_path": _write_json(
            tmp_path / "slo-evidence.json",
            slo_evidence,
        ),
        "expected_release_commit_sha": release_sha,
        "expected_release_image_digest": image_digest,
        "expected_release_deployment_id": _PERFORMANCE_RELEASE_DEPLOYMENT_ID,
        "expected_release_manifest_sha256": _PERFORMANCE_MANIFEST_SHA256,
        "expected_public_origin": "https://propertyquarry.com",
    }


def _launch_product_data_receipt_args(tmp_path: Path, *, generated_at: str) -> dict[str, object]:
    candidate_sha = "a" * 40
    registry = json.loads(overlay_read_model.REGISTRY_PATH.read_text(encoding="utf-8"))
    layers = [dict(row) for row in registry["layers"]]
    table_names = sorted(str(row["teable_table"]) for row in layers)
    layer_keys = sorted(str(row["layer_key"]) for row in layers)
    table_counts = {name: 1 for name in table_names}
    source_tables = {
        name: {
            "table_id_sha256": f"{index + 1:064x}",
            "record_count": 1,
            "page_count": 1,
            "pages": [
                {
                    "status_code": 200,
                    "response_sha256": f"{index + 101:064x}",
                    "size_bytes": 128,
                }
            ],
        }
        for index, name in enumerate(table_names)
    }
    source_temporal_by_layer: dict[str, dict[str, object]] = {}
    for layer in layers:
        layer_key = str(layer["layer_key"])
        temporalities = set(layer["allowed_source_temporalities"])
        if "current_feed" in temporalities:
            temporality = "current_feed"
        elif "live" in temporalities:
            temporality = "live"
        else:
            temporality = "reference"
        row_counts = {temporality: 1}
        source_age_policy = dict(
            layer.get("source_max_age_hours_by_temporality") or {}
        )
        sla_temporalities = {temporality} & set(source_age_policy)
        source_temporal_by_layer[layer_key] = {
            "cadence_class": layer["source_cadence_class"],
            "allowed_temporalities": list(layer["allowed_source_temporalities"]),
            "source_max_age_hours_by_temporality": source_age_policy,
            "source_sla_timestamp_field_by_temporality": dict(
                layer.get("source_sla_timestamp_field_by_temporality") or {}
            ),
            "row_counts_by_temporality": row_counts,
            "source_updated_at_row_counts_by_temporality": row_counts,
            "oldest_source_updated_at_by_temporality": {
                temporality: generated_at
            },
            "latest_source_updated_at_by_temporality": {
                temporality: generated_at
            },
            "source_sla_at_row_counts_by_temporality": {
                value: 1 for value in sla_temporalities
            },
            "oldest_source_sla_at_by_temporality": {
                value: generated_at for value in sla_temporalities
            },
            "latest_source_sla_at_by_temporality": {
                value: generated_at for value in sla_temporalities
            },
            "reference_periods": ["2025"] if temporality == "reference" else [],
            "reference_period_row_counts": (
                {"2025": 1} if temporality == "reference" else {}
            ),
        }
    overlay_receipt: dict[str, object] = {
        "schema": overlay_read_model.RECEIPT_SCHEMA,
        "status": "pass",
        "generated_at": generated_at,
        "candidate_sha": candidate_sha,
        "snapshot_id": "1" * 64,
        "source_schema": overlay_read_model.EXPORT_SCHEMA,
        "source_generated_at": generated_at,
        "source_payload_sha256": "2" * 64,
        "registry_payload_sha256": overlay_read_model._sha256(registry),
        "source_evidence": {
            "mode": "authenticated_teable_api",
            "auth_kind": "bearer_api_key",
            "secret_in_export": False,
            "base_origin": "https://app.teable.io",
            "base_id_sha256": "3" * 64,
            "redirects_followed": False,
            "table_discovery": {
                "status_code": 200,
                "response_sha256": "4" * 64,
                "size_bytes": 256,
            },
            "tables": source_tables,
        },
        "source_authority": {
            "bound_independently": True,
            "expected_origin": "https://app.teable.io",
            "expected_base_id_sha256": "3" * 64,
        },
        "ingestion": {
            "source": "authenticated_teable_api_export",
            "target": "postgres_cached_geo_rollup",
            "mode": "launch_authenticated_fetch",
            "transaction": "staged_validate_benchmark_atomic_pointer_switch",
            "table_count": 8,
            "layer_count": 8,
            "record_count": 8,
            "table_counts": table_counts,
            "layer_keys": layer_keys,
            "table_names": table_names,
        },
        "temporal_evidence": {
            "cache_max_age_policy_hours": 48.0,
            "oldest_cache_updated_at_by_layer": {
                key: generated_at for key in layer_keys
            },
            "latest_cache_updated_at_by_layer": {
                key: generated_at for key in layer_keys
            },
            "source_by_layer": source_temporal_by_layer,
            "cache_updated_at_proves_source_freshness": False,
        },
        "activation": {
            "phase": "staged",
            "candidate_snapshot_id": "1" * 64,
            "candidate_staged": True,
            "previous_active_snapshot_id": "f" * 64,
            "activated_snapshot_id": "",
            "activation_performed": False,
            "active_snapshot_unchanged": True,
            "active_snapshot_preserved_on_failure": True,
            "active_revalidation_performed": False,
            "active_revalidation_query_sample_count": 0,
            "active_pointer_switch": "atomic_final_transaction",
        },
        "freshness": {
            "max_age_policy_hours": 48.0,
            "latest_cache_by_layer": {key: generated_at for key in layer_keys},
        },
        "read_model": {
            "source_fetch_during_search": False,
            "lookup_policy": "indexed_postgres_cached_rollup_only",
            "coverage": [
                {
                    "layer_key": str(row["layer_key"]),
                    "teable_table": str(row["teable_table"]),
                    "record_count": 1,
                    "latest_cache_updated_at": generated_at,
                    "latest_ingested_at": generated_at,
                }
                for row in layers
            ],
            "sample_layer_count": 8,
            "query_sample_count": 24,
            "query_p95_ms": 4.0,
            "query_budget_ms": 100.0,
        },
        "privacy": {
            "area_context_only": True,
            "property_scoring": False,
            "person_scoring": False,
            "raw_article_bodies_stored": False,
            "match_key_allowlist": sorted(overlay_read_model.ALLOWED_MATCH_KEYS),
        },
        "claim_safety": {
            "aggregate_safety_context_only": True,
            "safety_source_rights_caveat_required": True,
            "municipal_rss_is_independent_press": False,
        },
        "failures": [],
    }

    public_origin = "https://propertyquarry.com"
    analytics_origin = "https://app.rybbit.io"
    site_id = "propertyquarry-production"
    browser = {
        "script": {
            "url": f"{analytics_origin}/api/script.js",
            "status_code": 200,
            "sha256": "5" * 64,
            "size_bytes": 42_000,
            "site_id_bound": True,
        },
        "collector": {
            "url_origin": analytics_origin,
            "url_path": "/api/track",
            "url_sha256": "6" * 64,
            "method": "POST",
            "status_code": 204,
            "response_sha256": "7" * 64,
            "size_bytes": 0,
            "request_payload_sha256": "b" * 64,
            "request_payload_size_bytes": 128,
            "event_name_bound": True,
            "observed_at": generated_at,
        },
        "event": {
            "name": rybbit_evidence.PROBE_EVENT_NAME,
            "sent_at": generated_at,
            "anonymous": True,
            "attribute_count": 0,
        },
        "privacy": {check: True for check in rybbit_evidence.REQUIRED_PRIVACY_CHECKS},
    }
    def api_response_provenance(url_sha256: str) -> dict[str, object]:
        return {
            "response_size_bytes": 128,
            "response_limit_bytes": rybbit_evidence.MAX_RYBBIT_API_RESPONSE_BYTES,
            "content_type": "application/json",
            "requested_url_origin": analytics_origin,
            "final_url_origin": analytics_origin,
            "requested_url_sha256": url_sha256,
            "final_url_sha256": url_sha256,
            "same_request_url": True,
            "redirected": False,
        }

    api = {
        "auth": {"kind": "bearer_api_key", "secret_in_receipt": False},
        "site": {
            "status_code": 200,
            "response_sha256": "8" * 64,
            **api_response_provenance("c" * 64),
            "site_id_bound": True,
        },
        "has_data": {
            "status_code": 200,
            "response_sha256": "9" * 64,
            **api_response_provenance("d" * 64),
            "has_data": True,
        },
        "events": {
            "status_code": 200,
            "response_sha256": "a" * 64,
            **api_response_provenance("e" * 64),
            "event_name": rybbit_evidence.PROBE_EVENT_NAME,
            "event_count": 1,
            "last_seen_at": generated_at,
            "observed_after_probe": True,
        },
    }
    rybbit_receipt = rybbit_evidence.build_receipt(
        candidate_sha=candidate_sha,
        public_origin=public_origin,
        analytics_origin=analytics_origin,
        site_id=site_id,
        browser=browser,
        api=api,
        generated_at=datetime.fromisoformat(generated_at),
    )
    image_digest = "sha256:" + "b" * 64
    release_identity = {
        "commit_sha": candidate_sha,
        "image_digest": image_digest,
    }
    launch_markets = list(gold_status.ACTIVE_PROVIDER_MATRIX_COUNTRY_CODES)
    global_market_envelope_receipt = {
        "schema": gold_status.GLOBAL_MARKET_ENVELOPE_RECEIPT_SCHEMA,
        "source_schema": "propertyquarry.global_market_envelope.v1",
        "source_envelope_id": "propertyquarry-global-market-envelope-test",
        "source_sha256": "d" * 64,
        "status": "READY",
        "generated_at": generated_at,
        "release_identity": dict(release_identity),
        "independently_attested": True,
        "live_receipt_ref": "/governed/global-market/live-attestation.json",
        "live_receipt_age_seconds": 300.0,
        "phase_one": {
            "operating_mode": "launch",
            "content_language": "en",
        },
        "summary": {
            "launch_supported_markets": launch_markets,
            "classifications": {
                "launch_supported": launch_markets,
                "private_beta": [],
                "preview": [],
                "catalog": [],
                "browser_state_only": [],
            },
            "market_count": len(launch_markets),
            "blocker_count": 0,
        },
        "markets": [
            {
                "country_code": country_code,
                "declared_classification": "launch_supported",
                "computed_classification": "launch_supported",
                "classification_match": True,
                "launch_supported": True,
                "status": "READY",
                "missing_dimensions": [],
            }
            for country_code in launch_markets
        ],
        "blockers": [],
    }
    incident_support_receipt = {
        "schema": gold_status.INCIDENT_SUPPORT_GATE_RECEIPT_SCHEMA,
        "status": "pass",
        "generated_at": generated_at,
        "source_contract": {
            "status": "pass",
            "sha256": "sha256:" + "c" * 64,
        },
        "live_receipt_path": "/governed/incident-support/live-attestation.json",
        "release_identity": release_identity,
        "required_markets": launch_markets,
        "live_receipt_age_seconds": 300.0,
        "blockers": [],
    }
    market_locales = {"AT": "de-AT", "DE": "de-DE", "CR": "es-CR"}
    global_experience_receipt = {
        "schema": gold_status.GLOBAL_EXPERIENCE_GATE_RECEIPT_SCHEMA,
        "status": "pass",
        "generated_at": generated_at,
        "service": "propertyquarry",
        "profile": "launch",
        "claim_scope": "core",
        "source_contract_status": "defined_not_live_evidence",
        "contract_sha256": "e" * 64,
        "live_receipt_path": "/governed/global-experience/live-attestation.json",
        "live_receipt_age_seconds": 300.0,
        "maximum_age_hours": 1.0,
        "release_identity": release_identity,
        "required_markets": [
            {"country_code": country_code, "locale": locale}
            for country_code, locale in market_locales.items()
        ],
        "market_results": [
            {
                "country_code": country_code,
                "locale": locale,
                "status": "pass",
                "blockers": [],
            }
            for country_code, locale in market_locales.items()
        ],
        "independently_attested": True,
        "blockers": [],
    }
    jurisdiction_contract_path = (
        ROOT / gold_status.JURISDICTION_PRIVACY_RIGHTS_CONTRACT_PATH
    )
    jurisdiction_envelope_path = (
        ROOT / gold_status.JURISDICTION_PRIVACY_RIGHTS_MARKET_ENVELOPE_PATH
    )
    jurisdiction_privacy_rights_receipt = {
        "schema": gold_status.JURISDICTION_PRIVACY_RIGHTS_GATE_RECEIPT_SCHEMA,
        "status": "pass",
        "generated_at": generated_at,
        "source_contract": {
            "path": gold_status.JURISDICTION_PRIVACY_RIGHTS_CONTRACT_PATH.as_posix(),
            "sha256": "sha256:"
            + hashlib.sha256(jurisdiction_contract_path.read_bytes()).hexdigest(),
            "status": "pass",
        },
        "market_envelope": {
            "path": gold_status.JURISDICTION_PRIVACY_RIGHTS_MARKET_ENVELOPE_PATH.as_posix(),
            "sha256": "sha256:"
            + hashlib.sha256(jurisdiction_envelope_path.read_bytes()).hexdigest(),
            "status": "pass",
        },
        "live_receipt_path": "/governed/compliance/jurisdiction-rights-attestation.json",
        "release_identity": release_identity,
        "required_markets": launch_markets,
        "live_receipt_age_seconds": 300.0,
        "blockers": [],
    }
    return {
        "evidence_overlay_receipt_path": _write_json(
            tmp_path / "evidence-overlay-read-model.json", overlay_receipt
        ),
        "rybbit_evidence_receipt_path": _write_json(
            tmp_path / "rybbit-delivery.json", rybbit_receipt
        ),
        "global_market_envelope_receipt_path": _write_json(
            tmp_path / "global-market-envelope.json",
            global_market_envelope_receipt,
        ),
        "incident_support_receipt_path": _write_json(
            tmp_path / "incident-support-gate.json",
            incident_support_receipt,
        ),
        "global_experience_receipt_path": _write_json(
            tmp_path / "global-experience-gate.json",
            global_experience_receipt,
        ),
        "jurisdiction_privacy_rights_receipt_path": _write_json(
            tmp_path / "jurisdiction-privacy-rights-gate.json",
            jurisdiction_privacy_rights_receipt,
        ),
        "expected_teable_origin": "https://app.teable.io",
        "expected_teable_base_id_sha256": "3" * 64,
        "expected_evidence_overlay_phase": "staged",
        "expected_rybbit_origin": analytics_origin,
        "expected_rybbit_site_id_sha256": rybbit_evidence._sha256_text(site_id),
    }


def test_gold_status_standard_profile_keeps_customer_ux_receipts_optional(tmp_path: Path) -> None:
    receipt = build_gold_status_receipt(**_minimal_gold_receipt_args(tmp_path, generated_at="2026-07-13T10:00:00+00:00"))

    assert receipt["status"] == "pass"
    assert receipt["readiness_profile"] == "standard"
    assert receipt["flagship_customer_ux_evidence"] == {
        "required": False,
        "ready": None,
        "required_receipts": [
            area
            for area in gold_status.FLAGSHIP_CUSTOMER_UX_RECEIPT_AREAS
            if area != "walkthrough_quality"
        ],
        "missing_receipts": [],
        "research_detail_required": False,
        "browser_all_mobile_proof_required": False,
        "browser_all_mobile_proof_ready": None,
        "continuous_ux_proof_required": False,
        "continuous_ux_proof_ready": None,
        "accessibility_proof_required": False,
        "accessibility_proof_ready": None,
        "activation_to_value_proof_required": False,
        "activation_to_value_proof_ready": None,
        "required_browser_engines": [],
        "live_mobile_billing_available": None,
        "authenticated_billing_available": None,
        "max_receipt_age_hours": None,
    }


def test_gold_status_standard_profile_keeps_legacy_performance_compatibility(
    tmp_path: Path,
) -> None:
    generated_at = "2026-07-13T10:00:00+00:00"
    args = _minimal_gold_receipt_args(tmp_path, generated_at=generated_at)
    legacy = _performance_payload()
    for field in (
        "schema",
        "flagship_status",
        "flagship_blockers",
        "server_request_evidence",
        "constrained_client_evidence",
        "claims",
    ):
        legacy.pop(field)
    legacy["generated_at"] = generated_at
    args["performance_receipt_path"] = _write_json(
        tmp_path / "legacy-performance.json",
        legacy,
    )

    receipt = build_gold_status_receipt(**args)

    assert receipt["status"] == "pass"
    assert receipt["performance"]["flagship_proof_required"] is False
    assert receipt["performance"]["flagship_proof_ok"] is None
    assert "schema_must_be_authenticated_performance_v2" in receipt[
        "performance"
    ]["flagship_proof"]["errors"]


def test_gold_compatibility_aliases_are_strict_launch_tier_claims(
    tmp_path: Path,
) -> None:
    args = _minimal_gold_receipt_args(
        tmp_path,
        generated_at="2026-07-19T06:00:00+00:00",
    )
    tour_path = args["tour_control_receipt_path"]
    assert isinstance(tour_path, Path)
    _write_json(
        tour_path,
        {
            "generated_at": "2026-07-19T06:00:00+00:00",
            "status": "blocked_missing_provider_modes",
            "provider_counts": {"matterport": 1, "magicfit": 0},
            "ready_provider_modes": ["matterport"],
            "core_required_provider_modes": ["matterport"],
            "advanced_visual_required_provider_modes": ["magicfit"],
            "core_missing_provider_modes": [],
            "advanced_visual_missing_provider_modes": ["magicfit"],
            "operator_missing_provider_modes": ["magicfit"],
            "required_provider_modes": ["matterport", "magicfit"],
            "missing_provider_modes": ["magicfit"],
            "magicfit_playback": {
                "playback_ok": True,
                "playable_count": 0,
                "ready_count": 0,
            },
        },
    )

    core_receipt = build_gold_status_receipt(
        **args,
        readiness_profile="core_gold",
    )
    advanced_receipt = build_gold_status_receipt(
        **args,
        readiness_profile="advanced_visual_gold",
    )

    assert core_receipt["status"] == "blocked"
    assert core_receipt["readiness_profile"] == "launch"
    assert core_receipt["requested_readiness_profile"] == "core_gold"
    assert core_receipt["evidence_tier"] == "launch"
    assert core_receipt["claim_scope"] == "core"
    assert core_receipt["core_gold_status"] == "blocked"
    assert core_receipt["core_missing_provider_modes"] == []
    assert core_receipt["advanced_visual_gold_status"] == "unavailable"
    assert core_receipt["advanced_visual_missing_provider_modes"] == [
        "magicfit",
        "magic",
        "omagic",
    ]
    core_blocker_areas = {row["area"] for row in core_receipt["blockers"]}
    assert "flagship_customer_ux_evidence" in core_blocker_areas
    assert "evidence_overlay_read_model" in core_blocker_areas
    assert "rybbit_delivery" in core_blocker_areas
    assert "advanced_visual_provider_modes" not in core_blocker_areas
    assert any(
        row["area"] == "advanced_visual_provider_modes"
        for row in core_receipt["operator_blockers"]
    )
    assert advanced_receipt["status"] == "blocked"
    assert advanced_receipt["readiness_profile"] == "launch"
    assert advanced_receipt["requested_readiness_profile"] == (
        "advanced_visual_gold"
    )
    assert advanced_receipt["evidence_tier"] == "launch"
    assert advanced_receipt["claim_scope"] == "advanced_visual"
    assert advanced_receipt["advanced_visual_gold"]["status"] == "unavailable"
    assert any(
        row["area"] == "advanced_visual_provider_modes"
        for row in advanced_receipt["blockers"]
    )


def test_gold_status_standard_profile_does_not_gate_on_optional_accessibility_receipt(tmp_path: Path) -> None:
    generated_at = "2026-07-13T10:00:00+00:00"
    accessibility_path = _write_json(
        tmp_path / "accessibility-fail.json",
        {"generated_at": generated_at, "status": "fail", "failed_count": 4, "routes": []},
    )

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        accessibility_receipt_path=accessibility_path,
    )

    assert receipt["status"] == "pass"
    assert receipt["accessibility"]["status"] == "fail"
    assert receipt["accessibility"]["flagship_proof_ok"] is None


def test_gold_status_standard_profile_does_not_gate_on_optional_activation_receipt(tmp_path: Path) -> None:
    generated_at = "2026-07-13T10:00:00+00:00"
    activation_path = _write_json(
        tmp_path / "activation-fail.json",
        {"generated_at": generated_at, "status": "fail", "failed_count": 1, "steps": []},
    )

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        activation_to_value_receipt_path=activation_path,
    )

    assert receipt["status"] == "pass"
    assert receipt["activation_to_value"]["status"] == "fail"
    assert receipt["activation_to_value"]["flagship_proof_ok"] is None


def test_gold_status_standard_profile_does_not_gate_on_optional_failure_state_receipt(tmp_path: Path) -> None:
    generated_at = "2026-07-13T10:00:00+00:00"
    failure_path = _write_json(
        tmp_path / "failure-states-fail.json",
        {"generated_at": generated_at, "status": "fail", "failed_count": 2, "rows": []},
    )

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        failure_state_receipt_path=failure_path,
    )

    assert receipt["status"] == "pass"
    assert receipt["failure_states"]["status"] == "fail"
    assert receipt["failure_states"]["flagship_proof_ok"] is None


def test_gold_status_standard_profile_keeps_safe_billing_recovery_compatible_but_not_available(tmp_path: Path) -> None:
    generated_at = "2026-07-13T10:00:00+00:00"
    authenticated_smoke = _authenticated_smoke_payload(
        billing_external=False,
        billing_fail_closed=True,
    )
    authenticated_smoke["generated_at"] = generated_at

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        authenticated_smoke_receipt_path=_write_json(
            tmp_path / "authenticated-smoke.json",
            authenticated_smoke,
        ),
    )

    assert receipt["status"] == "pass"
    assert receipt["authenticated_customer_surfaces"]["billing_checks_ok"] is True
    assert receipt["authenticated_customer_surfaces"]["billing_availability"]["state"] == "unavailable"
    assert receipt["billing_handoff"]["status"] == "not_checked_compatible"
    assert receipt["billing_handoff"]["compatibility_ok"] is True
    assert receipt["billing_handoff"]["ready"] is False


def test_gold_status_flagship_requires_live_no_second_login_billing_not_recovery_or_account_fallback(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    cases = {
        "fail_closed": _authenticated_smoke_payload(
            billing_external=False,
            billing_fail_closed=True,
        ),
        "account_fallback": _authenticated_smoke_payload(
            billing_external=False,
            billing_fail_closed=False,
            billing_bridge_launch=True,
            billing_internal_account_fallback=True,
        ),
    }
    for case_name, authenticated_smoke in cases.items():
        case_dir = tmp_path / case_name
        authenticated_smoke["generated_at"] = generated_at
        flagship_args = _flagship_customer_ux_receipt_args(case_dir, generated_at=generated_at)
        flagship_args["authenticated_smoke_receipt_path"] = _write_json(
            case_dir / "authenticated-smoke-recovery.json",
            authenticated_smoke,
        )

        receipt = build_gold_status_receipt(
            **_minimal_gold_receipt_args(case_dir, generated_at=generated_at),
            **flagship_args,
            readiness_profile="flagship",
            max_receipt_age_hours=1,
            now=now,
        )

        assert receipt["status"] == "blocked"
        assert receipt["authenticated_customer_surfaces"]["billing_availability"]["available"] is False
        assert "billing_available_no_second_login_handoff" in receipt["authenticated_customer_surfaces"]["missing_billing_checks"]
        assert receipt["billing_handoff"]["strict_live_proof_required"] is True
        assert receipt["billing_handoff"]["ready"] is False
        assert any(row["area"] == "authenticated_customer_surfaces" for row in receipt["blockers"])
        assert any(row["area"] == "billing_handoff" for row in receipt["blockers"])


def test_gold_status_treats_intentionally_disabled_billing_as_named_launch_blocker(tmp_path: Path) -> None:
    generated_at = "2026-07-13T10:00:00+00:00"
    billing_payload = _billing_payload(host_resolves=True, status="disabled")
    billing_payload["billing_handoff"]["account_handoff_usable"] = True
    billing_payload["billing_handoff"]["pricing_surface_probe"] = {"placeholder": False}

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        billing_receipt_path=_write_json(tmp_path / "billing-disabled.json", billing_payload),
    )

    assert receipt["status"] == "blocked"
    assert receipt["billing_handoff"]["status"] == "disabled"
    assert receipt["billing_handoff"]["provider_disabled"] is True
    assert receipt["billing_handoff"]["ready"] is False
    blocker = next(row for row in receipt["blockers"] if row["area"] == "billing_handoff")
    assert blocker["launch_blocker_reason"] == "billing_intentionally_disabled"
    assert "enable billing" in blocker["action"]


def test_gold_status_launch_profile_blocks_missing_customer_and_product_data_receipts(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=(now - timedelta(minutes=5)).isoformat()),
        readiness_profile="launch",
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["readiness_profile"] == "launch"
    assert receipt["flagship_customer_ux_evidence"]["ready"] is False
    assert receipt["flagship_customer_ux_evidence"]["missing_receipts"] == list(
        area
        for area in gold_status.FLAGSHIP_CUSTOMER_UX_RECEIPT_AREAS
        if area != "walkthrough_quality"
    )
    assert receipt["flagship_customer_ux_evidence"]["max_receipt_age_hours"] == 24.0
    blocker = next(row for row in receipt["blockers"] if row["area"] == "flagship_customer_ux_evidence")
    assert blocker["status"] == "missing_required_receipts"
    assert blocker["missing_receipts"] == [
        area
        for area in gold_status.FLAGSHIP_CUSTOMER_UX_RECEIPT_AREAS
        if area != "walkthrough_quality"
    ]
    assert receipt["launch_product_data_evidence"]["required"] is True
    assert receipt["launch_product_data_evidence"]["ready"] is False
    blocker_areas = {str(row["area"]) for row in receipt["blockers"]}
    assert {
        "evidence_overlay_read_model",
        "rybbit_delivery",
        "global_market_envelope",
        "incident_support",
        "global_experience",
        "jurisdiction_privacy_rights",
    }.issubset(blocker_areas)
    assert receipt["global_market_envelope"]["required"] is True
    assert receipt["global_market_envelope"]["ready"] is False
    assert receipt["incident_support"]["required"] is True
    assert receipt["incident_support"]["ready"] is False
    assert receipt["global_experience"]["required"] is True
    assert receipt["global_experience"]["ready"] is False
    assert receipt["jurisdiction_privacy_rights"]["required"] is True
    assert receipt["jurisdiction_privacy_rights"]["ready"] is False


def test_gold_status_flagship_profile_passes_with_fresh_customer_ux_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **_flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at),
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "pass"
    assert receipt["readiness_profile"] == "flagship"
    assert receipt["flagship_customer_ux_evidence"]["ready"] is True
    assert receipt["flagship_customer_ux_evidence"]["missing_receipts"] == []
    assert receipt["flagship_customer_ux_evidence"]["browser_all_mobile_proof_ready"] is True
    assert receipt["flagship_customer_ux_evidence"]["continuous_ux_proof_ready"] is True
    assert receipt["flagship_customer_ux_evidence"]["accessibility_proof_ready"] is True
    assert receipt["flagship_customer_ux_evidence"]["activation_to_value_proof_ready"] is True
    assert receipt["accessibility"]["flagship_proof_ok"] is True
    assert receipt["activation_to_value"]["flagship_proof_ok"] is True
    assert receipt["live_mobile_surfaces"]["missing_detail_routes"] == []
    assert receipt["slo_evidence"]["status"] == "pass"
    assert receipt["slo_evidence"]["required"] is True
    assert receipt["slo_evidence"]["age_seconds"] == 300.0
    assert any(row["area"] == "slo_evidence" for row in receipt["pass_areas"])
    assert receipt["live_mobile_surfaces"]["flagship_browser_proof_ok"] is True
    assert receipt["authenticated_customer_surfaces"]["billing_checks_ok"] is True
    assert receipt["billing_handoff"]["ready"] is True
    assert receipt["browser_rendered_3d"]["ready"] is True
    assert receipt["map_preview_flagship"]["ready"] is True
    assert receipt["walkthrough_quality"]["ready"] is None
    assert receipt["continuous_ux"]["flagship_proof_ok"] is True
    assert receipt["continuous_ux"]["supplemental_only"] is True
    assert receipt["continuous_ux"]["production_claim"] is False
    assert receipt["performance"]["flagship_proof_required"] is True
    assert receipt["performance"]["flagship_proof_ok"] is True
    assert receipt["performance"]["flagship_proof"]["errors"] == []
    assert receipt["global_market_envelope"]["required"] is False
    assert receipt["global_market_envelope"]["ready"] is None
    assert receipt["incident_support"]["required"] is False
    assert receipt["incident_support"]["ready"] is None
    assert receipt["global_experience"]["required"] is False
    assert receipt["global_experience"]["ready"] is None
    assert receipt["jurisdiction_privacy_rights"]["required"] is False
    assert receipt["jurisdiction_privacy_rights"]["ready"] is None


def test_gold_status_flagship_rejects_legacy_status_pass_without_v2_proof(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    args = _minimal_gold_receipt_args(tmp_path, generated_at=generated_at)
    legacy = _performance_payload()
    for field in (
        "schema",
        "flagship_status",
        "flagship_blockers",
        "server_request_evidence",
        "constrained_client_evidence",
        "claims",
    ):
        legacy.pop(field)
    legacy["generated_at"] = generated_at
    args["performance_receipt_path"] = _write_json(
        tmp_path / "legacy-performance-flagship.json",
        legacy,
    )

    receipt = build_gold_status_receipt(
        **args,
        **_flagship_customer_ux_receipt_args(
            tmp_path,
            generated_at=generated_at,
        ),
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["performance"]["status"] == "pass"
    assert receipt["performance"]["flagship_proof_ok"] is False
    blocker = next(
        row
        for row in receipt["blockers"]
        if row["area"] == "mobile_and_authenticated_surfaces"
    )
    assert blocker["status"] == "blocked"
    assert blocker["receipt_status"] == "pass"
    assert "schema_must_be_authenticated_performance_v2" in blocker[
        "flagship_proof"
    ]["errors"]


def test_gold_status_flagship_activation_proof_rejects_another_release_candidate(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    activation_path = Path(flagship_args["activation_to_value_receipt_path"])
    activation_receipt = json.loads(activation_path.read_text(encoding="utf-8"))
    activation_receipt["release_commit_sha"] = "b" * 40
    _write_json(activation_path, activation_receipt)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["flagship_customer_ux_evidence"]["activation_to_value_proof_ready"] is False
    proof = receipt["activation_to_value"]["flagship_proof"]
    assert proof["expected_release_commit_sha"] == "a" * 40
    assert proof["reported_release_commit_sha"] == "b" * 40
    assert proof["release_commit_sha_matches"] is False
    blocker = next(row for row in receipt["blockers"] if row["area"] == "activation_to_value")
    assert blocker["proof"]["release_commit_sha_matches"] is False


def test_gold_status_launch_profile_requires_and_consumes_real_product_data_receipts(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    launch_product_data_args = _launch_product_data_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **_flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at),
        **launch_product_data_args,
        readiness_profile="launch",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "pass"
    assert receipt["readiness_profile"] == "launch"
    assert receipt["launch_product_data_evidence"]["ready"] is True
    assert receipt["launch_product_data_evidence"]["evidence_overlay_read_model"]["source"] == (
        "authenticated_teable_api_export"
    )
    assert receipt["launch_product_data_evidence"]["evidence_overlay_read_model"]["layer_count"] == 8
    overlay_path = Path(launch_product_data_args["evidence_overlay_receipt_path"])
    overlay_sha256 = hashlib.sha256(overlay_path.read_bytes()).hexdigest()
    assert (
        receipt["launch_product_data_evidence"]["evidence_overlay_read_model"][
            "receipt_sha256"
        ]
        == overlay_sha256
    )
    assert receipt["launch_product_data_evidence"]["rybbit_delivery"]["collector_status_code"] == 204
    assert receipt["global_market_envelope"]["required"] is True
    assert receipt["global_market_envelope"]["ready"] is True
    assert receipt["global_market_envelope"]["launch_supported_markets"] == [
        "AT",
        "DE",
        "CR",
    ]
    assert receipt["incident_support"]["required"] is True
    assert receipt["incident_support"]["ready"] is True
    assert receipt["incident_support"]["required_markets"] == ["AT", "DE", "CR"]
    assert receipt["global_experience"]["required"] is True
    assert receipt["global_experience"]["ready"] is True
    assert {
        row["country_code"]
        for row in receipt["global_experience"]["required_markets"]
    } == {"AT", "DE", "CR"}
    assert receipt["jurisdiction_privacy_rights"]["required"] is True
    assert receipt["jurisdiction_privacy_rights"]["ready"] is True
    assert receipt["jurisdiction_privacy_rights"]["required_markets"] == [
        "AT",
        "DE",
        "CR",
    ]
    pass_areas = {str(row["area"]): row for row in receipt["pass_areas"]}
    assert {
        "evidence_overlay_read_model",
        "rybbit_delivery",
        "global_market_envelope",
        "incident_support",
        "global_experience",
        "jurisdiction_privacy_rights",
    }.issubset(pass_areas)
    assert pass_areas["evidence_overlay_read_model"]["receipt_sha256"] == overlay_sha256


@pytest.mark.parametrize(
    ("receipt_key", "identity_field", "invalid_value", "blocker_area"),
    (
        (
            "global_market_envelope_receipt_path",
            "image_digest",
            "sha256:" + "f" * 64,
            "global_market_envelope",
        ),
        (
            "incident_support_receipt_path",
            "commit_sha",
            "f" * 40,
            "incident_support",
        ),
        (
            "global_experience_receipt_path",
            "image_digest",
            "sha256:" + "e" * 64,
            "global_experience",
        ),
        (
            "jurisdiction_privacy_rights_receipt_path",
            "commit_sha",
            "e" * 40,
            "jurisdiction_privacy_rights",
        ),
    ),
)
def test_gold_status_launch_core_rejects_governance_receipt_for_another_release(
    tmp_path: Path,
    receipt_key: str,
    identity_field: str,
    invalid_value: str,
    blocker_area: str,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    launch_args = _launch_product_data_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    receipt_path = Path(launch_args[receipt_key])
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["release_identity"][identity_field] = invalid_value
    _write_json(receipt_path, payload)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **_flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at),
        **launch_args,
        readiness_profile="launch",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    blocker = next(
        row for row in receipt["blockers"] if row["area"] == blocker_area
    )
    assert any("does not match the Gold release" in error for error in blocker["errors"])


@pytest.mark.parametrize(
    ("section", "expected_error"),
    (
        ("source_contract", "current source contract"),
        ("market_envelope", "current global market envelope"),
    ),
)
def test_gold_status_launch_core_rejects_stale_jurisdiction_authority_digest(
    tmp_path: Path,
    section: str,
    expected_error: str,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    launch_args = _launch_product_data_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    receipt_path = Path(launch_args["jurisdiction_privacy_rights_receipt_path"])
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload[section]["sha256"] = "sha256:" + "f" * 64
    _write_json(receipt_path, payload)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **_flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at),
        **launch_args,
        readiness_profile="launch",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    blocker = next(
        row
        for row in receipt["blockers"]
        if row["area"] == "jurisdiction_privacy_rights"
    )
    assert any(expected_error in error for error in blocker["errors"])


def test_gold_status_launch_core_rejects_stale_jurisdiction_attestation(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    launch_args = _launch_product_data_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    receipt_path = Path(launch_args["jurisdiction_privacy_rights_receipt_path"])
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["live_receipt_age_seconds"] = 3601.0
    _write_json(receipt_path, payload)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **_flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at),
        **launch_args,
        readiness_profile="launch",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    blocker = next(
        row
        for row in receipt["blockers"]
        if row["area"] == "jurisdiction_privacy_rights"
    )
    assert any("live attestation is stale" in error for error in blocker["errors"])


def test_gold_status_standard_only_surfaces_optional_governance_receipts(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    invalid_market = _write_json(
        tmp_path / "invalid-market-envelope.json",
        {"schema": "wrong", "status": "BLOCKED", "generated_at": generated_at},
    )
    invalid_incident = _write_json(
        tmp_path / "invalid-incident-support.json",
        {"schema": "wrong", "status": "blocked", "generated_at": generated_at},
    )
    invalid_experience = _write_json(
        tmp_path / "invalid-global-experience.json",
        {"schema": "wrong", "status": "blocked", "generated_at": generated_at},
    )
    invalid_jurisdiction = _write_json(
        tmp_path / "invalid-jurisdiction-rights.json",
        {"schema": "wrong", "status": "blocked", "generated_at": generated_at},
    )

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        global_market_envelope_receipt_path=invalid_market,
        incident_support_receipt_path=invalid_incident,
        global_experience_receipt_path=invalid_experience,
        jurisdiction_privacy_rights_receipt_path=invalid_jurisdiction,
        readiness_profile="standard",
        now=now,
    )

    assert receipt["status"] == "pass"
    assert receipt["global_market_envelope"]["required"] is False
    assert receipt["global_market_envelope"]["ready"] is False
    assert receipt["incident_support"]["required"] is False
    assert receipt["incident_support"]["ready"] is False
    assert receipt["global_experience"]["required"] is False
    assert receipt["global_experience"]["ready"] is False
    assert receipt["jurisdiction_privacy_rights"]["required"] is False
    assert receipt["jurisdiction_privacy_rights"]["ready"] is False
    blocker_areas = {str(row["area"]) for row in receipt["blockers"]}
    assert "global_market_envelope" not in blocker_areas
    assert "incident_support" not in blocker_areas
    assert "global_experience" not in blocker_areas
    assert "jurisdiction_privacy_rights" not in blocker_areas


def test_gold_status_discovers_governance_receipts_only_from_named_patterns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market_path = (
        tmp_path
        / "state"
        / "receipts"
        / "propertyquarry_global_market_envelope_receipt-current.json"
    )
    incident_path = (
        tmp_path
        / "state"
        / "receipts"
        / "propertyquarry_incident_support_gate-current.json"
    )
    experience_path = (
        tmp_path
        / "state"
        / "receipts"
        / "propertyquarry_global_experience_gate-current.json"
    )
    jurisdiction_path = (
        tmp_path
        / "state"
        / "receipts"
        / "propertyquarry_jurisdiction_privacy_rights_gate-current.json"
    )
    market_path.parent.mkdir(parents=True)
    _write_json(market_path, {"schema": gold_status.GLOBAL_MARKET_ENVELOPE_RECEIPT_SCHEMA})
    _write_json(incident_path, {"schema": gold_status.INCIDENT_SUPPORT_GATE_RECEIPT_SCHEMA})
    _write_json(experience_path, {"schema": gold_status.GLOBAL_EXPERIENCE_GATE_RECEIPT_SCHEMA})
    _write_json(
        jurisdiction_path,
        {"schema": gold_status.JURISDICTION_PRIVACY_RIGHTS_GATE_RECEIPT_SCHEMA},
    )
    monkeypatch.chdir(tmp_path)

    assert gold_status._default_receipt_path_if_exists(
        "global_market_envelope"
    ) == market_path.resolve()
    assert gold_status._default_receipt_path_if_exists(
        "incident_support"
    ) == incident_path.resolve()
    assert gold_status._default_receipt_path_if_exists(
        "global_experience"
    ) == experience_path.resolve()
    assert gold_status._default_receipt_path_if_exists(
        "jurisdiction_privacy_rights"
    ) == jurisdiction_path.resolve()


def test_flagship_continuous_ux_accepts_zero_non_gated_browser_diagnostics(
    tmp_path: Path,
) -> None:
    generated_at = "2026-07-13T11:55:00+00:00"
    flagship_args = _flagship_customer_ux_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    continuous_path = Path(flagship_args["continuous_ux_receipt_path"])
    continuous = json.loads(continuous_path.read_text(encoding="utf-8"))
    for row in continuous["rows"]:
        metrics = row["metrics"]
        if row["browser_engine"] == gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_ENGINE:
            assert metrics["first_value_gated"] is True
            assert metrics["first_value_cold_ms"] > 0
            assert metrics["first_value_ms"] > 0
            assert all(value > 0 for value in metrics["first_value_samples_ms"])
            continue
        metrics.update(
            {
                "first_value_cold_ms": 0.0,
                "first_value_ms": 0.0,
                "first_value_samples_ms": [0.0],
                "first_value_initial_samples_ms": [0.0],
            }
        )

    proof_ok, proof = gold_status._flagship_continuous_ux_proof(
        continuous,
        expected_release_commit_sha=continuous["release_commit_sha"],
    )

    assert proof_ok is True
    assert proof["failed_rows"] == []


def test_flagship_continuous_ux_accepts_one_coherent_bounded_retry(
    tmp_path: Path,
) -> None:
    flagship_args = _flagship_customer_ux_receipt_args(
        tmp_path,
        generated_at="2026-07-13T11:55:00+00:00",
    )
    continuous_path = Path(flagship_args["continuous_ux_receipt_path"])
    continuous = json.loads(continuous_path.read_text(encoding="utf-8"))
    for row in continuous["rows"]:
        if row["browser_engine"] != gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_ENGINE:
            continue
        row["metrics"].update(
            {
                "first_value_initial_samples_ms": [3_300.0, 3_400.0, 3_500.0],
                "first_value_retry_used": True,
            }
        )

    proof_ok, proof = gold_status._flagship_continuous_ux_proof(
        continuous,
        expected_release_commit_sha=continuous["release_commit_sha"],
    )

    assert proof_ok is True
    assert proof["failed_rows"] == []


def test_gold_status_flagship_rejects_continuous_ux_production_claim_or_wrong_sha(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    continuous_path = Path(flagship_args["continuous_ux_receipt_path"])
    continuous = json.loads(continuous_path.read_text(encoding="utf-8"))
    continuous["production_claim"] = True
    continuous["release_commit_sha"] = "b" * 40
    _write_json(continuous_path, continuous)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["continuous_ux"]["flagship_proof_ok"] is False
    assert set(receipt["continuous_ux"]["flagship_proof"]["contract_errors"]) >= {
        "production_claim_must_be_false",
        "release_commit_sha_mismatch",
    }
    assert any(row["area"] == "continuous_ux" for row in receipt["blockers"])


def test_gold_status_flagship_rejects_continuous_ux_mock_or_over_budget_evidence(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    continuous_path = Path(flagship_args["continuous_ux_receipt_path"])
    continuous = json.loads(continuous_path.read_text(encoding="utf-8"))
    continuous["proof_mode"] = "contract_mock"
    chromium_row = next(
        row
        for row in continuous["rows"]
        if row["browser_engine"]
        == gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_ENGINE
    )
    chromium_row["metrics"]["first_value_ms"] = (
        gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BUDGET_MS + 1.0
    )
    _write_json(continuous_path, continuous)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    proof = receipt["continuous_ux"]["flagship_proof"]
    assert "proof_mode_not_real_browser" in proof["contract_errors"]
    assert proof["failed_rows"][0]["first_value_ms"] == (
        gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_BUDGET_MS + 1.0
    )


@pytest.mark.parametrize(
    ("route", "target", "field", "value"),
    (
        ("/app/search", "metrics", "loading_action_available", _CONTINUOUS_UX_MISSING),
        ("/app/search", "metrics", "loading_state_visible", False),
        ("/app/search", "metrics", "loading_state_semantic", _CONTINUOUS_UX_MISSING),
        (
            "/app/search?continuous_ux_state=offline",
            "metrics",
            "error_state_visible",
            _CONTINUOUS_UX_MISSING,
        ),
        (
            "/app/search?continuous_ux_state=offline",
            "metrics",
            "error_state_semantic",
            False,
        ),
        (
            "/app/search?continuous_ux_state=offline",
            "metrics",
            "error_state_recovered_online",
            _CONTINUOUS_UX_MISSING,
        ),
        ("/app/search", "row", "status_code", _CONTINUOUS_UX_MISSING),
        ("/app/search", "row", "status_code", 503),
        ("/app/search", "metrics", "document_ready_state", _CONTINUOUS_UX_MISSING),
        ("/app/search", "metrics", "document_ready_state", "loading"),
        ("/app/search", "row", "error", _CONTINUOUS_UX_MISSING),
        ("/app/search", "row", "error", "playwright_timeout"),
        ("/app/search", "metrics", "first_value_retry_used", _CONTINUOUS_UX_MISSING),
        ("/app/search", "metrics", "first_value_retry_used", True),
        ("/app/search", "metrics", "first_value_cold_ms", -1.0),
        ("/app/search", "metrics", "first_value_cold_ms", float("inf")),
        (
            "/app/search",
            "metrics",
            "first_value_initial_samples_ms",
            _CONTINUOUS_UX_MISSING,
        ),
        ("/app/search", "metrics", "first_value_initial_samples_ms", [220.0, 240.0]),
        ("/app/search", "metrics", "zoom_400_viewport_width", _CONTINUOUS_UX_MISSING),
        ("/app/search", "metrics", "zoom_400_viewport_width", 321),
        ("/app/search", "metrics", "zoom_400_scroll_width", _CONTINUOUS_UX_MISSING),
        ("/app/search", "metrics", "horizontal_overflow", _CONTINUOUS_UX_MISSING),
        ("/app/search", "metrics", "horizontal_overflow", True),
        ("/app/search", "metrics", "provider_response_mocked", _CONTINUOUS_UX_MISSING),
        ("/app/search", "metrics", "provider_response_mocked", True),
        ("/app/search", "metrics", "request_interception_mode", _CONTINUOUS_UX_MISSING),
        ("/app/search", "metrics", "route_fulfill_count", _CONTINUOUS_UX_MISSING),
        ("/app/search", "receipt", "provider_response_mocking", _CONTINUOUS_UX_MISSING),
    ),
)
def test_gold_status_rejects_missing_or_tampered_continuous_ux_raw_evidence(
    tmp_path: Path,
    route: str,
    target: str,
    field: str,
    value: object,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    continuous_path = Path(flagship_args["continuous_ux_receipt_path"])
    continuous = json.loads(continuous_path.read_text(encoding="utf-8"))
    row = next(
        candidate
        for candidate in continuous["rows"]
        if candidate["browser_engine"]
        == gold_status.REQUIRED_CONTINUOUS_UX_FIRST_VALUE_ENGINE
        and candidate["route"] == route
    )
    evidence = (
        continuous
        if target == "receipt"
        else row
        if target == "row"
        else row["metrics"]
    )
    if value is _CONTINUOUS_UX_MISSING:
        evidence.pop(field)
    else:
        evidence[field] = value
    _write_json(continuous_path, continuous)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["continuous_ux"]["flagship_proof_ok"] is False


def test_gold_status_blocks_slo_evidence_older_than_fifteen_minutes_even_with_looser_input(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=16)).isoformat()
    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **_flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at),
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        slo_evidence_max_age_seconds=3600,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["slo_evidence"]["max_age_seconds"] == 900
    assert "probe_stale" in receipt["slo_evidence"]["errors"]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "slo_evidence")
    assert blocker["age_seconds"] == 960.0


def test_gold_status_blocks_slo_evidence_bound_to_another_image(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    flagship_args["expected_release_image_digest"] = "sha256:" + "c" * 64

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert "release_image_digest_mismatch" in receipt["slo_evidence"]["errors"]
    assert any(row["area"] == "slo_evidence" for row in receipt["blockers"])


def test_gold_status_flagship_rejects_incomplete_accessibility_engine_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    accessibility_path = Path(flagship_args["accessibility_receipt_path"])
    accessibility = json.loads(accessibility_path.read_text(encoding="utf-8"))
    accessibility["routes"] = [
        row for row in accessibility["routes"]
        if row["browser_engine"] != "webkit"
    ]
    accessibility["route_count"] = len(accessibility["routes"])
    _write_json(accessibility_path, accessibility)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["flagship_customer_ux_evidence"]["accessibility_proof_ready"] is False
    assert receipt["accessibility"]["flagship_proof"]["missing_browser_engines"] == ["webkit"]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "accessibility")
    assert blocker["proof"]["missing_samples"]


@pytest.mark.parametrize("metric_value", [None, "0", False, 1])
def test_gold_status_flagship_rejects_missing_or_nonzero_moderate_wcag_metric(
    tmp_path: Path,
    metric_value: object,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    accessibility_path = Path(flagship_args["accessibility_receipt_path"])
    accessibility = json.loads(accessibility_path.read_text(encoding="utf-8"))
    first_row = accessibility["routes"][0]
    if metric_value is None:
        first_row["metrics"].pop("axe_moderate_or_higher_wcag_count")
    else:
        first_row["metrics"]["axe_moderate_or_higher_wcag_count"] = metric_value
    _write_json(accessibility_path, accessibility)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["accessibility"]["flagship_proof_ok"] is False
    failed = receipt["accessibility"]["flagship_proof"]["failed_rows"]
    assert failed
    expected = 1 if metric_value == 1 and type(metric_value) is int else "missing_or_invalid"
    assert failed[0]["moderate_or_higher_wcag_count"] == expected


@pytest.mark.parametrize(
    ("concrete_route", "placeholder_route", "expected_family"),
    (
        (
            "/app/research/current-result?run_id=run-flagship",
            "/app/research/{candidate_id}?run_id={run_id}",
            "/app/research/[detail]",
        ),
        (
            "/app/shortlist/run/run-flagship",
            "/app/shortlist/run/{run_id}",
            "/app/shortlist/run/[run]",
        ),
        (
            "/tours/tour-flagship",
            "/tours/{slug}",
            "/tours/[slug]",
        ),
    ),
)
def test_gold_status_flagship_rejects_literal_accessibility_route_placeholders(
    tmp_path: Path,
    concrete_route: str,
    placeholder_route: str,
    expected_family: str,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    accessibility_path = Path(flagship_args["accessibility_receipt_path"])
    accessibility = json.loads(accessibility_path.read_text(encoding="utf-8"))
    accessibility["configured_routes"] = [
        placeholder_route if route == concrete_route else route
        for route in accessibility["configured_routes"]
    ]
    for row in accessibility["routes"]:
        if row["route"] == concrete_route:
            row["route"] = placeholder_route
    _write_json(accessibility_path, accessibility)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    proof = receipt["accessibility"]["flagship_proof"]
    assert receipt["status"] == "blocked"
    assert proof["literal_placeholder_routes"] == [placeholder_route]
    assert proof["dynamic_routes"][expected_family] == []
    assert any(row["route"] == expected_family for row in proof["missing_samples"])


@pytest.mark.parametrize(
    ("route_prefix", "expected_route_key"),
    [
        ("/app/billing", "/app/billing"),
        ("/app/shortlist/run/", "/app/shortlist/run/[run]"),
        ("/tours/", "/tours/[slug]"),
    ],
)
def test_gold_status_flagship_rejects_missing_core_accessibility_route_family(
    tmp_path: Path,
    route_prefix: str,
    expected_route_key: str,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    accessibility_path = Path(flagship_args["accessibility_receipt_path"])
    accessibility = json.loads(accessibility_path.read_text(encoding="utf-8"))
    accessibility["configured_routes"] = [
        route
        for route in accessibility["configured_routes"]
        if not str(route).split("?", 1)[0].startswith(route_prefix)
    ]
    accessibility["routes"] = [
        row
        for row in accessibility["routes"]
        if not str(row["route"]).split("?", 1)[0].startswith(route_prefix)
    ]
    accessibility["route_count"] = len(accessibility["routes"])
    _write_json(accessibility_path, accessibility)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["flagship_customer_ux_evidence"]["accessibility_proof_ready"] is False
    assert any(
        row["route"] == expected_route_key
        for row in receipt["accessibility"]["flagship_proof"]["missing_samples"]
    )


def test_gold_status_flagship_rejects_stale_accessibility_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    accessibility_path = Path(flagship_args["accessibility_receipt_path"])
    accessibility = json.loads(accessibility_path.read_text(encoding="utf-8"))
    accessibility["generated_at"] = (now - timedelta(hours=2)).isoformat()
    _write_json(accessibility_path, accessibility)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert any(
        row["area"] == "accessibility" and row["status"] == "stale"
        for row in receipt["receipt_freshness"]["stale_receipts"]
    )


def test_gold_status_flagship_rejects_mock_boundary_activation_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    activation_path = Path(flagship_args["activation_to_value_receipt_path"])
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    activation["live_contract"]["provider_response_mocking_forbidden"] = False
    _write_json(activation_path, activation)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["flagship_customer_ux_evidence"]["activation_to_value_proof_ready"] is False
    assert receipt["activation_to_value"]["flagship_proof"]["missing_live_contract"] == [
        "provider_response_mocking_forbidden"
    ]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "activation_to_value")
    assert blocker["proof"]["missing_live_contract"] == ["provider_response_mocking_forbidden"]


def test_gold_status_flagship_rejects_contract_mock_activation_proof_mode(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    activation_path = Path(flagship_args["activation_to_value_receipt_path"])
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    activation["proof_mode"] = "contract_mock"
    _write_json(activation_path, activation)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["activation_to_value"]["flagship_proof_ok"] is False
    assert receipt["activation_to_value"]["flagship_proof"]["proof_mode"] == "contract_mock"


def test_gold_status_flagship_rejects_contract_mock_failure_state_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    failure_path = Path(flagship_args["failure_state_receipt_path"])
    failure_states = json.loads(failure_path.read_text(encoding="utf-8"))
    failure_states["proof_mode"] = "contract_mock"
    failure_states["checks"][2]["ok"] = False
    _write_json(failure_path, failure_states)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["failure_states"]["flagship_proof_ok"] is False
    assert receipt["failure_states"]["flagship_proof"]["proof_mode"] == "contract_mock"
    assert any(row["area"] == "failure_states" for row in receipt["blockers"])


def test_gold_status_flagship_independently_rejects_changed_customer_snapshot(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    failure_path = Path(flagship_args["failure_state_receipt_path"])
    failure_states = json.loads(failure_path.read_text(encoding="utf-8"))
    failure_states["rows"][0]["preservation_probe"]["after"]["sha256"] = "b" * 64
    _write_json(failure_path, failure_states)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["failure_states"]["flagship_proof_ok"] is False
    assert "preservation_probe_receipt" in receipt["failure_states"]["flagship_proof"]["failed_rows"][0]["missing_checks"]


def test_gold_status_flagship_rejects_noncanonical_preservation_probe_route(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    failure_path = Path(flagship_args["failure_state_receipt_path"])
    failure_states = json.loads(failure_path.read_text(encoding="utf-8"))
    failure_states["preservation_probe_route"] = "/health"
    _write_json(failure_path, failure_states)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    proof = receipt["failure_states"]["flagship_proof"]
    assert proof["preservation_probe_route"] == "/health"
    assert proof["required_preservation_probe_route"] == (
        gold_status.REQUIRED_FLAGSHIP_FAILURE_PRESERVATION_ROUTE
    )


def test_gold_status_flagship_rejects_stale_activation_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    activation_path = Path(flagship_args["activation_to_value_receipt_path"])
    activation = json.loads(activation_path.read_text(encoding="utf-8"))
    activation["generated_at"] = (now - timedelta(hours=2)).isoformat()
    _write_json(activation_path, activation)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert any(
        row["area"] == "activation_to_value" and row["status"] == "stale"
        for row in receipt["receipt_freshness"]["stale_receipts"]
    )


def test_gold_status_flagship_rejects_chromium_only_proof_unless_explicitly_configured(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    live_mobile_path = Path(flagship_args["live_mobile_receipt_path"])
    live_mobile = json.loads(live_mobile_path.read_text(encoding="utf-8"))
    live_mobile["routes"] = [
        row for row in live_mobile["routes"]
        if row["browser_engine"] == "chromium"
    ]
    live_mobile["route_count"] = len(live_mobile["routes"])
    live_mobile["browser_engine"] = "chromium"
    live_mobile["browser_engines"] = ["chromium"]
    live_mobile["required_browser_engines"] = ["chromium"]
    live_mobile["browser_proof"]["required_browser_engines"] = ["chromium"]
    live_mobile["browser_proof"]["observed_browser_engines"] = ["chromium"]
    live_mobile["browser_proof"]["expected_sample_count"] = len(live_mobile["routes"])
    live_mobile["browser_proof"]["proven_sample_count"] = len(live_mobile["routes"])
    _write_json(live_mobile_path, live_mobile)

    default_receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert default_receipt["status"] == "blocked"
    proof = default_receipt["live_mobile_surfaces"]["flagship_browser_proof"]
    assert proof["missing_browser_engines"] == ["firefox", "webkit"]
    assert {row["browser_engine"] for row in proof["missing_samples"]} == {"firefox", "webkit"}

    chromium_compatibility_receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        required_browser_engines=("chromium",),
        max_receipt_age_hours=1,
        now=now,
    )
    assert chromium_compatibility_receipt["status"] == "pass"
    assert chromium_compatibility_receipt["flagship_customer_ux_evidence"]["required_browser_engines"] == [
        "chromium"
    ]


def test_gold_status_flagship_profile_rejects_static_or_synthetic_mobile_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=generated_at)
    live_mobile_path = Path(flagship_args["live_mobile_receipt_path"])
    live_mobile = json.loads(live_mobile_path.read_text(encoding="utf-8"))
    live_mobile["routes"][0]["proof_mode"] = "static_html"
    live_mobile["routes"][0]["metrics"]["proof_mode"] = "static_html"
    live_mobile["routes"][0]["metrics"]["static_html_probe"] = True
    _write_json(live_mobile_path, live_mobile)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["flagship_customer_ux_evidence"]["browser_all_mobile_proof_ready"] is False
    blocker = next(row for row in receipt["blockers"] if row["area"] == "live_mobile_surfaces")
    assert blocker["flagship_browser_proof"]["static_or_synthetic_rows"][0]["proof_mode"] == "static_html"


def test_gold_status_flagship_profile_blocks_stale_customer_ux_evidence(tmp_path: Path) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    fresh_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(tmp_path, generated_at=fresh_at)
    public_smoke_path = Path(flagship_args["public_smoke_receipt_path"])
    public_smoke = json.loads(public_smoke_path.read_text(encoding="utf-8"))
    public_smoke["generated_at"] = (now - timedelta(hours=2)).isoformat()
    _write_json(public_smoke_path, public_smoke)

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=fresh_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["flagship_customer_ux_evidence"]["ready"] is False
    assert receipt["receipt_freshness"]["status"] == "fail"
    assert any(
        row["area"] == "public_auth_surfaces" and row["status"] == "stale"
        for row in receipt["receipt_freshness"]["stale_receipts"]
    )


def test_gold_status_cli_keeps_live_container_tour_receipt_as_fallback() -> None:
    source = (ROOT / "scripts/propertyquarry_gold_status.py").read_text(encoding="utf-8")

    assert "state/receipts/propertyquarry_live_authenticated*.json" in source
    assert "state/receipts/property_provider_stage*.json" in source
    assert "_completion/property_tour_controls/*.json" in source
    assert "_completion/tours/property-tour-controls-live-container-current.json" in source
    assert "_completion/smoke/property-live-mobile-surface-latest.json" in source
    assert "_completion/smoke/property-live-public-latest.json" in source
    assert "_completion/smoke/property-live-authenticated-latest.json" in source
    assert "_completion/smoke/property-live-3d-browser-gate-latest.json" in source
    assert "_completion/smoke/property-live-walkthrough-quality-latest.json" in source
    assert "_completion/scene_video_readiness/release-gate.json" in source
    assert "_completion/scene_video_readiness/release-gate-verifier.json" in source
    assert "_completion/scene_video_readiness/runtime-status.json" in source
    assert "_completion/scene_video_readiness/provider-refresh-packet.json" in source
    assert "_completion/scene_video_readiness/provider-refresh-packet-verifier.json" in source
    assert "_completion/smoke/property-live-mobile-surface-with-research-detail-pass.json" not in source
    assert "_completion/tours/property-tour-controls-after-monotonic-counters.json" not in source


def test_gold_status_defaults_pick_newest_matching_receipt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    older = _write_json(
        tmp_path / "_completion" / "tours" / "property-tour-controls-live-container-current.json",
        {"generated_at": "2026-06-26T01:00:00+00:00", "status": "blocked_missing_provider_modes"},
    )
    newer = _write_json(
        tmp_path / "_completion" / "tours" / "property-tour-controls-live-current-refresh.json",
        {"generated_at": "2026-06-26T03:45:47+00:00", "status": "blocked_missing_provider_modes"},
    )

    selected = _latest_receipt_path(
        ("_completion/tours/property-tour-controls*.json",),
        fallback=str(older),
    )

    assert selected == newer


def test_gold_status_tour_control_default_prefers_complete_live_container_receipt(tmp_path: Path, monkeypatch) -> None:
    from scripts.propertyquarry_gold_status import _default_receipt_path

    monkeypatch.chdir(tmp_path)
    live_container = _write_json(
        tmp_path / "_completion" / "tours" / "property-tour-controls-live-container-current.json",
        {"generated_at": "2026-06-26T23:28:00+00:00", "status": "pass"},
    )
    _write_json(
        tmp_path / "_completion" / "property_tour_controls" / "strict-current.json",
        {"generated_at": "2026-06-27T11:36:37+00:00", "status": "blocked_missing_provider_modes"},
    )
    _write_json(
        tmp_path / "_completion" / "tours" / "property-tour-controls-current.json",
        {"generated_at": "2026-06-27T12:00:00+00:00", "status": "blocked_missing_provider_modes"},
    )

    assert _default_receipt_path("tour_control") == live_container.resolve()


def test_gold_status_defaults_prefer_complete_receipt_over_newer_running_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    complete_receipt = _write_json(
        tmp_path / "_completion" / "provider_smoke" / "production-e2e-provider-matrix-current.json",
        {
            "generated_at": "2026-06-26T19:15:15+00:00",
            "status": "pass",
            "complete": True,
            "checkpoint": False,
        },
    )
    _write_json(
        tmp_path / "_completion" / "provider_smoke" / "goal-continuation-provider-matrix.json",
        {
            "generated_at": "2026-06-26T19:16:15+00:00",
            "status": "running",
            "complete": False,
            "checkpoint": True,
        },
    )

    selected = _latest_receipt_path(
        ("_completion/provider_smoke/*.json",),
        fallback=str(complete_receipt),
    )

    assert selected == complete_receipt


def test_gold_status_provider_matrix_default_finds_live_e2e_receipts(tmp_path: Path, monkeypatch) -> None:
    from scripts.propertyquarry_gold_status import _default_receipt_path

    monkeypatch.chdir(tmp_path)
    _write_json(
        tmp_path / "_completion" / "provider_smoke" / "all-search-ready-current-resumed.json",
        {"generated_at": "2026-06-26T09:00:00+00:00", "status": "pass"},
    )
    _write_json(
        tmp_path / "_completion" / "smoke" / "property-provider-e2e-at-de-cr-latest.json",
        {"generated_at": "2026-06-26T11:07:15+00:00", "status": "pass"},
    )
    _write_json(
        tmp_path / "_completion" / "smoke" / "property-live-provider-latest.json",
        {"generated_at": "2026-06-26T12:10:00+00:00", "status": "blocked_targeted_search_matrix_not_executed"},
    )
    deploy_receipt = _write_json(
        tmp_path / "_completion" / "provider_smoke" / "production-e2e-provider-matrix-current.json",
        {
            "generated_at": "2026-06-26T19:19:23+00:00",
            "status": "pass",
            "complete": True,
            "checkpoint": False,
        },
    )

    assert _default_receipt_path("provider_matrix") == deploy_receipt.resolve()


def test_gold_status_provider_matrix_default_prefers_executed_pass_over_newer_planned_wrapper(tmp_path: Path, monkeypatch) -> None:
    from scripts.propertyquarry_gold_status import _default_receipt_path

    monkeypatch.chdir(tmp_path)
    deploy_receipt = _write_json(
        tmp_path / "_completion" / "provider_smoke" / "production-e2e-provider-matrix-current.json",
        {
            "generated_at": "2026-06-26T19:28:32.892417+00:00",
            "status": "pass",
            "country_scope": "all_search_ready",
            "targeted_search_matrix_status": "pass",
            "targeted_search_matrix_executed": True,
            "targeted_search_matrix_summary": {
                "executed": True,
                "all_search_ready_providers_covered": True,
            },
        },
    )
    _write_json(
        tmp_path / "_completion" / "smoke" / "property-live-provider-latest.json",
        {
            "generated_at": "2026-06-26T20:35:20.798671+00:00",
            "status": "blocked_targeted_search_matrix_not_executed",
            "country_scope": "all_search_ready",
            "targeted_search_matrix_status": "planned",
            "targeted_search_matrix_executed": False,
            "targeted_search_matrix_summary": {
                "executed": False,
                "all_search_ready_providers_covered": False,
            },
        },
    )

    assert _default_receipt_path("provider_matrix") == deploy_receipt.resolve()


def test_gold_status_provider_matrix_default_prefers_broader_staged_receipt_over_narrower_newer_slice(tmp_path: Path, monkeypatch) -> None:
    from scripts.propertyquarry_gold_status import _default_receipt_path

    monkeypatch.chdir(tmp_path)
    aggregate = _write_json(
        tmp_path / "state" / "receipts" / "property_provider_stage_at_de_cr_batch2.json",
        {
            "generated_at": "2026-06-27T21:18:38.700044+00:00",
            "status": "staged_provider_coverage_incomplete",
            "targeted_search_matrix_status": "partial",
            "targeted_search_matrix_executed": True,
            "targeted_search_matrix_count": 12,
            "targeted_search_matrix_summary": {
                "executed": True,
                "case_count": 12,
                "executed_case_count": 12,
            },
        },
    )
    _write_json(
        tmp_path / "state" / "receipts" / "property_live_provider_smoke_at_willhaben_e2e.json",
        {
            "generated_at": "2026-06-27T21:30:00+00:00",
            "status": "staged_provider_coverage_incomplete",
            "targeted_search_matrix_status": "partial",
            "targeted_search_matrix_executed": True,
            "targeted_search_matrix_count": 2,
            "targeted_search_matrix_summary": {
                "executed": True,
                "case_count": 2,
                "executed_case_count": 2,
            },
        },
    )

    assert _default_receipt_path("provider_matrix") == aggregate.resolve()


def test_gold_status_provider_matrix_default_prefers_current_at_de_cr_scope_over_older_broader_history(tmp_path: Path, monkeypatch) -> None:
    from scripts.propertyquarry_gold_status import _default_receipt_path

    monkeypatch.chdir(tmp_path)
    _write_json(
        tmp_path / "_completion" / "provider_smoke" / "production-e2e-provider-matrix-rerun30.json",
        {
            "generated_at": "2026-06-26T09:19:42.699605+00:00",
            "status": "pass",
            "country_scope": "all_search_ready",
            "targeted_search_matrix_status": "pass",
            "targeted_search_matrix_executed": True,
            "targeted_search_matrix_count": 242,
            "targeted_search_matrix_summary": {
                "executed": True,
                "case_count": 242,
                "country_codes": ["AT", "BE", "CA", "CR", "DE", "CH", "IE", "UK", "AU", "ES", "IT", "FR", "NL", "PT", "PL", "SE", "US"],
                "all_search_ready_providers_covered": True,
            },
        },
    )
    current_scope = _write_json(
        tmp_path / "_completion" / "provider_smoke" / "production-e2e-provider-matrix-at-de-cr-current.json",
        {
            "generated_at": "2026-06-27T18:08:25.149862+00:00",
            "status": "pass",
            "country_scope": "explicit",
            "targeted_search_matrix_status": "pass",
            "targeted_search_matrix_executed": True,
            "targeted_search_matrix_count": 140,
            "targeted_search_matrix_summary": {
                "executed": True,
                "case_count": 140,
                "country_codes": ["AT", "DE", "CR"],
                "all_search_ready_providers_covered": True,
            },
        },
    )

    assert _default_receipt_path("provider_matrix") == current_scope.resolve()


def test_gold_status_provider_matrix_default_prefers_newer_all_search_ready_active_scope_over_older_explicit_alias(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from scripts.propertyquarry_gold_status import _default_receipt_path

    monkeypatch.chdir(tmp_path)
    _write_json(
        tmp_path / "_completion" / "provider_smoke" / "production-e2e-provider-matrix-current.refreshing.json",
        {
            "generated_at": "2026-07-03T16:15:54.208492+00:00",
            "status": "pass",
            "country_scope": "explicit",
            "targeted_search_matrix_status": "pass",
            "targeted_search_matrix_executed": True,
            "targeted_search_matrix_count": 160,
            "targeted_search_matrix_summary": {
                "executed": True,
                "case_count": 160,
                "country_codes": ["AT", "DE", "CR"],
                "all_search_ready_providers_covered": True,
            },
        },
    )
    refreshed = _write_json(
        tmp_path / "_completion" / "provider_smoke" / "production-e2e-provider-matrix-at-de-cr-current.json",
        {
            "generated_at": "2026-07-06T14:41:26.751944+00:00",
            "status": "pass",
            "country_scope": "all_search_ready",
            "targeted_search_matrix_status": "pass",
            "targeted_search_matrix_executed": True,
            "targeted_search_matrix_count": 160,
            "targeted_search_matrix_summary": {
                "executed": True,
                "case_count": 160,
                "country_codes": ["AT", "DE", "CR"],
                "all_search_ready_providers_covered": True,
            },
        },
    )

    assert _default_receipt_path("provider_matrix") == refreshed.resolve()


def test_gold_status_authenticated_smoke_default_finds_newer_state_receipt(tmp_path: Path, monkeypatch) -> None:
    from scripts.propertyquarry_gold_status import _default_receipt_path

    monkeypatch.chdir(tmp_path)
    _write_json(
        tmp_path / "_completion" / "smoke" / "property-live-authenticated-latest.json",
        {"generated_at": "2026-06-27T19:19:51.123278+00:00", "status": "pass"},
    )
    current = _write_json(
        tmp_path / "state" / "receipts" / "propertyquarry_live_authenticated_smoke_latest.json",
        {"generated_at": "2026-06-27T21:26:46.749288+00:00", "status": "pass"},
    )

    assert _default_receipt_path("authenticated_smoke") == current.resolve()


def test_gold_status_provider_ownership_default_finds_release_gate_receipt(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    expected = _write_json(
        tmp_path / "_completion" / "property_tour_ownership" / "release-gate.json",
        _tour_provider_ownership_payload(),
    )

    assert gold_status._default_receipt_path("tour_provider_ownership") == expected.resolve()


def test_gold_status_write_syncs_latest_aliases_from_release_gate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    output_path = tmp_path / "_completion" / "property_gold_status" / "release-gate.json"
    payload = json.dumps({"status": "pass", "generated_at": "2026-06-27T11:05:53+00:00"})

    synced = gold_status._write_gold_status_output(output_path, payload)

    latest_path = (tmp_path / "_completion" / "property_gold_status" / "latest.json").resolve()
    legacy_path = (tmp_path / "_completion" / "propertyquarry-gold-status-latest.json").resolve()
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "pass"
    assert json.loads(latest_path.read_text(encoding="utf-8"))["status"] == "pass"
    assert json.loads(legacy_path.read_text(encoding="utf-8"))["status"] == "pass"
    assert synced == [str(latest_path), str(legacy_path)]


def test_gold_status_write_syncs_other_latest_alias_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    output_path = tmp_path / "_completion" / "property_gold_status" / "latest.json"
    payload = json.dumps({"status": "pass", "generated_at": "2026-06-27T11:05:53+00:00"})

    synced = gold_status._write_gold_status_output(output_path, payload)

    legacy_path = (tmp_path / "_completion" / "propertyquarry-gold-status-latest.json").resolve()
    release_gate_path = tmp_path / "_completion" / "property_gold_status" / "release-gate.json"
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "pass"
    assert json.loads(legacy_path.read_text(encoding="utf-8"))["status"] == "pass"
    assert not release_gate_path.exists()
    assert synced == [str(legacy_path)]


def test_gold_status_write_replaces_final_symlink_without_touching_its_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    output_path = tmp_path / "_completion" / "property_gold_status" / "release-gate.json"
    output_path.parent.mkdir(parents=True)
    symlink_target = tmp_path / "must-not-change.json"
    symlink_target.write_text('{"status":"keep"}\n', encoding="utf-8")
    output_path.symlink_to(symlink_target)
    payload = json.dumps({"status": "pass", "generated_at": "2026-06-27T11:05:53+00:00"})

    synced = gold_status._write_gold_status_output(output_path, payload)

    assert symlink_target.read_text(encoding="utf-8") == '{"status":"keep"}\n'
    assert not output_path.is_symlink()
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "pass"
    assert synced == [
        str(tmp_path / "_completion" / "property_gold_status" / "latest.json"),
        str(tmp_path / "_completion" / "propertyquarry-gold-status-latest.json"),
    ]


def test_gold_status_write_rejects_symlinked_output_parent(
    tmp_path: Path,
) -> None:
    redirected_parent = tmp_path / "redirected"
    redirected_parent.mkdir()
    unsafe_parent = tmp_path / "output"
    unsafe_parent.symlink_to(redirected_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="Unsafe Gold status output parent"):
        gold_status._write_gold_status_output(
            unsafe_parent / "status.json",
            '{"status":"pass"}',
        )

    assert not (redirected_parent / "status.json").exists()


def test_gold_status_write_rejects_peer_writable_ancestor_before_creating_output(
    tmp_path: Path,
) -> None:
    unsafe_parent = tmp_path / "unsafe"
    unsafe_parent.mkdir()
    unsafe_parent.chmod(0o777)
    descendant = unsafe_parent / "must-not-be-created"
    try:
        with pytest.raises(
            ValueError,
            match="Unsafe Gold status output directory chain",
        ):
            gold_status._write_gold_status_output(
                descendant / "status.json",
                '{"status":"pass"}',
            )
    finally:
        unsafe_parent.chmod(0o700)

    assert not descendant.exists()


def test_gold_status_write_detects_parent_swap_without_redirecting_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_parent = tmp_path / "trusted"
    attacker_parent = tmp_path / "attacker"
    detached_parent = tmp_path / "detached-trusted"
    original_parent.mkdir()
    attacker_parent.mkdir()
    attacker_output = attacker_parent / "status.json"
    attacker_output.write_text("attacker-sentinel\n", encoding="utf-8")
    output_path = original_parent / "status.json"
    swapped = False

    def swap_parent_before_staging(_byte_count: int | None = None) -> str:
        nonlocal swapped
        if not swapped:
            original_parent.rename(detached_parent)
            attacker_parent.rename(original_parent)
            swapped = True
        return "0" * 32

    monkeypatch.setattr(gold_status.secrets, "token_hex", swap_parent_before_staging)

    with pytest.raises(RuntimeError, match="output parent changed"):
        gold_status._write_gold_status_output(output_path, '{"status":"pass"}')

    assert (original_parent / "status.json").read_text(encoding="utf-8") == (
        "attacker-sentinel\n"
    )
    assert not (detached_parent / "status.json").exists()
    assert list(detached_parent.glob(".propertyquarry-gold-*.tmp")) == []


@pytest.mark.parametrize("failure_after", (1, 2, 3))
def test_gold_status_write_clears_all_canonical_outputs_after_partial_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_after: int,
) -> None:
    monkeypatch.chdir(tmp_path)
    release_gate_path = (
        tmp_path / "_completion" / "property_gold_status" / "release-gate.json"
    )
    latest_path = tmp_path / "_completion" / "property_gold_status" / "latest.json"
    legacy_path = tmp_path / "_completion" / "propertyquarry-gold-status-latest.json"
    canonical_paths = (release_gate_path, latest_path, legacy_path)
    for path in canonical_paths:
        _write_json(path, {"status": "previous-pass"})

    real_publish = gold_status._publish_private_text_at
    publication_count = 0

    def publish_then_fail(
        output_path: Path,
        rendered: str,
        directory_chain: gold_status._GoldStatusDirectoryChain,
    ) -> tuple[int, int]:
        nonlocal publication_count
        identity = real_publish(output_path, rendered, directory_chain)
        publication_count += 1
        if publication_count == failure_after:
            raise OSError(f"injected failure after publication {failure_after}")
        return identity

    monkeypatch.setattr(gold_status, "_publish_private_text_at", publish_then_fail)

    with pytest.raises(
        OSError,
        match=f"injected failure after publication {failure_after}",
    ):
        gold_status._write_gold_status_output(
            release_gate_path,
            '{"status":"pass"}',
        )

    assert publication_count == failure_after
    assert all(not path.exists() for path in canonical_paths)
    assert list(tmp_path.rglob(".propertyquarry-gold-*.tmp")) == []


def _provider_matrix_payload(*, status: str = "pass", executed: bool = True) -> dict[str, object]:
    return {
        "status": status,
        "country_scope": "all_search_ready",
        "targeted_search_matrix_status": "pass" if status == "pass" else "planned",
        "targeted_search_matrix_executed": executed,
        "targeted_search_matrix_count": 242,
        "targeted_search_matrix_summary": {
            "executed": executed,
            "strict_case_count": 121,
            "soft_filter_case_count": 121,
            "failed_case_count": 0,
            "all_search_ready_providers_covered": True,
            "all_search_ready_provider_modes_passed": True,
            "dispatch_acceptance_complete": True,
            "status_readback_complete": True,
            "payload_contracts_ok": True,
            "provider_country_scope_ok": True,
            "target_context_country_scope_ok": True,
            "agent_unlimited_results_ok": True,
            "strict_without_soft_filters_ok": True,
            "soft_filters_present_ok": True,
        },
        "cross_country_sanitization_summary": {
            "case_count": 18,
            "status_counts": {"pass": 18},
            "sanitization_ok": True,
        },
    }


def _provider_catalog_payload(*, check_status: str = "pass") -> dict[str, object]:
    return {
        "generated_at": "2026-06-26T19:30:00+00:00",
        "status": "blocked_targeted_search_matrix_not_executed",
        "targeted_search_matrix_status": "planned",
        "targeted_search_matrix_executed": False,
        "targeted_search_matrix_count": 6,
        "checks": [
            {
                "country_code": "AT",
                "status": check_status,
                "runtime_provider_count_ok": check_status == "pass",
                "runtime_defaults_present_ok": True,
                "runtime_provider_country_scope_ok": True,
            }
        ],
        "targeted_search_matrix_summary": {
            "executed": False,
            "planned_case_count": 6,
            "executed_case_count": 0,
            "passed_case_count": 0,
            "all_search_ready_provider_modes_passed": True,
            "country_codes": ["AT"],
        },
    }


def _performance_payload(
    *,
    include_research_checks: bool = True,
    include_search_checks: bool = True,
    include_analytics_checks: bool = True,
    generated_at: str = "2026-07-13T11:55:00+00:00",
    executable_identity: dict[str, object] | None = None,
) -> dict[str, object]:
    research_checks = [
        {"name": "research_candidate", "ok": True},
        {"name": "research_visual_cards_present", "ok": True},
        {"name": "research_visual_requests_honest", "ok": True},
        {"name": "research_no_fake_visual_ready", "ok": True},
        {"name": "research_listing_facts", "ok": True},
        {"name": "research_listed_price_signal", "ok": True},
        {"name": "research_ranking_only_no_compare_cards", "ok": True},
        {"name": "research_mobile_open_property_compact_layout", "ok": True},
        {"name": "research_mobile_visual_frame_compact", "ok": True},
    ]
    search_checks = [
        {"name": "search_gzip_delivery", "ok": True},
        {"name": "search_gzip_vary_accept_encoding", "ok": True},
        {"name": "search_compressed_payload_under_budget", "ok": True},
        {"name": "what_matters_distance_controls_compact", "ok": True},
        {"name": "what_matters_school_distance_controls", "ok": True},
    ]
    analytics_checks = [
        {"name": "rybbit_no_identify", "ok": True},
        {"name": "rybbit_taxonomy_events_only", "ok": True},
        {"name": "rybbit_allowed_attributes_only", "ok": True},
        {"name": "rybbit_no_private_payload", "ok": True},
    ]
    if include_analytics_checks:
        research_checks.extend(analytics_checks)
        search_checks.extend(analytics_checks)
    route_paths = [
        "/sign-in",
        "/app/search",
        "/app/agents",
        "/app/properties?run_id=run-gold",
        "/app/shortlist?run_id=run-gold",
        "/app/research/perf-candidate-1020?run_id=run-gold",
        "/app/alerts?run_id=run-gold",
        "/app/account",
        "/app/billing",
        "/app/settings/google",
        "/app/settings/access",
        "/app/settings/usage",
        "/app/settings/support",
        "/app/settings/trust",
        "/app/settings/invitations",
    ]
    route_rows: list[dict[str, object]] = []
    for path in route_paths:
        route_checks: list[dict[str, object]] = [
            {"name": "route_response_ok", "ok": True}
        ]
        if path == "/app/search":
            route_checks.extend(search_checks if include_search_checks else [])
        elif path.startswith("/app/research/"):
            route_checks.extend(
                research_checks if include_research_checks else research_checks[:4]
            )
        route_rows.append(
            {
                "path": path,
                "ok": True,
                "attempt_count": 2,
                "attempt_durations_ms": [180, 120],
                "first_duration_ms": 180,
                "duration_ms": 120,
                "budget_ms": 1200,
                "cold_budget_ms": 2400,
                "status_code": 200,
                "checks": route_checks,
                "measurements": {
                    "cold": {
                        "sequence": 1,
                        "kind": "first_measured_request_after_fixture_setup",
                        "duration_ms": 180,
                        "status_code": 200,
                        "response_bytes": 64_000,
                        "budget_ms": 2400,
                        "cache_state": "server_cache_not_explicitly_prewarmed_or_cleared",
                        "ok": True,
                    },
                    "warm": {
                        "sequence": 2,
                        "kind": "same_client_immediate_repeat_request",
                        "duration_ms": 120,
                        "status_code": 200,
                        "response_bytes": 64_000,
                        "budget_ms": 1200,
                        "cache_state": "same_process_and_client_repeat_eligible",
                        "ok": True,
                    },
                },
                "cold_to_warm": {
                    "duration_delta_ms": 60,
                    "response_bytes_delta": 0,
                },
            }
        )

    def browser_measurement(phase: str) -> dict[str, object]:
        is_cold = phase == "cold"
        nonce_sha256 = hashlib.sha256(
            f"propertyquarry-test-{phase}-verified-probe-nonce".encode("utf-8")
        ).hexdigest()
        checks = [
            {"name": f"{phase}_navigation_under_budget", "ok": True},
            {"name": f"{phase}_request_observed", "ok": True},
            {"name": f"{phase}_request_count_under_budget", "ok": True},
            {"name": f"{phase}_transferred_bytes_under_budget", "ok": True},
            {"name": f"{phase}_failed_requests_under_budget", "ok": True},
            {"name": f"{phase}_requests_completed", "ok": True},
        ]
        if is_cold:
            checks.extend(
                (
                    {"name": "cold_transferred_bytes_observed", "ok": True},
                    {
                        "name": "cold_cdp_document_signing_interception_ok",
                        "ok": True,
                    },
                )
            )
        else:
            checks.extend(
                (
                    {
                        "name": "warm_signed_release_probe_nonces_unique",
                        "ok": True,
                    },
                    {
                        "name": "warm_cdp_document_signing_interception_ok",
                        "ok": True,
                    },
                    {"name": "warm_http_cache_reuse_observed", "ok": True},
                )
            )
        checks.extend(
            [
                {"name": f"{phase}_navigation_status_ok", "ok": True},
                {"name": f"{phase}_final_target_url_observed", "ok": True},
                {
                    "name": f"{phase}_document_release_identity_exact",
                    "ok": True,
                },
                {
                    "name": f"{phase}_document_cache_control_no_store",
                    "ok": True,
                },
                {
                    "name": f"{phase}_server_verified_probe_nonce_acknowledged",
                    "ok": True,
                },
                {
                    "name": f"{phase}_authenticated_app_surface_observed",
                    "ok": True,
                },
            ]
        )
        return {
            "phase": phase,
            "cache_state": (
                "cleared_before_navigation"
                if is_cold
                else "same_context_repeat_cache_observed"
            ),
            "duration_ms": 2400 if is_cold else 1400,
            "status_code": 200,
            "final_url": "https://propertyquarry.com/app/search",
            "document_release_identity": {
                "commit_sha": _PERFORMANCE_RELEASE_COMMIT_SHA,
                "image_digest": _PERFORMANCE_RELEASE_IMAGE_DIGEST,
                "deployment_id": _PERFORMANCE_RELEASE_DEPLOYMENT_ID,
                "manifest_status": "complete",
                "manifest_sha256": _PERFORMANCE_MANIFEST_SHA256,
                "replica_id": f"propertyquarry-production-{phase}-replica",
            },
            "document_authentication_binding": {
                "cache_control": "no-store",
                "expected_nonce_sha256": nonce_sha256,
                "acknowledged_nonce_sha256": nonce_sha256,
            },
            "request_count": 32 if is_cold else 12,
            "transferred_bytes": 480_000 if is_cold else 120_000,
            "failed_request_count": 0,
            "failed_requests": [],
            "incomplete_request_count": 0,
            "cache_hit_count": 0 if is_cold else 8,
            "subresource_cache_hit_count": 0 if is_cold else 7,
            "slowest_resources": [
                {
                    "url": "https://propertyquarry.com/app/search",
                    "resource_type": "Document",
                    "status_code": 200,
                    "duration_ms": 800 if is_cold else 400,
                    "transferred_bytes": 120_000 if is_cold else 20_000,
                    "cache_source": "network",
                    "failed": False,
                    "incomplete": False,
                }
            ],
            "navigation_timing": {
                "responseStartMs": 300,
                "responseEndMs": 600,
                "domContentLoadedMs": 900,
                "loadEventMs": 1100,
                "transferSize": 120_000,
                "encodedBodySize": 100_000,
                "decodedBodySize": 240_000,
            },
            "checks": checks,
            "ok": True,
        }

    cold = browser_measurement("cold")
    warm = browser_measurement("warm")
    release_identity = {
        "commit_sha": _PERFORMANCE_RELEASE_COMMIT_SHA,
        "image_digest": _PERFORMANCE_RELEASE_IMAGE_DIGEST,
        "deployment_id": _PERFORMANCE_RELEASE_DEPLOYMENT_ID,
        "manifest_sha256": _PERFORMANCE_MANIFEST_SHA256,
    }
    return {
        "schema": gold_status.AUTHENTICATED_PERFORMANCE_FLAGSHIP_SCHEMA,
        "generated_at": generated_at,
        "status": "pass",
        "status_scope": "legacy_authenticated_route_smoke_plus_any_explicitly_requested_constrained_probe",
        "flagship_status": "pass",
        "flagship_blockers": [],
        "principal_id": "principal-performance-gold",
        "run_id": "run-performance-gold",
        "failed_count": 0,
        "route_count": 15,
        "release_identity": release_identity,
        "thresholds": {
            "warm_route_budget_ms": 1200,
            "cold_route_budget_ms": 2400,
        },
        "server_request_evidence": {
            "status": "pass",
            "cold_definition": "first measured route request after authenticated fixture setup; server caches are neither claimed empty nor explicitly prewarmed",
            "warm_definition": "immediate same-process and same-client repeat request",
            "cold_route_count": 15,
            "warm_route_count": 15,
        },
        "routes": route_rows,
        "constrained_client_evidence": {
            "status": "pass",
            "profile": json.loads(
                json.dumps(gold_status.AUTHENTICATED_PERFORMANCE_FLAGSHIP_PROFILE)
            ),
            "target": "https://propertyquarry.com/app/search",
            "requested_browser_engines": ["chromium"],
            "release_identity": {
                "status": "pass",
                "version_url": "https://propertyquarry.com/version",
                "status_code": 200,
                "tls_verified": True,
                "expected": dict(release_identity),
                "observed": {
                    **release_identity,
                    "manifest_status": "complete",
                    "manifest_sha256": _PERFORMANCE_MANIFEST_SHA256,
                    "replica_id": "propertyquarry-production-7d9c8f7cc8-abcde",
                },
                "matches_expected": True,
                "error": "",
                "credential_persisted": False,
            },
            "engine_rows": [
                {
                    "status": "pass",
                    "browser_engine": "chromium",
                    "identity": dict(
                        executable_identity or _PYTHON_EXECUTABLE_IDENTITY
                    ),
                    "launch_binding": {
                        "mechanism": "playwright_explicit_executable_path",
                        "executable_path": str(
                            (executable_identity or _PYTHON_EXECUTABLE_IDENTITY)[
                                "executable_path"
                            ]
                        ),
                        "executable_sha256": str(
                            (executable_identity or _PYTHON_EXECUTABLE_IDENTITY)[
                                "executable_sha256"
                            ]
                        ),
                        "prelaunch_bytes": int(
                            (executable_identity or _PYTHON_EXECUTABLE_IDENTITY)[
                                "executable_bytes"
                            ]
                        ),
                        "postlaunch_identity_match": True,
                    },
                    "profile_support": {
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
                    },
                    "authentication": {
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
                    },
                    "measurements": {"cold": cold, "warm": warm},
                    "cold_to_warm": {
                        "duration_delta_ms": 1000,
                        "request_count_delta": 20,
                        "transferred_bytes_delta": 360_000,
                    },
                    "limitations": [
                        "Lab navigation evidence only; no field Core Web Vitals or physical-device performance is claimed."
                    ],
                    "field_core_web_vitals_claimed": False,
                    "physical_device_claimed": False,
                }
            ],
            "limitations_by_engine": {},
            "field_core_web_vitals_claimed": False,
            "physical_device_claimed": False,
        },
        "claims": {
            "cold_and_warm_server_request_lab_evidence": True,
            "constrained_browser_lab_evidence": True,
            "signed_release_probe_authentication": True,
            "exact_live_release_identity_observed": True,
            "field_core_web_vitals": False,
            "physical_device_performance": False,
        },
        "notes": [
            "Synthetic unit-test fixture for the independently verified authenticated performance contract."
        ],
    }


def _flagship_performance_test_payload(
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    executable_identity = _secure_test_chromium_executable_identity(tmp_path)
    return (
        _performance_payload(executable_identity=executable_identity),
        executable_identity,
    )


def _evaluate_flagship_performance_payload(
    payload: dict[str, object],
    executable_identity: dict[str, object],
    *,
    expected_public_origin: str = "https://propertyquarry.com",
) -> tuple[bool, dict[str, object]]:
    return gold_status._flagship_authenticated_performance_proof(
        payload,
        expected_public_origin=expected_public_origin,
        expected_release_commit_sha=_PERFORMANCE_RELEASE_COMMIT_SHA,
        expected_release_image_digest=_PERFORMANCE_RELEASE_IMAGE_DIGEST,
        expected_release_deployment_id=_PERFORMANCE_RELEASE_DEPLOYMENT_ID,
        expected_release_manifest_sha256=_PERFORMANCE_MANIFEST_SHA256,
        expected_chromium_executable_path=str(
            executable_identity["executable_path"]
        ),
        expected_chromium_executable_sha256=str(
            executable_identity["executable_sha256"]
        ),
    )


def test_flagship_performance_proof_independently_validates_exact_v2_evidence(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is True
    assert proof["errors"] == []
    assert proof["target_origin_matches_expected"] is True
    assert proof["field_core_web_vitals_claimed"] is False
    assert proof["physical_device_claimed"] is False


def test_flagship_performance_proof_rejects_unbound_browser_launch(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "launch_binding"
    ]["mechanism"] = "playwright_default_discovery"

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_explicit_launch_binding_invalid" in proof["errors"]


def test_flagship_performance_proof_rejects_tiny_chromium_marker_file(
    tmp_path: Path,
) -> None:
    chromium_dir = tmp_path / "controller-browser" / "chromium-tiny"
    chromium_dir.mkdir(mode=0o700, parents=True)
    executable_path = chromium_dir / "chrome"
    executable_bytes = b"\x7fELF" + b"Chromium"
    executable_path.write_bytes(executable_bytes)
    executable_path.chmod(0o700)
    executable_identity = {
        "engine": "chromium",
        "browser_version": "145.0.7632.6",
        "playwright_version": "1.60.0",
        "executable_path": str(executable_path),
        "executable_sha256": hashlib.sha256(executable_bytes).hexdigest(),
        "executable_bytes": len(executable_bytes),
    }
    payload = _performance_payload(executable_identity=executable_identity)

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "browser_executable_size_out_of_range" in proof["errors"]


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    (
        (
            lambda payload: payload.pop("schema"),
            "schema_must_be_authenticated_performance_v2",
        ),
        (
            lambda payload: payload.__setitem__("flagship_status", "pass")
            or payload.__setitem__("constrained_client_evidence", {"status": "pass"}),
            "constrained_client_profile_not_exact",
        ),
        (
            lambda payload: payload["claims"].__setitem__(  # type: ignore[index, union-attr]
                "field_core_web_vitals", True
            ),
            "performance_claims_not_exact_or_overclaimed",
        ),
        (
            lambda payload: payload["constrained_client_evidence"][  # type: ignore[index]
                "engine_rows"
            ][0]["identity"].__setitem__("executable_sha256", "claimed-pass"),
            "chromium_identity_invalid",
        ),
        (
            lambda payload: payload["constrained_client_evidence"][  # type: ignore[index]
                "engine_rows"
            ][0]["profile_support"]["network_throttling"].__setitem__(
                "applied", False
            ),
            "chromium_profile_controls_not_exactly_applied",
        ),
        (
            lambda payload: payload["constrained_client_evidence"][  # type: ignore[index]
                "engine_rows"
            ][0]["measurements"].pop("warm"),
            "chromium_cold_warm_measurements_missing",
        ),
    ),
)
def test_flagship_performance_proof_rejects_status_only_and_malformed_claims(
    tmp_path: Path,
    mutation: object,
    expected_error: str,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    assert callable(mutation)
    mutation(payload)

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert expected_error in proof["errors"]


def test_flagship_performance_proof_rejects_bool_impersonating_integer(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    payload["routes"][0]["attempt_count"] = True  # type: ignore[index]

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "server_route_0_cold_warm_status_invalid" in proof["errors"]


def test_flagship_performance_proof_rejects_self_declared_sixty_second_budgets(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    payload["thresholds"] = {
        "warm_route_budget_ms": 60_000,
        "cold_route_budget_ms": 60_000,
    }
    for route in payload["routes"]:  # type: ignore[union-attr]
        route["budget_ms"] = 60_000
        route["cold_budget_ms"] = 60_000
        route["measurements"]["cold"]["budget_ms"] = 60_000
        route["measurements"]["warm"]["budget_ms"] = 60_000

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "flagship_server_thresholds_not_fixed" in proof["errors"]
    assert "server_route_0_cold_warm_status_invalid" in proof["errors"]


def test_flagship_performance_proof_rehashes_browser_executable(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    identity = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "identity"
    ]
    reported_digest = str(identity["executable_sha256"])
    identity["executable_sha256"] = (
        ("0" if reported_digest[0] != "0" else "1") + reported_digest[1:]
    )

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_executable_identity_mismatch" in proof["errors"]


def test_flagship_performance_proof_rejects_observed_release_identity_mismatch(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    payload["constrained_client_evidence"]["release_identity"]["observed"][  # type: ignore[index]
        "deployment_id"
    ] = "propertyquarry-other-production"

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "constrained_release_identity_not_exact" in proof["errors"]


def test_flagship_performance_proof_rejects_stale_cold_replica_release(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    cold_identity = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]["cold"]["document_release_identity"]
    cold_identity["commit_sha"] = "d" * 40

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_cold_document_release_identity_invalid" in proof["errors"]


def test_flagship_performance_proof_rejects_wrong_warm_document_deployment(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    warm_identity = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]["warm"]["document_release_identity"]
    warm_identity["deployment_id"] = "propertyquarry-stale-deployment"

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_warm_document_release_identity_invalid" in proof["errors"]


def test_flagship_performance_proof_rejects_bool_document_replica_id(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    cold_identity = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]["cold"]["document_release_identity"]
    cold_identity["replica_id"] = True

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_cold_document_release_identity_invalid" in proof["errors"]


def test_flagship_performance_proof_rejects_document_version_manifest_mismatch(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    warm_identity = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]["warm"]["document_release_identity"]
    warm_identity["manifest_sha256"] = "d" * 64

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_warm_document_release_identity_invalid" in proof["errors"]


def test_flagship_performance_proof_rejects_mutually_consistent_unattested_manifest(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    constrained = payload["constrained_client_evidence"]
    constrained["release_identity"]["observed"]["manifest_sha256"] = "d" * 64
    for phase in ("cold", "warm"):
        constrained["engine_rows"][0]["measurements"][phase][
            "document_release_identity"
        ]["manifest_sha256"] = "d" * 64

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "constrained_release_identity_not_exact" in proof["errors"]
    assert proof["expected_release_identity"]["manifest_sha256"] == (
        _PERFORMANCE_MANIFEST_SHA256
    )
    assert proof["observed_release_identity"]["manifest_sha256"] == "d" * 64


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("cache_control", "public, max-age=300"),
        ("acknowledged_nonce_sha256", "d123456789abcdef" * 4),
        ("expected_nonce_sha256", True),
    ),
)
def test_flagship_performance_proof_rejects_forged_document_auth_binding(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    binding = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]["cold"]["document_authentication_binding"]
    binding[field] = value

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert (
        "chromium_cold_document_authentication_binding_invalid"
        in proof["errors"]
    )


def test_flagship_performance_proof_rejects_replayed_nonce_ack_between_phases(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    measurements = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]
    cold_hash = measurements["cold"]["document_authentication_binding"][
        "acknowledged_nonce_sha256"
    ]
    measurements["warm"]["document_authentication_binding"] = {
        "cache_control": "no-store",
        "expected_nonce_sha256": cold_hash,
        "acknowledged_nonce_sha256": cold_hash,
    }

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_probe_nonce_bindings_not_distinct" in proof["errors"]


def test_flagship_performance_proof_requires_observed_warm_http_cache_reuse(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    measurements = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]
    measurements["warm"]["subresource_cache_hit_count"] = 0

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_warm_http_cache_reuse_not_observed" in proof["errors"]


def test_flagship_performance_proof_requires_warm_transfer_reduction(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    measurements = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]
    measurements["warm"]["transferred_bytes"] = measurements["cold"][
        "transferred_bytes"
    ]

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_warm_http_cache_reuse_not_observed" in proof["errors"]


def test_flagship_performance_proof_rejects_redirect_away_from_exact_target(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    cold = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]["cold"]
    cold["status_code"] = 302
    cold["final_url"] = "https://propertyquarry.com/sign-in"

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_cold_measurement_thresholds_invalid" in proof["errors"]


def test_flagship_performance_proof_rejects_python_binary_as_chromium(
    tmp_path: Path,
) -> None:
    executable_identity = _secure_test_chromium_executable_identity(tmp_path)
    chromium_path = Path(str(executable_identity["executable_path"]))
    python_path = chromium_path.with_name("python3")
    chromium_path.rename(python_path)
    executable_identity["executable_path"] = str(python_path)
    payload = _performance_payload(executable_identity=executable_identity)

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "browser_executable_path_not_chromium" in proof["errors"]


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    (
        ("resource_type", "ServiceWorker"),
        ("cache_source", "service_worker"),
    ),
)
def test_flagship_performance_proof_rejects_unsafe_waterfall_classification(
    tmp_path: Path,
    field: str,
    invalid_value: str,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    resource = payload["constrained_client_evidence"]["engine_rows"][0][  # type: ignore[index]
        "measurements"
    ]["cold"]["slowest_resources"][0]
    resource[field] = invalid_value

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
    )

    assert ready is False
    assert "chromium_cold_waterfall_resource_invalid" in proof["errors"]


def test_flagship_performance_proof_rejects_loopback_as_public_origin(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)
    loopback_origin = "https://127.0.0.1"
    constrained = payload["constrained_client_evidence"]  # type: ignore[assignment]
    constrained["target"] = f"{loopback_origin}/app/search"
    constrained["release_identity"]["version_url"] = f"{loopback_origin}/version"
    for measurement in constrained["engine_rows"][0]["measurements"].values():
        measurement["final_url"] = f"{loopback_origin}/app/search"
        measurement["slowest_resources"][0]["url"] = (
            f"{loopback_origin}/app/search"
        )

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
        expected_public_origin=loopback_origin,
    )

    assert ready is False
    assert "constrained_client_target_origin_mismatch" in proof["errors"]


@pytest.mark.parametrize(
    "invalid_origin",
    (
        "https://propertyquarry.com/",
        "https://propertyquarry.com/ignored?x=1",
        "https://propertyquarry.com?x=1",
        "https://propertyquarry.com#fragment",
        "https://user@propertyquarry.com",
        "https://propertyquarry.com:443",
        "https://PropertyQuarry.com",
        "https://127.0.0.2",
        "https://10.0.0.1",
        "https://169.254.1.1",
        "https://[::1]",
        "https://foo.localhost",
        "https://propertyquarry.local",
        "https://propertyquarry.example.com",
        "https://service.internal",
        "https://flagship.onion",
        "https://release.corp",
        "https://a.b",
        "https://service.notarealtld",
        "https://co.uk",
        "https://propertyquarry.co",
        "https://propertyquarry.com.evil",
        "https://evilpropertyquarry.com",
        "https://propertyquarry-com.com",
    ),
)
def test_performance_public_origin_requires_exact_bare_public_dns_origin(
    invalid_origin: str,
) -> None:
    assert gold_status._exact_public_https_origin(invalid_origin) is None


def test_performance_public_origin_accepts_exact_bare_public_dns_origin() -> None:
    assert gold_status._exact_public_https_origin(
        "https://propertyquarry.com"
    ) == ("https", "propertyquarry.com", 443)
    assert gold_status._exact_public_https_origin(
        "https://eu.propertyquarry.com"
    ) == ("https", "eu.propertyquarry.com", 443)


def test_flagship_performance_proof_rejects_ignored_expected_origin_path(
    tmp_path: Path,
) -> None:
    payload, executable_identity = _flagship_performance_test_payload(tmp_path)

    ready, proof = _evaluate_flagship_performance_payload(
        payload,
        executable_identity,
        expected_public_origin="https://propertyquarry.com/ignored?x=1",
    )

    assert ready is False
    assert "constrained_client_target_origin_mismatch" in proof["errors"]


def _billing_payload(*, host_resolves: bool = True, status: str = "disabled") -> dict[str, object]:
    return {
        "status": status,
        "error": "" if host_resolves and status != "blocked" else "billing_handoff_host_unresolved:gaierror",
        "billing_handoff": {
            "configured": True,
            "url": "https://billing.propertyquarry.com/account",
            "host": "billing.propertyquarry.com",
            "host_resolves": host_resolves,
            "error": "" if host_resolves else "billing_handoff_host_unresolved:gaierror",
            "required_dns_record": {
                "name": "billing.propertyquarry.com",
                "type": "CNAME",
                "target": "members.brilliantdirectories.com",
                "purpose": "make /app/billing redirect only to a resolving HTTPS white-label account lane",
            },
            "next_action": "keep the resolving HTTPS billing handoff under the allowlisted white-label host"
            if host_resolves
            else "create DNS for billing.propertyquarry.com before enabling the Brilliant Directories billing handoff",
        },
    }


def _billing_bridge_payload() -> dict[str, object]:
    payload = _billing_payload(host_resolves=True, status="dry_verified_configured")
    payload["billing_handoff"]["account_handoff_usable"] = False
    payload["billing_handoff"]["account_handoff_error"] = "billing_handoff_requires_separate_login"
    payload["billing_handoff"]["pricing_surface_probe"] = {
        "pricing_url": "https://billing.propertyquarry.com/join",
        "configured": True,
        "status_code": 302,
        "placeholder": False,
        "placeholder_hits": [],
        "error": "",
        "title": "",
    }
    payload["billing_sso_bridge"] = {
        "enabled": True,
        "configured": True,
        "ready": True,
        "config_ready": True,
        "url": "https://billing.propertyquarry.com/sso/propertyquarry",
        "host": "billing.propertyquarry.com",
        "host_resolves": True,
        "exchange_checked": True,
        "exchange_usable": True,
        "exchange_probe": {
            "checked": True,
            "usable": True,
            "status_code": 200,
            "final_host": "billing.propertyquarry.com",
            "final_path": "/account",
            "redirected_to_login": False,
            "error": "",
        },
        "error": "",
    }
    payload["member_login_token_handoff"] = {
        "enabled": False,
        "configured": False,
        "ready": False,
        "error": "",
        "next_action": (
            "generate a Brilliant Directories API key in the admin backend, confirm the member-login token account lane, "
            "then set PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY, PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY_HEADER, "
            "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_MEMBER_LOGIN_TOKEN_ENABLED=1, and "
            "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_MEMBER_LOGIN_TOKEN_SECRET before using a member-session handoff"
        ),
    }
    return payload


def _billing_member_token_payload() -> dict[str, object]:
    payload = _billing_bridge_payload()
    payload["billing_sso_bridge"].update(
        {
            "ready": False,
            "exchange_usable": False,
            "exchange_probe": {
                "checked": True,
                "usable": False,
                "status_code": 200,
                "final_host": "billing.propertyquarry.com",
                "final_path": "/login",
                "redirected_to_login": True,
                "error": "billing_sso_bridge_exchange_requires_login",
            },
            "error": "billing_sso_bridge_exchange_requires_login",
        }
    )
    payload["member_login_token_handoff"] = {
        "enabled": True,
        "configured": True,
        "ready": True,
        "host": "billing.propertyquarry.com",
        "error": "",
        "next_action": "",
    }
    return payload


def _tour_provider_ownership_payload() -> dict[str, object]:
    return {
        "status": "pass",
        "missing_providers": [],
        "providers": {
            "3dvista": {"status": "owned_configured", "export_verified": False},
            "pano2vr": {"status": "owned_configured", "export_verified": False},
        },
    }


def _security_posture_payload(*, status: str = "pass") -> dict[str, object]:
    failures = [] if status == "pass" else ["ea/Dockerfile.property must run as USER ea"]
    return {
        "schema": "propertyquarry.security_posture_receipt.v1",
        "generated_at": "2026-06-29T10:09:00Z",
        "status": status,
        "required_checks": ["non_root_pinned_runtime_image"],
        "failure_count": len(failures),
        "failures": failures,
    }


def _release_hygiene_payload(*, status: str = "pass", tracked_dirty_path_count: int = 0) -> dict[str, object]:
    failures = [] if status == "pass" else ["release manifest runtime commit does not match current HEAD or deployed parent"]
    return {
        "schema": "propertyquarry.release_hygiene_receipt.v1",
        "status": status,
        "required_checks": [
            "release_manifest_runtime_commit_matches_head_or_parent",
            "tracked_worktree_clean",
            "no_untracked_release_source_files",
        ],
        "failure_count": len(failures),
        "failures": failures,
        "manifest_runtime_commit": "d8426c7",
        "head_commit": "88cdc13",
        "parent_commit": "6d80515",
        "tracked_dirty_path_count": tracked_dirty_path_count,
    }


def _furniture_style_contract_payload(*, status: str = "pass") -> dict[str, object]:
    failures = [] if status == "pass" else ["furniture style catalog missing value urban_jungle"]
    return {
        "schema": "propertyquarry.furniture_style_contract_receipt.v2",
        "status": status,
        "style_count": 5 if status == "pass" else 4,
        "style_values": ["gilded_penthouse", "ikea_practical", "landhaus", "urban_jungle", "warm_scandi"],
        "plan_caps": {"free": 5, "plus": 5, "agent": 5},
        "helper_plan_caps": {"free": 5, "plus": 5, "agent": 5},
        "availability_mode": "per_visual_request",
        "pricing_surface_bound": True,
        "failure_count": len(failures),
        "failures": failures,
    }


def _bts_methodology_contract_payload(*, status: str = "pass") -> dict[str, object]:
    failures = [] if status == "pass" else ["selected-district location row must stay +0"]
    return {
        "schema": "propertyquarry.bts_methodology_contract_receipt.v1",
        "status": status,
        "language_count": 8,
        "languages": ["de", "en", "es", "fr", "it", "nl", "pl", "pt"],
        "source_section_count": 5 if status == "pass" else 4,
        "failure_count": len(failures),
        "failures": failures,
    }


def _tour_delivery_contract_payload(*, status: str = "pass") -> dict[str, object]:
    failures = [] if status == "pass" else ["Matterport must remain a first-class ready provider mode"]
    return {
        "schema": "propertyquarry.tour_delivery_contract_shape_receipt.v1",
        "status": status,
        "required_provider_modes": ["matterport", "3dvista", "magicfit"],
        "optional_provider_modes": ["pano2vr", "krpano"],
        "ready_provider_modes": ["3dvista", "krpano", "magicfit", "matterport", "pano2vr"] if status == "pass" else ["krpano", "magicfit", "pano2vr"],
        "missing_provider_modes": [] if status == "pass" else ["3dvista", "matterport"],
        "matterport_ready_count": 29 if status == "pass" else 0,
        "failure_count": len(failures),
        "failures": failures,
    }


def _browser_3d_gate_payload(*, status: str = "pass") -> dict[str, object]:
    failing = status != "pass"
    checks: list[dict[str, object]] = [
        {"name": "matterport_rendered_viewer", "ok": True},
        {
            "name": "3dvista_rendered_viewer",
            "ok": not failing,
            "state": {
                "provider_frame_url": "https://propertyquarry.com/tours/demo/3dvista/index.html",
                "visible_canvas_count": 1,
                "frame_text": "Loading virtual tour. Please wait..." if failing else "",
            },
        },
        {"name": "pano2vr_rendered_viewer", "ok": True},
    ]
    return {
        "contract_name": "propertyquarry.3d_browser_gate.v1",
        "generated_at": "2026-06-29T10:00:00Z",
        "status": status,
        "providers": ["3dvista", "pano2vr", "matterport"],
        "failed_count": 1 if failing else 0,
        "checks": checks,
        "provider_results": [
            {"provider": "3dvista", "status": "fail" if failing else "pass"},
            {"provider": "pano2vr", "status": "pass"},
            {"provider": "matterport", "status": "pass"},
        ],
    }


def _walkthrough_quality_gate_payload(*, status: str = "pass") -> dict[str, object]:
    failing = status != "pass"
    slug = "magicfit-proof-tour"
    video_relpath = "magicfit-walkthrough.mp4"
    video_sha256 = "a" * 64
    checks: list[dict[str, object]] = [
        {"name": "walkthrough_video_file_present", "ok": True},
        {
            "name": "walkthrough_duration_floor",
            "ok": not failing,
            "duration_seconds": 15.104 if failing else 45.0,
            "min_duration_seconds": 30.0,
        },
        {
            "name": "walkthrough_room_coverage_complete",
            "ok": not failing,
            "coverage": {
                "status": "fail" if failing else "pass",
                "rooms_expected": ["bedroom", "kitchen", "living"],
                "rooms_visited": ["kitchen"] if failing else ["bedroom", "kitchen", "living"],
                "missing_rooms": ["bedroom", "living"] if failing else [],
                "room_segment_count": 1 if failing else 3,
            },
        },
        {
            "name": "walkthrough_frame_jump_limit",
            "ok": not failing,
            "frame_delta_stats": {
                "ok": True,
                "max_delta": 60.064 if failing else 18.2,
                "sampled_frame_count": 30,
            },
        },
    ]
    return {
        "contract_name": "propertyquarry.walkthrough_quality_gate.v1",
        "generated_at": "2026-06-29T10:01:00Z",
        "status": status,
        "demo_slug": slug,
        "video_relpath": video_relpath,
        "video_sha256": video_sha256,
        "provider_proof_receipt_path": "walkthrough-provider-proof.json",
        "provider_media_binding": {
            "provider": "magicfit",
            "bundle_slug": slug,
            "video_relpath": video_relpath,
            "bundle_media_path": f"{slug}/{video_relpath}",
            "video_sha256": video_sha256,
        },
        "failed_count": 3 if failing else 0,
        "checks": checks,
    }


def _walkthrough_provider_proof_payload(*, status: str = "pass") -> dict[str, object]:
    passing = status == "pass"
    video_sha256 = "a" * 64
    verified_providers = ["magicfit", "omagic"] if passing else ["magicfit"]
    verified_orchestrators = ["ea"] if passing else []
    return {
        "contract_name": "propertyquarry.walkthrough_provider_proof_gate.v1",
        "generated_at": "2026-06-29T10:01:30Z",
        "status": status,
        "required_providers": ["magicfit", "omagic"],
        "verified_providers": verified_providers,
        "verified_orchestrators": verified_orchestrators,
        "indexed_participants": ["ea", "magicfit", "omagic"],
        "provenance_index": [
            {
                "key": "ea",
                "kind": "orchestrator",
                "role": "governance_and_verification",
                "status": "pass" if passing else "fail",
                "media_authorship": False,
                "evidence_contract": "propertyquarry.walkthrough_provider_proof_gate.v1",
            },
            {
                "key": "magicfit",
                "kind": "media_provider",
                "role": "walkthrough_media_provider",
                "status": "pass",
                "media_authorship": True,
                "evidence_bundle_slug": "magicfit-proof-tour",
                "evidence_video_relpath": "magicfit-walkthrough.mp4",
                "evidence_video_sha256": video_sha256,
            },
            {
                "key": "omagic",
                "kind": "media_provider",
                "role": "walkthrough_media_provider",
                "status": "pass" if passing else "fail",
                "media_authorship": True,
                "evidence_bundle_slug": "omagic-proof-tour" if passing else "",
                "evidence_video_relpath": "omagic-walkthrough.mp4" if passing else "",
                "evidence_video_sha256": video_sha256 if passing else "",
            },
        ],
        "missing_providers": [] if passing else ["omagic"],
        "failed_count": 0 if passing else 1,
        "provider_results": [
            {
                "provider": "magicfit",
                "status": "pass",
                "slug": "magicfit-proof-tour",
                "video_relpath": "magicfit-walkthrough.mp4",
                "video_sha256": video_sha256,
                "failed_count": 0,
            },
            {
                "provider": "omagic",
                "status": "pass" if passing else "fail",
                "slug": "omagic-proof-tour" if passing else "",
                "video_relpath": "omagic-walkthrough.mp4" if passing else "",
                "video_sha256": video_sha256 if passing else "",
                "failed_count": 0 if passing else 1,
            },
        ],
    }


def test_gold_status_walkthrough_provider_proof_requires_truthful_ea_index() -> None:
    payload = _walkthrough_provider_proof_payload()

    assert gold_status._walkthrough_provider_proof_receipt_ok(payload) is True

    without_ea = {**payload, "verified_orchestrators": []}
    assert gold_status._walkthrough_provider_proof_receipt_ok(without_ea) is False

    false_authorship = json.loads(json.dumps(payload))
    false_authorship["provenance_index"][0]["media_authorship"] = True
    assert gold_status._walkthrough_provider_proof_receipt_ok(false_authorship) is False

    false_media_evidence = json.loads(json.dumps(payload))
    false_media_evidence["provenance_index"][0]["evidence_sidecar_path"] = (
        "/tmp/tour.magicfit.json"
    )
    assert gold_status._walkthrough_provider_proof_receipt_ok(false_media_evidence) is False

    missing_index = {**payload, "provenance_index": []}
    assert gold_status._walkthrough_provider_proof_receipt_ok(missing_index) is False


def test_gold_status_walkthrough_quality_binding_matches_provider_proof() -> None:
    quality = _walkthrough_quality_gate_payload()
    provider = _walkthrough_provider_proof_payload()

    ok, details = gold_status._walkthrough_quality_provider_binding_status(
        quality,
        provider,
    )

    assert ok is True
    assert details["status"] == "pass"

    digest_mismatch = json.loads(json.dumps(quality))
    digest_mismatch["provider_media_binding"]["video_sha256"] = "b" * 64
    mismatch_ok, mismatch_details = (
        gold_status._walkthrough_quality_provider_binding_status(
            digest_mismatch,
            provider,
        )
    )
    assert mismatch_ok is False
    assert mismatch_details["checks"]["quality_binding_matches_provider_result"] is False

    legacy_quality = json.loads(json.dumps(quality))
    legacy_quality.pop("provider_media_binding")
    legacy_quality.pop("video_sha256")
    legacy_ok, legacy_details = gold_status._walkthrough_quality_provider_binding_status(
        legacy_quality,
        provider,
    )
    assert legacy_ok is False
    assert legacy_details["checks"]["quality_binding_matches_provider_result"] is False
    assert legacy_details["checks"]["quality_top_level_identity_matches"] is False


def test_gold_status_blocks_walkthrough_quality_digest_mismatch(
    tmp_path: Path,
) -> None:
    quality_payload = _walkthrough_quality_gate_payload()
    quality_payload["provider_media_binding"]["video_sha256"] = "b" * 64
    quality = _write_json(tmp_path / "walkthrough-quality.json", quality_payload)
    provider = _write_json(
        tmp_path / "walkthrough-provider-proof.json",
        _walkthrough_provider_proof_payload(),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=_write_json(tmp_path / "performance.json", {}),
        tour_control_receipt_path=_write_json(tmp_path / "tour-controls.json", {}),
        export_discovery_receipt_path=_write_json(tmp_path / "discovery.json", {}),
        repair_canary_receipt_path=_write_json(tmp_path / "repair.json", {}),
        provider_matrix_receipt_path=_write_json(tmp_path / "provider-matrix.json", {}),
        walkthrough_quality_receipt_path=quality,
        walkthrough_provider_proof_receipt_path=provider,
        claim_scope="advanced_visual",
    )

    blocker = next(
        row for row in receipt["blockers"] if row["area"] == "walkthrough_quality"
    )
    assert receipt["status"] == "blocked"
    assert receipt["walkthrough_quality"]["ready"] is False
    assert (
        blocker["provider_binding"]["checks"][
            "quality_binding_matches_provider_result"
        ]
        is False
    )


def _runtime_reconstruction_payload(
    *,
    status: str = "pass",
    glb: bool = True,
    browser_shell: bool = True,
    public_contract: bool = True,
    required_paths: bool = True,
    route_label_quality: bool = True,
    walkthrough_label_quality: bool = True,
    walkthrough_generated: bool = True,
    walkthrough_status: str = "pass",
    honest_disclosure: bool = True,
    browser_shell_status: str | None = None,
    browser_failures: list[str] | None = None,
    public_failures: list[str] | None = None,
) -> dict[str, object]:
    glb_size = 30700 if glb else 0
    normalized_browser_failures = list(browser_failures or ([] if browser_shell else ["layout_preview_heading_wrong"]))
    normalized_public_failures = list(public_failures or ([] if public_contract else ["viewer_not_redirected"]))
    normalized_browser_shell_status = str(
        browser_shell_status if browser_shell_status is not None else ("pass" if browser_shell else "failed")
    )
    return {
        "contract_name": "propertyquarry.runtime_reconstruction_smoke.v1",
        "generated_at": "2026-06-29T10:02:00Z",
        "status": status,
        "glb_required": glb,
        "glb_non_empty": glb,
        "glb_manifest_ok": glb,
        "glb_capability_ok": True,
        "required_paths_ok": required_paths,
        "route_label_quality_ok": route_label_quality,
        "walkthrough_label_quality_ok": walkthrough_label_quality,
        "walkthrough_generated_ok": walkthrough_generated,
        "honest_disclosure_ok": honest_disclosure,
        "browser_shell_ok": browser_shell,
        "public_route_contract_ok": public_contract,
        "viewer_url": "https://propertyquarry.com/tours/files/demo/generated-reconstruction/viewer.html",
        "details": {
            "glb_export_status": "generated" if glb else "failed",
            "paths": {"glb": {"size_bytes": glb_size}},
            "walkthrough_status": walkthrough_status,
        },
        "browser_shell": {
            "status": normalized_browser_shell_status,
            "failures": normalized_browser_failures,
        },
        "public_route_contract": {
            "status": "pass" if public_contract else "failed",
            "failures": normalized_public_failures,
        },
    }


def _service_generated_reconstruction_payload(
    *,
    status: str = "pass",
    browser_shell: bool = True,
    required_paths: bool = True,
    top_level_video_contract: bool = True,
    route_label_quality: bool = True,
    walkthrough_generated: bool = True,
    delivery_contract: bool = True,
    public_contract: bool = True,
) -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.service_generated_reconstruction_smoke.v1",
        "generated_at": "2026-06-29T10:03:00Z",
        "status": status,
        "browser_shell_ok": browser_shell,
        "required_paths_ok": required_paths,
        "top_level_video_contract_ok": top_level_video_contract,
        "route_label_quality_ok": route_label_quality,
        "walkthrough_generated_ok": walkthrough_generated,
        "delivery_contract_ok": delivery_contract,
        "public_route_contract_ok": public_contract,
        "viewer_url": "https://propertyquarry.com/tours/demo-generated-reconstruction",
        "browser_shell": {
            "status": "pass" if browser_shell else "failed",
            "failures": [] if browser_shell else ["launch_shell_media_grid_map_label_present"],
        },
    }


def _scene_video_readiness_payload(*, blocked: bool = False) -> dict[str, object]:
    if blocked:
        return {
            "contract_name": "propertyquarry.scene_video_readiness.v1",
            "generated_at": "2026-06-29T10:05:00Z",
            "summary": {
                "provider_count": 5,
                "ready_count": 2,
                "blocked_count": 3,
                "blocked_providers": ["magicfit", "magic", "omagic"],
            },
            "telegram_delivery_readiness": {"status": "ready", "blockers": []},
            "next_actions": [
                {"provider": "magicfit", "reason": "provider_account_visibility_gap", "do_not_touch": ["ONEMIN_*"]},
                {"provider": "omagic", "reason": "omagic_credentials_missing", "do_not_touch": ["ONEMIN_*"]},
            ],
        }
    return {
        "contract_name": "propertyquarry.scene_video_readiness.v1",
        "generated_at": "2026-06-29T10:05:00Z",
        "summary": {"provider_count": 5, "ready_count": 5, "blocked_count": 0, "blocked_providers": []},
        "telegram_delivery_readiness": {"status": "ready", "blockers": []},
        "next_actions": [],
    }


def _scene_video_readiness_verifier_payload(*, status: str = "pass") -> dict[str, object]:
    return {
        "generated_at": "2026-06-29T10:06:00Z",
        "status": status,
        "blockers": [] if status == "pass" else ["magic_backend_mismatch"],
        "checked_providers": ["mootion", "magicfit", "magic", "omagic", "onemin_i2v"],
        "provider_count": 5,
    }


def _scene_video_runtime_status_payload(*, blocked: bool = False) -> dict[str, object]:
    if blocked:
        return {
            "contract_name": "propertyquarry.scene_video_runtime_status.v1",
            "generated_at": "2026-06-29T10:05:30Z",
            "source_kind": "receipt_file",
            "source_ref": "/tmp/scene-video-readiness.json",
            "summary": {
                "provider_count": 5,
                "ready_count": 2,
                "blocked_count": 3,
                "blocked_providers": ["magicfit", "magic", "omagic"],
                "action_required_count": 3,
                "action_required_providers": ["magicfit", "magic", "omagic"],
                "delivery_ready": True,
            },
            "providers": [
                {
                    "provider": "mootion",
                    "provider_key": "mootion",
                    "status": "ready",
                    "ready": True,
                    "attention_required": False,
                    "execution_lane": "browseract_remote",
                },
                {
                    "provider": "magicfit",
                    "provider_key": "magicfit",
                    "provider_backend_key": "magicfit",
                    "status": "blocked",
                    "ready": False,
                    "attention_required": True,
                    "execution_lane": "magicfit",
                    "runtime_account_count": 0,
                    "expected_account_count": 3,
                    "visible_account_gap": 3,
                    "credit_state": "unverified",
                    "blocking_reason": "magicfit_credentials_missing",
                    "blockers": ["magicfit_credentials_missing"],
                    "next_action": "refresh visible MagicFit accounts before claiming provider parity",
                    "next_action_reason": "provider_account_visibility_gap",
                    "next_action_severity": "high",
                },
                {
                    "provider": "magic",
                    "provider_key": "magic",
                    "provider_backend_key": "omagic",
                    "status": "blocked",
                    "ready": False,
                    "attention_required": True,
                    "execution_lane": "omagic",
                    "runtime_account_count": 0,
                    "expected_account_count": 8,
                    "visible_account_gap": 8,
                    "blocking_reason": "omagic_model_upload_adapter_disabled",
                    "blockers": ["omagic_model_upload_adapter_disabled"],
                    "next_action": "expose shared OMagic/Magic accounts to the runtime",
                    "next_action_reason": "provider_account_visibility_gap",
                    "next_action_severity": "high",
                },
                {
                    "provider": "omagic",
                    "provider_key": "omagic",
                    "provider_backend_key": "omagic",
                    "status": "blocked",
                    "ready": False,
                    "attention_required": True,
                    "execution_lane": "omagic",
                    "runtime_account_count": 0,
                    "expected_account_count": 8,
                    "visible_account_gap": 8,
                    "blocking_reason": "omagic_model_upload_adapter_disabled",
                    "blockers": ["omagic_model_upload_adapter_disabled"],
                    "next_action": "configure OMagic credentials before enabling the adapter",
                    "next_action_reason": "omagic_credentials_missing",
                    "next_action_severity": "high",
                },
                {
                    "provider": "onemin_i2v",
                    "provider_key": "onemin_i2v",
                    "status": "ready",
                    "ready": True,
                    "attention_required": False,
                    "execution_lane": "onemin_i2v",
                    "credit_state": "funded",
                },
            ],
            "delivery": {
                "transport": "telegram",
                "status": "ready",
                "configured": True,
                "blockers": [],
            },
        }
    return {
        "contract_name": "propertyquarry.scene_video_runtime_status.v1",
        "generated_at": "2026-06-29T10:05:30Z",
        "source_kind": "receipt_file",
        "source_ref": "/tmp/scene-video-readiness.json",
        "summary": {
            "provider_count": 5,
            "ready_count": 5,
            "blocked_count": 0,
            "blocked_providers": [],
            "action_required_count": 0,
            "action_required_providers": [],
            "delivery_ready": True,
        },
        "providers": [
            {"provider": "mootion", "provider_key": "mootion", "status": "ready", "ready": True},
            {
                "provider": "magicfit",
                "provider_key": "magicfit",
                "status": "ready",
                "ready": True,
                "attention_required": False,
                "runtime_account_count": 1,
                "expected_account_count": 1,
                "visible_account_gap": 0,
                "credit_state": "funded",
            },
            {
                "provider": "magic",
                "provider_key": "magic",
                "status": "ready",
                "ready": True,
                "attention_required": False,
                "runtime_account_count": 1,
                "expected_account_count": 1,
                "visible_account_gap": 0,
                "credit_state": "funded",
            },
            {
                "provider": "omagic",
                "provider_key": "omagic",
                "status": "ready",
                "ready": True,
                "attention_required": False,
                "runtime_account_count": 1,
                "expected_account_count": 1,
                "visible_account_gap": 0,
                "credit_state": "funded",
            },
            {"provider": "onemin_i2v", "provider_key": "onemin_i2v", "status": "ready", "ready": True},
        ],
        "delivery": {"transport": "telegram", "status": "ready", "configured": True, "blockers": []},
    }


def _scene_video_provider_refresh_packet_payload() -> dict[str, object]:
    return {
        "contract_name": "propertyquarry.scene_video_provider_refresh_packet.v1",
        "generated_at": "2026-06-29T10:07:00Z",
        "providers": [
            {"provider": "magicfit", "expected_account_count": 3, "runtime_account_count": 1, "visible_account_gap": 2},
            {"provider": "omagic", "aliases": ["magic"], "expected_account_count": 8, "runtime_account_count": 0, "visible_account_gap": 8},
        ],
    }


def _scene_video_provider_refresh_packet_verifier_payload(*, status: str = "pass") -> dict[str, object]:
    return {
        "generated_at": "2026-06-29T10:08:00Z",
        "status": status,
        "blockers": [] if status == "pass" else ["omagic_onemin_boundary_missing"],
        "checked_providers": ["magicfit", "omagic"],
        "provider_count": 2,
    }


def _advanced_visual_binding_fixture(
    tmp_path: Path,
    *,
    walkthrough_quality: Path,
    walkthrough_provider_proof: Path,
    scene_video_readiness: Path,
    scene_video_readiness_verifier: Path,
    scene_video_runtime_status: Path,
    scene_video_provider_refresh_packet: Path,
    scene_video_provider_refresh_packet_verifier: Path,
    privacy: Path,
) -> tuple[Path, datetime, str, str]:
    observed_at = datetime(2026, 6, 29, 11, 0, tzinfo=timezone.utc)
    release_commit_sha = "1" * 40
    release_image_digest = "sha256:" + "2" * 64
    source_schema_fields = {
        walkthrough_quality: (
            "contract_name",
            "propertyquarry.walkthrough_quality_gate.v1",
        ),
        walkthrough_provider_proof: (
            "contract_name",
            "propertyquarry.walkthrough_provider_proof_gate.v1",
        ),
        scene_video_readiness: (
            "contract_name",
            "propertyquarry.scene_video_readiness.v1",
        ),
        scene_video_readiness_verifier: (
            "contract_name",
            "propertyquarry.scene_video_readiness_verifier.v1",
        ),
        scene_video_runtime_status: (
            "contract_name",
            "propertyquarry.scene_video_runtime_status.v1",
        ),
        scene_video_provider_refresh_packet: (
            "contract_name",
            "propertyquarry.scene_video_provider_refresh_packet.v1",
        ),
        scene_video_provider_refresh_packet_verifier: (
            "contract_name",
            "propertyquarry.scene_video_provider_refresh_packet_verifier.v1",
        ),
        privacy: ("schema", "propertyquarry.security_posture_receipt.v1"),
    }
    source_payloads: dict[Path, dict[str, object]] = {}
    for source_path, (schema_field, schema_value) in source_schema_fields.items():
        source_payload = json.loads(source_path.read_text(encoding="utf-8"))
        source_payload[schema_field] = schema_value
        source_payload["release_commit_sha"] = release_commit_sha
        source_payload["image_digest"] = release_image_digest
        source_payloads[source_path] = source_payload
    for source_path in (
        walkthrough_provider_proof,
        scene_video_readiness,
        privacy,
    ):
        _write_json(source_path, source_payloads[source_path])
    source_payloads[walkthrough_quality]["provider_proof_receipt_sha256"] = (
        hashlib.sha256(walkthrough_provider_proof.read_bytes()).hexdigest()
    )
    _write_json(walkthrough_quality, source_payloads[walkthrough_quality])
    readiness_sha = hashlib.sha256(scene_video_readiness.read_bytes()).hexdigest()
    for derived_path in (
        scene_video_readiness_verifier,
        scene_video_runtime_status,
        scene_video_provider_refresh_packet,
    ):
        source_payloads[derived_path]["source_receipt_sha256"] = readiness_sha
        _write_json(derived_path, source_payloads[derived_path])
    source_payloads[scene_video_provider_refresh_packet_verifier][
        "source_packet_sha256"
    ] = hashlib.sha256(scene_video_provider_refresh_packet.read_bytes()).hexdigest()
    _write_json(
        scene_video_provider_refresh_packet_verifier,
        source_payloads[scene_video_provider_refresh_packet_verifier],
    )
    source_paths = {
        "walkthrough_quality": walkthrough_quality,
        "walkthrough_provider_proof": walkthrough_provider_proof,
        "scene_video_readiness": scene_video_readiness,
        "scene_video_readiness_verifier": scene_video_readiness_verifier,
        "scene_video_runtime_status": scene_video_runtime_status,
        "scene_video_provider_refresh_packet": (
            scene_video_provider_refresh_packet
        ),
        "scene_video_provider_refresh_packet_verifier": (
            scene_video_provider_refresh_packet_verifier
        ),
        "privacy": privacy,
    }
    payload = advanced_binding.build_advanced_visual_binding_receipt(
        release_commit_sha=release_commit_sha,
        release_image_digest=release_image_digest,
        source_receipt_paths=source_paths,
        max_age_hours=2,
        now=observed_at,
    )
    assert payload["status"] == "pass", payload["errors"]
    path = _write_json(tmp_path / "advanced-visual-binding.json", payload)
    return path, observed_at, release_commit_sha, release_image_digest


def test_core_gold_does_not_load_mixed_or_advanced_receipts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    core_paths = {
        name: tmp_path / f"core-{name}.json"
        for name in ("performance", "tour", "repair", "provider")
    }
    advanced_paths = {
        name: tmp_path / f"MAGICFIT-OMAGIC-SENTINEL-{name}.json"
        for name in (
            "export_discovery",
            "import_manifest",
            "vendor_tooling",
            "walkthrough_quality",
            "walkthrough_provider_proof",
            "scene_video_readiness",
            "scene_video_readiness_verifier",
            "scene_video_runtime_status",
            "scene_video_provider_refresh_packet",
            "scene_video_provider_refresh_packet_verifier",
            "advanced_visual_binding",
        )
    }
    loaded_paths: list[Path] = []

    def _recording_loader(path: Path) -> dict[str, object]:
        loaded_paths.append(Path(path))
        return {}

    monkeypatch.setattr(gold_status, "_load_json", _recording_loader)
    receipt = build_gold_status_receipt(
        performance_receipt_path=core_paths["performance"],
        tour_control_receipt_path=core_paths["tour"],
        export_discovery_receipt_path=advanced_paths["export_discovery"],
        import_manifest_receipt_path=advanced_paths["import_manifest"],
        repair_canary_receipt_path=core_paths["repair"],
        provider_matrix_receipt_path=core_paths["provider"],
        vendor_tooling_receipt_path=advanced_paths["vendor_tooling"],
        walkthrough_quality_receipt_path=advanced_paths["walkthrough_quality"],
        walkthrough_provider_proof_receipt_path=advanced_paths[
            "walkthrough_provider_proof"
        ],
        scene_video_readiness_receipt_path=advanced_paths[
            "scene_video_readiness"
        ],
        scene_video_readiness_verifier_receipt_path=advanced_paths[
            "scene_video_readiness_verifier"
        ],
        scene_video_runtime_status_receipt_path=advanced_paths[
            "scene_video_runtime_status"
        ],
        scene_video_provider_refresh_packet_path=advanced_paths[
            "scene_video_provider_refresh_packet"
        ],
        scene_video_provider_refresh_packet_verifier_receipt_path=(
            advanced_paths["scene_video_provider_refresh_packet_verifier"]
        ),
        advanced_visual_binding_receipt_path=advanced_paths[
            "advanced_visual_binding"
        ],
        claim_scope="core",
    )

    assert receipt["claim_scope"] == "core"
    assert set(loaded_paths) == set(core_paths.values())
    assert not set(loaded_paths).intersection(advanced_paths.values())
    assert receipt["advanced_visual_gold"]["candidate_binding"][
        "receipt_path"
    ] == ""


def _live_mobile_payload(*, routes: list[str] | None = None, status: str = "pass", failed_count: int = 0) -> dict[str, object]:
    route_list = routes or [
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
        "/app/research/perf-candidate-1020?run_id=run-gold",
        "/app/properties/packets",
        "/app/properties/notifications/preview",
        "/app/support",
        "/app/shortlist/run/run-gold",
        "/tours/tour-gold",
    ]
    return {
        "status": status,
        "failed_count": failed_count,
        "route_count": len(route_list),
        "viewport": {"width": 390, "height": 844},
        "coverage_checks": [
            {
                "name": "research_detail_route_configured",
                "ok": any(str(route).split("?", 1)[0].startswith("/app/research/") for route in route_list),
                "required_route_prefix": "/app/research/",
                "reason": "Gold mobile smoke must exercise a current live research detail page, not only /app/research.",
            },
            {
                "name": "shortlist_run_route_configured",
                "ok": any(str(route).split("?", 1)[0].startswith("/app/shortlist/run/") for route in route_list),
                "required_route_prefix": "/app/shortlist/run/",
                "reason": "Gold mobile smoke must exercise a concrete ranked shortlist run.",
            },
            {
                "name": "public_tour_route_configured",
                "ok": any(
                    str(route).split("?", 1)[0].startswith("/tours/")
                    and str(route).split("?", 1)[0].count("/") == 2
                    for route in route_list
                ),
                "required_route_prefix": "/tours/",
                "reason": "Gold mobile smoke must exercise a concrete first-party tour shell.",
            },
            {
                "name": "registry_mobile_customer_surfaces_covered",
                "ok": True,
                "covered_surface_count": 22,
                "missing_surface_keys": [],
                "reason": "Live mobile smoke routes must cover every customer-visible /app surface declared in the PropertyQuarry surface registry.",
            },
        ],
        "routes": [{"route": route, "ok": True, "checks": []} for route in route_list],
    }


def _public_smoke_payload(*, status: str = "pass", failed_count: int = 0, include_account_creation: bool = True) -> dict[str, object]:
    sign_in_checks = [
        {"name": "sign_in_minimal_copy", "ok": True},
        {"name": "sign_in_connected_identity_creates_account", "ok": include_account_creation},
        {"name": "sign_in_no_unavailable_auth_copy", "ok": True},
        {"name": "sign_in_google_state", "ok": True},
        {"name": "sign_in_google_feedback", "ok": True},
    ]
    return {
        "status": status,
        "failed_count": failed_count,
        "route_count": 22,
        "checks": [
            {
                "path": "/sign-in",
                "ok": status == "pass" and failed_count == 0 and include_account_creation,
                "checks": sign_in_checks,
            }
        ],
    }


def _authenticated_smoke_payload(
    *,
    status: str = "pass",
    failed_count: int = 0,
    billing_external: bool = False,
    billing_fail_closed: bool = True,
    billing_bridge_launch: bool = False,
    billing_internal_account_fallback: bool = False,
    local_board_deleted: bool = True,
    include_notification_checks: bool = True,
) -> dict[str, object]:
    billing_checks = [
        {"name": "billing_local_board_deleted", "ok": local_board_deleted, "detail": "" if local_board_deleted else "billing history, compare plans"},
    ]
    if billing_external:
        billing_checks.append({"name": "billing_external_handoff", "ok": True})
        billing_checks.append({"name": "billing_external_handoff_resolves", "ok": True})
        billing_checks.append({"name": "billing_external_handoff_usable", "ok": True})
        billing_checks.append({"name": "billing_no_second_login", "ok": True})
    if billing_fail_closed:
        billing_checks.append({"name": "billing_fail_closed_recovery", "ok": True})
    if billing_bridge_launch:
        billing_checks.append({"name": "billing_bridge_launch", "ok": True})
    if billing_internal_account_fallback:
        billing_checks.append({"name": "billing_internal_account_fallback", "ok": True})
    notification_checks = [
        {"name": "account_notifications", "ok": True},
        {"name": "account_notification_form", "ok": True},
        {"name": "account_notification_email_channel", "ok": True},
        {"name": "account_notification_telegram_channel", "ok": True},
        {"name": "account_notification_whatsapp_channel", "ok": True},
        {"name": "account_notification_primary_route", "ok": True},
        {"name": "account_notification_whatsapp_phone", "ok": True},
        {"name": "account_notification_save_action", "ok": True},
    ]
    return {
        "status": status,
        "failed_count": failed_count,
        "route_count": 3,
        "checks": [
            {
                "path": "/app/account",
                "status_code": 200,
                "ok": status == "pass" and failed_count == 0 and include_notification_checks,
                "checks": notification_checks if include_notification_checks else notification_checks[:2],
            },
            {
                "path": "/app/billing",
                "status_code": 303 if (billing_external or billing_bridge_launch or billing_internal_account_fallback) else 503,
                "ok": (
                    status == "pass"
                    and failed_count == 0
                    and local_board_deleted
                    and (billing_external or billing_fail_closed or (billing_bridge_launch and billing_internal_account_fallback))
                ),
                "checks": billing_checks,
            }
        ],
    }


def _write_hardened_drop_readmes(tmp_path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    provider_bodies = {
        "3dvista": """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example: python /app/scripts/import_3dvista_export.py --slug demo --export-dir drop/3dvista
Public gold only passes when verify_property_tour_controls reports ready provider modes.
Copy the complete 3DVista export folder into this directory.
The entry must contain tdvplayer.
""",
        "pano2vr": """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example: python /app/scripts/import_pano2vr_export.py --slug demo --export-dir drop/pano2vr
Public gold only passes when verify_property_tour_controls reports ready provider modes.
Copy the complete Pano2VR output folder into this directory.
The entry must contain tour.js.
""",
        "krpano": """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example: python /app/scripts/import_krpano_walkable_scene.py --slug demo --panorama drop/krpano/panorama.jpg
Public gold only passes when verify_property_tour_controls reports ready provider modes.
Copy cube-face-1 through cube-face-6 or a real panorama.
Set KRPANO_LICENSE_DOMAIN=propertyquarry.com before importing.
""",
        "magicfit": """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example: python /app/scripts/import_magicfit_walkthrough.py --slug demo --video-path drop/magicfit/magicfit-walkthrough.mp4 --source-receipt drop/magicfit/magicfit-receipt.json
Public gold only passes when verify_property_tour_controls reports ready provider modes.
Copy magicfit-walkthrough.mp4 and magicfit-receipt.json into this directory.
""",
    }
    for provider, body in provider_bodies.items():
        export_dir = tmp_path / "drop" / provider
        export_dir.mkdir(parents=True, exist_ok=True)
        readme = export_dir / "README.propertyquarry-export.txt"
        readme.write_text(body, encoding="utf-8")
        rows.append({"provider": provider, "export_dir": str(export_dir), "readme": str(readme)})
    return rows


def _import_manifest_payload(tmp_path: Path, *, hardened_readmes: bool = True) -> dict[str, object]:
    providers = ["3dvista", "pano2vr", "krpano", "magicfit"]
    prepared_drop_dirs: list[dict[str, str]]
    if hardened_readmes:
        prepared_drop_dirs = _write_hardened_drop_readmes(tmp_path)
    else:
        prepared_drop_dirs = []
        for provider in providers:
            export_dir = tmp_path / "drop" / provider
            export_dir.mkdir(parents=True, exist_ok=True)
            readme = export_dir / "README.propertyquarry-export.txt"
            readme.write_text("Old placeholder instructions", encoding="utf-8")
            prepared_drop_dirs.append({"provider": provider, "export_dir": str(export_dir), "readme": str(readme)})
    return {
        "status": "waiting_for_verified_assets",
        "import_count": len(providers),
        "providers": providers,
        "drop_status_summary": {"ready_for_import": 0, "waiting_for_assets": len(providers), "other": 0},
        "prepared_drop_dirs": prepared_drop_dirs,
        "next_command": "python /app/scripts/import_property_tour_exports.py --manifest manifest.json",
    }


def test_gold_status_blocks_when_required_tour_provider_modes_are_missing(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 0, "3dvista": 0, "pano2vr": 0, "krpano": 0, "magicfit": 0},
            "ready_provider_modes": [],
            "core_required_provider_modes": ["matterport"],
            "core_missing_provider_modes": ["matterport"],
            "advanced_visual_required_provider_modes": ["magicfit"],
            "advanced_visual_missing_provider_modes": ["magicfit"],
            "missing_provider_modes": ["matterport", "magicfit"],
            "next_required_actions": [{"provider": "magicfit", "action": "import a walkthrough"}],
            "delivery_contracts": {
                "3dvista": {
                    "schema": "propertyquarry.tour_delivery_contract.v1",
                    "status": "blocked",
                    "blocked_reason": "missing_3dvista_export",
                    "required_to_send": ["A verified non-trial 3DVista VT Pro export"],
                    "ready_payload": {"provider": "3dvista", "ready_count": 0, "sample_controls": []},
                }
            },
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {
            "status": "blocked_no_verified_exports",
            "import_count": 0,
            "rejected_count": 1,
            "rejected": [
                {
                    "slug": "family-flat",
                    "provider": "magicfit",
                    "reason": "magicfit_receipt_missing",
                    "action": "copy the matching MagicFit render receipt as magicfit-receipt.json or receipt.json",
                    "drop_layout": "<drop>/<slug>/magicfit/",
                }
            ],
            "repair_count": 1,
            "repair_manifest": [
                {
                    "slug": "family-flat",
                    "provider": "magicfit",
                    "status": "waiting_for_verified_assets",
                    "reason": "magicfit_receipt_missing",
                    "drop_path": "/drop/family-flat/magicfit",
                    "required_action": "copy the matching MagicFit render receipt as magicfit-receipt.json or receipt.json",
                    "import_command_after_assets_arrive": "python /app/scripts/import_magicfit_walkthrough.py --slug family-flat --video-path /drop/family-flat/magicfit/magicfit-walkthrough.mp4 --source-receipt /drop/family-flat/magicfit/magicfit-receipt.json",
                }
            ],
        },
    )
    import_manifest = _write_json(tmp_path / "import-manifest.json", _import_manifest_payload(tmp_path))
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        import_manifest_receipt_path=import_manifest,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        claim_scope="advanced_visual",
    )

    assert receipt["status"] == "blocked"
    assert receipt["performance"]["status"] == "pass"
    assert receipt["self_healing"]["status"] == "pass"
    assert receipt["provider_matrix"]["targeted_search_matrix_executed"] is True
    assert receipt["provider_matrix"]["strict_case_count"] == 121
    assert receipt["provider_matrix"]["soft_filter_case_count"] == 121
    assert receipt["provider_matrix"]["strict_without_soft_filters_ok"] is True
    assert receipt["provider_matrix"]["soft_filters_present_ok"] is True
    assert receipt["provider_matrix"]["dispatch_acceptance_complete"] is True
    assert receipt["provider_matrix"]["status_readback_complete"] is True
    assert receipt["provider_matrix"]["payload_contracts_ok"] is True
    assert receipt["tour_controls"]["missing_provider_modes"] == [
        "matterport",
        "3dvista",
        "magicfit",
    ]
    assert receipt["tour_controls"]["delivery_contracts"]["3dvista"]["blocked_reason"] == "missing_3dvista_export"
    assert "verified non-trial 3DVista" in receipt["tour_controls"]["delivery_contracts"]["3dvista"]["required_to_send"][0]
    assert receipt["operator_import_manifest"]["ready_for_exports"] is True
    assert receipt["operator_import_manifest"]["status"] == "waiting_for_verified_assets"
    assert receipt["operator_import_manifest"]["drop_status_summary"]["waiting_for_assets"] == 4
    assert receipt["operator_import_manifest"]["missing_prepared_providers"] == []
    assert receipt["operator_import_manifest"]["hardened_readmes_ok"] is True
    assert receipt["operator_import_manifest"]["hardened_readme_provider_count"] == 4
    assert "gold still requires real imported assets" in receipt["operator_import_manifest"]["note"]
    assert receipt["export_discovery"]["rejected_sample"][0]["reason"] == "magicfit_receipt_missing"
    assert receipt["export_discovery"]["repair_count"] == 1
    assert receipt["export_discovery"]["repair_sample"][0]["status"] == "waiting_for_verified_assets"
    assert "import_magicfit_walkthrough.py" in receipt["export_discovery"]["repair_sample"][0]["import_command_after_assets_arrive"]
    assert "magicfit-receipt.json" in receipt["next_required_actions"][-1]["action"]
    assert receipt["next_required_actions"][-1]["rejected_sample"][0]["provider"] == "magicfit"
    assert any(row["area"] == "verified_tour_provider_modes" for row in receipt["blockers"])
    assert any(row["area"] == "tour_export_drop" for row in receipt["blockers"])


def test_gold_status_default_live_mobile_receipt_includes_postdeploy_names(tmp_path: Path, monkeypatch) -> None:
    from scripts.propertyquarry_gold_status import _default_receipt_path

    smoke_dir = tmp_path / "_completion" / "smoke"
    smoke_dir.mkdir(parents=True)
    older = smoke_dir / "property-live-mobile-surface-old.json"
    older.write_text(
        json.dumps({"generated_at": "2026-06-26T01:00:00+00:00", "status": "pass"}),
        encoding="utf-8",
    )
    newer = smoke_dir / "property-live-mobile-delivery-contract-postdeploy.json"
    newer.write_text(
        json.dumps({"generated_at": "2026-06-26T09:34:07+00:00", "status": "pass"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert _default_receipt_path("live_mobile") == newer.resolve()


def test_gold_status_ignores_missing_optional_tour_provider_modes(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "blocked_missing_provider_modes",
            "provider_counts": {"matterport": 29, "magicfit": 8, "3dvista": 0, "pano2vr": 0, "krpano": 0},
            "provider_blockers": {
                "3dvista": {"blocked_count": 12, "reasons": [{"reason": "missing_3dvista_export", "count": 12, "action": "import a verified 3DVista export"}]},
                "pano2vr": {"blocked_count": 12, "reasons": [{"reason": "missing_pano2vr_export", "count": 12, "action": "import a verified Pano2VR export"}]},
                "krpano": {"blocked_count": 9, "reasons": [{"reason": "missing_walkable_scene", "count": 9, "action": "provide a real walkable_scene"}]},
            },
            "ready_provider_modes": ["matterport", "magicfit"],
            "missing_provider_modes": ["3dvista", "pano2vr", "krpano"],
            "next_required_actions": [
                {"provider": "3dvista", "action": "import a verified 3DVista export"},
                {"provider": "pano2vr", "action": "import a verified Pano2VR export"},
                {"provider": "krpano", "action": "provide a real walkable_scene"},
            ],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {
            "status": "blocked_no_verified_exports",
            "import_count": 0,
            "rejected_count": 4,
            "rejected": [
                {"slug": "flat", "provider": "3dvista", "reason": "3dvista_export_entry_unverified", "action": "copy the complete 3DVista export", "drop_layout": "<drop>/<slug>/3dvista/"},
                {
                    "slug": "flat",
                    "provider": "pano2vr",
                    "reason": "pano2vr_export_entry_unverified",
                    "action": "copy the complete Pano2VR export",
                    "drop_layout": "<drop>/<slug>/pano2vr/",
                    "file_count": 1,
                    "present_sample": ["index.html"],
                    "entry_candidates": ["index.html"],
                    "missing": ["pano2vr_runtime_marker"],
                    "missing_markers": ["ggpkg", "ggskin", "pano.xml", "tour.js"],
                },
                {"slug": "flat", "provider": "krpano", "reason": "krpano_assets_missing", "action": "copy a real panorama", "drop_layout": "<drop>/<slug>/krpano/"},
                {"slug": "flat", "provider": "magicfit", "reason": "magicfit_video_missing", "action": "copy the MagicFit walkthrough", "drop_layout": "<drop>/<slug>/magicfit/"},
            ],
        },
    )
    import_manifest = _write_json(tmp_path / "import-manifest.json", _import_manifest_payload(tmp_path))
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        import_manifest_receipt_path=import_manifest,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert not any(
        row["area"] == "verified_tour_provider_modes"
        for row in receipt["blockers"]
    )
    assert receipt["tour_controls"]["core_missing_provider_modes"] == []
    assert receipt["tour_controls"]["provider_blockers"]["krpano"]["reasons"][0]["reason"] == "missing_walkable_scene"
    assert receipt["notes"][0] == "Gold remains blocked until every failing gate below is repaired."
    missing_note = receipt["notes"][-1]
    assert "MagicFit" not in missing_note
    assert "Matterport" not in missing_note
    assert "3DVista" not in missing_note
    assert "krpano" not in missing_note


def test_gold_status_blocks_when_magicfit_ready_lacks_playback_proof(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "magicfit_playback": {
                "playback_ok": True,
                "playable_count": 0,
                "ready_count": 1,
                "evidence": [],
            },
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    ownership = _write_json(tmp_path / "tour-provider-ownership.json", _tour_provider_ownership_payload())
    vendor_tooling = _write_json(
        tmp_path / "vendor-tooling.json",
        {
            "status": "pass",
            "host_ready": True,
            "generated_tour_ready": True,
            "generated_tour_tools": {
                "krpanotools": {"available": True, "path": "/usr/local/bin/krpanotools"},
                "blender": {"available": True, "path": "/usr/bin/blender"},
                "colmap": {"available": True, "path": "/usr/bin/colmap"},
            },
            "runtime_generated_tour_ready": False,
            "runtime_generated_tour_tools": {
                "ffmpeg": {"available": True, "path": "/usr/bin/ffmpeg"},
                "blender": {"available": False, "path": ""},
            },
            "wine_runtime_ready": True,
            "installer_count": 2,
            "installer_counts": {"3dvista": 1, "pano2vr": 1},
            "installed_app_count": 1,
            "installed_app_counts": {"3dvista": 1, "pano2vr": 0},
            "installed_apps": [
                {
                    "provider": "3dvista",
                    "path": "/state/vendor_apps/3dvista/3DVista Virtual Tour.exe",
                    "size_bytes": 123,
                    "layout": "portable_extract",
                }
            ],
            "verified_export_ready_counts": {"3dvista": 1, "pano2vr": 1},
            "missing_verified_exports": [],
            "next_actions": [],
        },
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_provider_ownership_receipt_path=ownership,
        vendor_tooling_receipt_path=vendor_tooling,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "magicfit_walkthrough_playback")
    assert receipt["status"] == "blocked"
    assert receipt["core_missing_provider_modes"] == []
    assert receipt["advanced_visual_gold_status"] in {"blocked", "unavailable"}
    assert receipt["tour_controls"]["magicfit_playback_ok"] is False
    assert blocker["playable_count"] == 0
    assert blocker["ready_count"] == 1


def test_gold_status_blocks_when_browser_3d_gate_fails_even_if_tour_controls_pass(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload(status="fail"))
    walkthrough_quality = _write_json(
        tmp_path / "walkthrough-quality.json",
        _walkthrough_quality_gate_payload(),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        walkthrough_quality_receipt_path=walkthrough_quality,
        claim_scope="advanced_visual",
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "browser_rendered_3d")
    assert receipt["status"] == "blocked"
    assert receipt["browser_rendered_3d"]["ready"] is False
    assert receipt["tour_controls"]["status"] == "pass"
    assert blocker["failed_checks"][0]["name"] == "3dvista_rendered_viewer"
    assert blocker["failed_checks"][0]["state"]["frame_text"].startswith("Loading virtual tour")
    assert any(row["provider"] == "3dvista" and row["status"] == "fail" for row in blocker["provider_results"])
    assert "renders in a real browser" in blocker["action"]


def test_gold_status_blocks_when_walkthrough_quality_gate_fails_even_if_video_exists(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    walkthrough_quality = _write_json(
        tmp_path / "walkthrough-quality.json",
        _walkthrough_quality_gate_payload(status="fail"),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        walkthrough_quality_receipt_path=walkthrough_quality,
        claim_scope="advanced_visual",
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "walkthrough_quality")
    failed_names = {row["name"] for row in blocker["failed_checks"]}
    assert receipt["status"] == "blocked"
    assert receipt["walkthrough_quality"]["ready"] is False
    assert receipt["walkthrough_quality"]["video_relpath"] == "magicfit-walkthrough.mp4"
    assert "walkthrough_duration_floor" in failed_names
    assert "walkthrough_room_coverage_complete" in failed_names
    assert "walkthrough_frame_jump_limit" in failed_names
    coverage_failure = next(row for row in blocker["failed_checks"] if row["name"] == "walkthrough_room_coverage_complete")
    assert coverage_failure["coverage"]["missing_rooms"] == ["bedroom", "living"]
    jump_failure = next(row for row in blocker["failed_checks"] if row["name"] == "walkthrough_frame_jump_limit")
    assert jump_failure["frame_delta_stats"]["max_delta"] == 60.064


def test_gold_status_blocks_when_generated_reconstruction_runtime_gate_fails_even_if_browser_3d_passes(
    tmp_path: Path,
) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(
        tmp_path / "runtime-reconstruction.json",
        _runtime_reconstruction_payload(status="fail", glb=False, required_paths=False),
    )
    walkthrough_quality = _write_json(
        tmp_path / "walkthrough-quality.json",
        _walkthrough_quality_gate_payload(),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "generated_reconstruction_glb")
    assert receipt["status"] == "blocked"
    assert receipt["browser_rendered_3d"]["ready"] is True
    assert receipt["generated_reconstruction_glb"]["ready"] is False
    assert receipt["generated_reconstruction_glb"]["glb_size_bytes"] == 0
    assert blocker["glb_export_status"] == "failed"
    assert blocker["glb_non_empty"] is False
    assert blocker["glb_manifest_ok"] is False
    assert "property_runtime_reconstruction_smoke.py" in blocker["action"]
    assert "--require-public-contract" in blocker["action"]
    assert "--require-glb" in blocker["action"]
    assert "model export" in blocker["action"]


def test_gold_status_blocks_generated_reconstruction_runtime_without_real_glb_even_when_public_contract_passes(
    tmp_path: Path,
) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(
        tmp_path / "runtime-reconstruction.json",
        _runtime_reconstruction_payload(status="pass", glb=False, public_contract=True, required_paths=True),
    )
    walkthrough_quality = _write_json(
        tmp_path / "walkthrough-quality.json",
        _walkthrough_quality_gate_payload(),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "generated_reconstruction_glb")
    assert receipt["status"] == "blocked"
    assert receipt["generated_reconstruction_glb"]["ready"] is False
    assert receipt["generated_reconstruction_glb"]["glb_required"] is False
    assert receipt["generated_reconstruction_glb"]["glb_non_empty"] is False
    assert receipt["generated_reconstruction_glb"]["glb_manifest_ok"] is False
    assert receipt["generated_reconstruction_glb"]["route_label_quality_ok"] is True
    assert receipt["generated_reconstruction_glb"]["walkthrough_label_quality_ok"] is True
    assert receipt["generated_reconstruction_glb"]["walkthrough_generated_ok"] is True
    assert receipt["generated_reconstruction_glb"]["browser_shell_ok"] is True
    assert blocker["glb_non_empty"] is False
    assert blocker["glb_manifest_ok"] is False


def test_gold_status_blocks_generated_reconstruction_when_browser_shell_proof_is_missing(
    tmp_path: Path,
) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(
        tmp_path / "runtime-reconstruction.json",
        _runtime_reconstruction_payload(status="pass", browser_shell=False, public_contract=True, required_paths=True),
    )
    walkthrough_quality = _write_json(
        tmp_path / "walkthrough-quality.json",
        _walkthrough_quality_gate_payload(),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "generated_reconstruction_glb")
    assert receipt["status"] == "blocked"
    assert receipt["generated_reconstruction_glb"]["ready"] is False
    assert receipt["generated_reconstruction_glb"]["browser_shell_ok"] is False
    assert blocker["browser_shell_ok"] is False
    assert "--require-browser-shell" in blocker["action"]
    assert "--host-header propertyquarry.com" in blocker["action"]


def test_gold_status_blocks_generated_reconstruction_when_browser_shell_status_is_not_pass(
    tmp_path: Path,
) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(
        tmp_path / "runtime-reconstruction.json",
        _runtime_reconstruction_payload(
            status="pass",
            browser_shell=True,
            browser_shell_status="failed",
            browser_failures=["browser_shell_probe_timeout"],
        ),
    )
    walkthrough_quality = _write_json(tmp_path / "walkthrough-quality.json", _walkthrough_quality_gate_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "generated_reconstruction_glb")
    assert receipt["status"] == "blocked"
    assert receipt["generated_reconstruction_glb"]["ready"] is False
    assert receipt["generated_reconstruction_glb"]["browser_shell_ok"] is True
    assert receipt["generated_reconstruction_glb"]["browser_shell_status"] == "failed"
    assert blocker["browser_shell_status"] == "failed"
    assert blocker["browser_shell_failures"] == ["browser_shell_probe_timeout"]


def test_gold_status_blocks_generated_reconstruction_without_honest_generated_disclosure(
    tmp_path: Path,
) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(
        tmp_path / "runtime-reconstruction.json",
        _runtime_reconstruction_payload(status="pass", honest_disclosure=False),
    )
    walkthrough_quality = _write_json(tmp_path / "walkthrough-quality.json", _walkthrough_quality_gate_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "generated_reconstruction_glb")
    assert receipt["status"] == "blocked"
    assert receipt["generated_reconstruction_glb"]["ready"] is False
    assert receipt["generated_reconstruction_glb"]["honest_disclosure_ok"] is False
    assert blocker["honest_disclosure_ok"] is False


def test_gold_status_generated_reconstruction_blocker_surfaces_walkthrough_and_runtime_truth_split(
    tmp_path: Path,
) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(
        tmp_path / "runtime-reconstruction.json",
        _runtime_reconstruction_payload(
            status="failed",
            browser_shell=False,
            public_contract=False,
            walkthrough_generated=False,
            walkthrough_status="failed",
            public_failures=["canonical_not_shell_or_control"],
        ),
    )
    release_hygiene = _write_json(
        tmp_path / "release-hygiene.json",
        _release_hygiene_payload(status="fail", tracked_dirty_path_count=4),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        release_hygiene_receipt_path=release_hygiene,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "generated_reconstruction_glb")
    assert receipt["status"] == "blocked"
    assert receipt["generated_reconstruction_glb"]["ready"] is False
    assert receipt["generated_reconstruction_glb"]["walkthrough_generated_ok"] is False
    assert receipt["generated_reconstruction_glb"]["walkthrough_status"] == "failed"
    assert receipt["generated_reconstruction_glb"]["public_contract_failures"] == ["canonical_not_shell_or_control"]
    assert receipt["generated_reconstruction_glb"]["tracked_dirty_path_count"] == 4
    assert "image-baked /app code" in receipt["generated_reconstruction_glb"]["note"]
    assert blocker["walkthrough_generated_ok"] is False
    assert blocker["walkthrough_status"] == "failed"
    assert blocker["public_contract_failures"] == ["canonical_not_shell_or_control"]
    assert blocker["manifest_runtime_commit"] == "d8426c7"
    assert blocker["head_commit"] == "88cdc13"
    assert blocker["tracked_dirty_path_count"] == 4
    assert "image-baked /app code" in blocker["action"]
    assert any("host worktree changes do not count as runtime proof" in note for note in receipt["notes"])


def test_gold_status_blocks_when_service_generated_reconstruction_smoke_fails(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(tmp_path / "runtime-reconstruction.json", _runtime_reconstruction_payload())
    service_generated_reconstruction = _write_json(
        tmp_path / "service-generated-reconstruction.json",
        _service_generated_reconstruction_payload(delivery_contract=False),
    )
    walkthrough_quality = _write_json(
        tmp_path / "walkthrough-quality.json",
        _walkthrough_quality_gate_payload(),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        service_generated_reconstruction_receipt_path=service_generated_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "service_generated_reconstruction")
    assert receipt["status"] == "blocked"
    assert receipt["service_generated_reconstruction"]["ready"] is False
    assert receipt["service_generated_reconstruction"]["delivery_contract_ok"] is False
    assert blocker["delivery_contract_ok"] is False
    assert "property_service_generated_reconstruction_smoke.py" in blocker["action"]
    assert "--require-public-contract" in blocker["action"]


def test_gold_status_blocks_when_service_generated_reconstruction_browser_shell_proof_is_missing(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(tmp_path / "runtime-reconstruction.json", _runtime_reconstruction_payload())
    service_generated_reconstruction = _write_json(
        tmp_path / "service-generated-reconstruction.json",
        _service_generated_reconstruction_payload(browser_shell=False),
    )
    walkthrough_quality = _write_json(
        tmp_path / "walkthrough-quality.json",
        _walkthrough_quality_gate_payload(),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        service_generated_reconstruction_receipt_path=service_generated_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "service_generated_reconstruction")
    assert receipt["status"] == "blocked"
    assert receipt["service_generated_reconstruction"]["ready"] is False
    assert receipt["service_generated_reconstruction"]["browser_shell_ok"] is False
    assert blocker["browser_shell_ok"] is False
    assert "--require-browser-shell" in blocker["action"]
    assert "--host-header propertyquarry.com" in blocker["action"]


def test_gold_status_surfaces_magicfit_renderer_configuration_when_magicfit_mode_is_missing(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "blocked_missing_provider_modes",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 0},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano"],
            "missing_provider_modes": ["magicfit"],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    ownership = _write_json(tmp_path / "tour-provider-ownership.json", _tour_provider_ownership_payload())
    vendor_tooling = _write_json(
        tmp_path / "vendor-tooling.json",
        {
            "status": "pass",
            "host_ready": True,
            "generated_tour_ready": True,
            "generated_tour_tools": {},
            "runtime_generated_tour_ready": False,
            "runtime_generated_tour_tools": {},
            "wine_runtime_ready": True,
            "installer_count": 2,
            "installer_counts": {"3dvista": 1, "pano2vr": 1},
            "installed_app_count": 1,
            "installed_app_counts": {"3dvista": 1, "pano2vr": 0},
            "installed_apps": [],
            "verified_export_ready_counts": {"3dvista": 1, "pano2vr": 1},
            "missing_verified_exports": [],
            "magicfit_renderer": {
                "status": "blocked_configuration",
                "script_path": "/docker/property/scripts/render_magicfit_property_flythrough.py",
                "script_ready": True,
                "credentials_configured": False,
                "credential_sources": [],
                "env_files_checked": ["/docker/property/.env"],
                "python_modules_ready": True,
                "python_modules": {
                    "playwright": {"available": True, "path": "/usr/bin/python3", "version": "ok"},
                    "requests": {"available": True, "path": "/usr/bin/python3", "version": "ok"},
                },
                "ready": False,
                "next_action": "configure PROPERTYQUARRY_MAGICFIT_EMAIL and PROPERTYQUARRY_MAGICFIT_PASSWORD",
            },
            "next_actions": [],
        },
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_provider_ownership_receipt_path=ownership,
        vendor_tooling_receipt_path=vendor_tooling,
        claim_scope="advanced_visual",
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "advanced_visual_provider_modes")
    magicfit_action = next(
        row
        for row in receipt["next_required_actions"]
        if row.get("provider") == "magicfit" and row.get("area") == "magicfit_renderer"
    )

    assert receipt["status"] == "blocked"
    assert receipt["vendor_tooling"]["magicfit_renderer"]["ready"] is False
    assert receipt["vendor_tooling"]["magicfit_renderer"]["credentials_configured"] is False
    assert blocker["provider_details"]["magicfit"]["renderer_ready"] is False
    assert blocker["provider_details"]["magicfit"]["credentials_configured"] is False
    assert magicfit_action["script_ready"] is True
    assert magicfit_action["credentials_configured"] is False
    assert "renderer configuration" in " ".join(receipt["notes"]).lower()


def test_gold_status_passes_only_when_all_required_evidence_is_present(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
            "magicfit_playback": {
                "playback_ok": True,
                "playable_count": 1,
                "ready_count": 1,
                "evidence": [
                    {
                        "slug": "magicfit-proof-tour",
                        "provider": "magicfit",
                        "control_path": "/tours/magicfit-proof-tour/walkthrough",
                        "media_identity": "/tours/magicfit-proof-tour/walkthrough",
                    }
                ],
            },
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    ownership = _write_json(tmp_path / "tour-provider-ownership.json", _tour_provider_ownership_payload())
    vendor_tooling = _write_json(
        tmp_path / "vendor-tooling.json",
        {
            "status": "pass",
            "host_ready": True,
            "generated_tour_ready": True,
            "generated_tour_tools": {
                "krpanotools": {"available": True, "path": "/usr/local/bin/krpanotools"},
                "blender": {"available": True, "path": "/usr/bin/blender"},
                "colmap": {"available": True, "path": "/usr/bin/colmap"},
            },
            "runtime_generated_tour_ready": False,
            "runtime_generated_tour_tools": {
                "ffmpeg": {"available": True, "path": "/usr/bin/ffmpeg"},
                "blender": {"available": False, "path": ""},
            },
            "wine_runtime_ready": True,
            "installer_count": 2,
            "installer_counts": {"3dvista": 1, "pano2vr": 1},
            "installed_app_count": 1,
            "installed_app_counts": {"3dvista": 1, "pano2vr": 0},
            "installed_apps": [
                {
                    "provider": "3dvista",
                    "path": "/state/vendor_apps/3dvista/3DVista Virtual Tour.exe",
                    "size_bytes": 123,
                    "layout": "portable_extract",
                }
            ],
            "verified_export_ready_counts": {"3dvista": 1, "pano2vr": 1},
            "missing_verified_exports": [],
            "next_actions": [],
        },
    )
    security_posture = _write_json(tmp_path / "security-posture.json", _security_posture_payload())
    release_hygiene = _write_json(tmp_path / "release-hygiene.json", _release_hygiene_payload())
    furniture_style_contract = _write_json(tmp_path / "furniture-style-contract.json", _furniture_style_contract_payload())
    bts_methodology_contract = _write_json(tmp_path / "bts-methodology-contract.json", _bts_methodology_contract_payload())
    tour_delivery_contract = _write_json(tmp_path / "tour-delivery-contract.json", _tour_delivery_contract_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(
        tmp_path / "runtime-reconstruction.json",
        _runtime_reconstruction_payload(),
    )
    service_generated_reconstruction = _write_json(
        tmp_path / "service-generated-reconstruction.json",
        _service_generated_reconstruction_payload(),
    )
    walkthrough_quality = _write_json(tmp_path / "walkthrough-quality.json", _walkthrough_quality_gate_payload())
    walkthrough_provider_proof = _write_json(
        tmp_path / "walkthrough-provider-proof.json",
        _walkthrough_provider_proof_payload(),
    )
    scene_video = _write_json(tmp_path / "scene-video-readiness.json", _scene_video_readiness_payload())
    scene_video_verifier = _write_json(tmp_path / "scene-video-readiness-verifier.json", _scene_video_readiness_verifier_payload())
    scene_video_runtime_status = _write_json(
        tmp_path / "scene-video-runtime-status.json",
        _scene_video_runtime_status_payload(),
    )
    scene_video_provider_refresh_packet = _write_json(
        tmp_path / "scene-video-provider-refresh-packet.json",
        _scene_video_provider_refresh_packet_payload(),
    )
    scene_video_provider_refresh_packet_verifier = _write_json(
        tmp_path / "scene-video-provider-refresh-packet-verifier.json",
        _scene_video_provider_refresh_packet_verifier_payload(),
    )
    (
        advanced_visual_binding,
        advanced_visual_now,
        advanced_visual_release_sha,
        advanced_visual_image_digest,
    ) = _advanced_visual_binding_fixture(
        tmp_path,
        walkthrough_quality=walkthrough_quality,
        walkthrough_provider_proof=walkthrough_provider_proof,
        scene_video_readiness=scene_video,
        scene_video_readiness_verifier=scene_video_verifier,
        scene_video_runtime_status=scene_video_runtime_status,
        scene_video_provider_refresh_packet=scene_video_provider_refresh_packet,
        scene_video_provider_refresh_packet_verifier=(
            scene_video_provider_refresh_packet_verifier
        ),
        privacy=security_posture,
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_provider_ownership_receipt_path=ownership,
        vendor_tooling_receipt_path=vendor_tooling,
        security_posture_receipt_path=security_posture,
        release_hygiene_receipt_path=release_hygiene,
        furniture_style_contract_receipt_path=furniture_style_contract,
        bts_methodology_contract_receipt_path=bts_methodology_contract,
        tour_delivery_contract_receipt_path=tour_delivery_contract,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        service_generated_reconstruction_receipt_path=service_generated_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
        walkthrough_provider_proof_receipt_path=walkthrough_provider_proof,
        scene_video_readiness_receipt_path=scene_video,
        scene_video_readiness_verifier_receipt_path=scene_video_verifier,
        scene_video_runtime_status_receipt_path=scene_video_runtime_status,
        scene_video_provider_refresh_packet_path=scene_video_provider_refresh_packet,
        scene_video_provider_refresh_packet_verifier_receipt_path=scene_video_provider_refresh_packet_verifier,
        advanced_visual_binding_receipt_path=advanced_visual_binding,
        expected_release_commit_sha=advanced_visual_release_sha,
        expected_release_image_digest=advanced_visual_image_digest,
        now=advanced_visual_now,
        claim_scope="advanced_visual",
    )

    assert receipt["status"] == "pass"
    assert receipt["ready_for_notification"] is True
    assert receipt["performance"]["research_detail_checks_ok"] is True
    assert receipt["performance"]["missing_research_detail_checks"] == []
    assert receipt["performance"]["search_checks_ok"] is True
    assert receipt["performance"]["missing_search_checks"] == []
    assert receipt["analytics"]["status"] == "pass"
    assert receipt["analytics"]["route_count"] == 2
    assert receipt["vendor_tooling"]["generated_tour_ready"] is True
    assert receipt["vendor_tooling"]["generated_tour_tools"]["colmap"]["available"] is True
    assert receipt["vendor_tooling"]["runtime_generated_tour_ready"] is False
    assert receipt["vendor_tooling"]["runtime_generated_tour_tools"]["blender"]["available"] is False
    assert receipt["vendor_tooling"]["installer_counts"] == {"3dvista": 1, "pano2vr": 1}
    assert receipt["vendor_tooling"]["installed_app_count"] == 1
    assert receipt["vendor_tooling"]["installed_app_counts"] == {"3dvista": 1, "pano2vr": 0}
    assert receipt["vendor_tooling"]["installed_apps"][0]["layout"] == "portable_extract"
    assert receipt["blockers"] == []
    assert receipt["notes"][0] == "Current gold gate is green on the active proof set."
    assert receipt["notes"][1].startswith("Provider E2E is current:")
    assert "wrong-country selections sanitized before dispatch" in receipt["notes"][1]
    assert "Self-healing canary is current" in receipt["notes"][2]
    assert "Gold is not claimable" not in " ".join(receipt["notes"])
    pass_areas = {str(row["area"]) for row in receipt["pass_areas"]}
    assert {
        "performance",
        "analytics_privacy",
        "tour_provider_ownership",
        "provider_targeted_search_matrix",
        "self_healing",
        "production_security_posture",
        "release_hygiene",
        "furniture_style_variants",
        "bts_methodology",
        "tour_delivery_contract_shape",
        "browser_rendered_3d",
        "generated_reconstruction_glb",
        "service_generated_reconstruction",
        "walkthrough_quality",
        "walkthrough_provider_proof",
        "scene_video_readiness",
        "scene_video_provider_refresh_packet",
        "advanced_visual_candidate_binding",
        "receipt_freshness",
    }.issubset(pass_areas)
    assert receipt["bts_methodology"]["source_section_count"] == 5
    assert receipt["tour_delivery_contract_shape"]["matterport_ready_count"] == 29
    assert receipt["browser_rendered_3d"]["ready"] is True
    assert receipt["generated_reconstruction_glb"]["ready"] is True
    assert receipt["generated_reconstruction_glb"]["glb_size_bytes"] == 30700
    assert receipt["generated_reconstruction_glb"]["browser_shell_ok"] is True
    assert receipt["generated_reconstruction_glb"]["route_label_quality_ok"] is True
    assert receipt["generated_reconstruction_glb"]["walkthrough_label_quality_ok"] is True
    assert receipt["generated_reconstruction_glb"]["walkthrough_generated_ok"] is True
    assert receipt["service_generated_reconstruction"]["ready"] is True
    assert receipt["service_generated_reconstruction"]["browser_shell_ok"] is True
    assert receipt["service_generated_reconstruction"]["top_level_video_contract_ok"] is True
    assert receipt["service_generated_reconstruction"]["delivery_contract_ok"] is True
    assert receipt["walkthrough_quality"]["ready"] is True
    assert receipt["walkthrough_provider_proof"]["ready"] is True
    assert receipt["walkthrough_provider_proof"]["verified_providers"] == ["magicfit", "omagic"]
    assert receipt["scene_video_readiness"]["ready"] is True
    assert receipt["scene_video_readiness"]["actionability_ready"] is True
    assert receipt["scene_video_readiness"]["provider_runtime_ready"] is True
    assert receipt["scene_video_readiness"]["provider_action_required"] is False
    assert receipt["scene_video_readiness"]["provider_blocked_count"] == 0
    assert receipt["scene_video_readiness"]["provider_summary"]["provider_count"] == 5
    assert receipt["scene_video_readiness"]["runtime_status"]["contract_name"] == "propertyquarry.scene_video_runtime_status.v1"
    assert receipt["scene_video_readiness"]["runtime_status"]["summary"]["provider_count"] == 5
    assert receipt["scene_video_readiness"]["checked_providers"] == ["mootion", "magicfit", "magic", "omagic", "onemin_i2v"]
    assert receipt["scene_video_readiness"]["required_providers"] == ["magicfit", "magic", "omagic"]
    assert receipt["scene_video_readiness"]["missing_required_providers"] == []
    assert receipt["scene_video_readiness"]["provider_refresh_packet"]["ready"] is True
    assert receipt["scene_video_readiness"]["provider_refresh_packet"]["checked_providers"] == ["magicfit", "omagic"]
    assert receipt["scene_video_readiness"]["provider_refresh_packet"]["packet_provider_count"] == 2

    failed_walkthrough_provider_proof = _write_json(
        tmp_path / "walkthrough-provider-proof-failed.json",
        _walkthrough_provider_proof_payload(status="fail"),
    )
    unproven_provider_receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_provider_ownership_receipt_path=ownership,
        vendor_tooling_receipt_path=vendor_tooling,
        security_posture_receipt_path=security_posture,
        release_hygiene_receipt_path=release_hygiene,
        furniture_style_contract_receipt_path=furniture_style_contract,
        bts_methodology_contract_receipt_path=bts_methodology_contract,
        tour_delivery_contract_receipt_path=tour_delivery_contract,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        service_generated_reconstruction_receipt_path=service_generated_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
        walkthrough_provider_proof_receipt_path=failed_walkthrough_provider_proof,
        scene_video_readiness_receipt_path=scene_video,
        scene_video_readiness_verifier_receipt_path=scene_video_verifier,
        scene_video_runtime_status_receipt_path=scene_video_runtime_status,
        scene_video_provider_refresh_packet_path=scene_video_provider_refresh_packet,
        scene_video_provider_refresh_packet_verifier_receipt_path=scene_video_provider_refresh_packet_verifier,
        claim_scope="advanced_visual",
    )
    walkthrough_proof_blocker = next(
        row for row in unproven_provider_receipt["blockers"] if row["area"] == "walkthrough_provider_proof"
    )
    assert unproven_provider_receipt["status"] == "blocked"
    assert unproven_provider_receipt["walkthrough_provider_proof"]["ready"] is False
    assert walkthrough_proof_blocker["verified_providers"] == ["magicfit"]
    assert walkthrough_proof_blocker["missing_providers"] == ["omagic"]

    missing_magic_runtime_payload = _scene_video_runtime_status_payload()
    missing_magic_runtime_payload["providers"] = [
        row
        for row in list(missing_magic_runtime_payload["providers"])
        if row.get("provider") != "magic"
    ]
    missing_magic_runtime_payload["summary"] = {
        "provider_count": 4,
        "ready_count": 4,
        "blocked_count": 0,
        "blocked_providers": [],
        "action_required_count": 0,
        "action_required_providers": [],
        "delivery_ready": True,
    }
    missing_magic_runtime_status = _write_json(
        tmp_path / "scene-video-runtime-status-missing-magic.json",
        missing_magic_runtime_payload,
    )
    missing_magic_runtime_receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_provider_ownership_receipt_path=ownership,
        vendor_tooling_receipt_path=vendor_tooling,
        security_posture_receipt_path=security_posture,
        release_hygiene_receipt_path=release_hygiene,
        furniture_style_contract_receipt_path=furniture_style_contract,
        bts_methodology_contract_receipt_path=bts_methodology_contract,
        tour_delivery_contract_receipt_path=tour_delivery_contract,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        service_generated_reconstruction_receipt_path=service_generated_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
        walkthrough_provider_proof_receipt_path=walkthrough_provider_proof,
        scene_video_readiness_receipt_path=scene_video,
        scene_video_readiness_verifier_receipt_path=scene_video_verifier,
        scene_video_runtime_status_receipt_path=missing_magic_runtime_status,
        scene_video_provider_refresh_packet_path=scene_video_provider_refresh_packet,
        scene_video_provider_refresh_packet_verifier_receipt_path=scene_video_provider_refresh_packet_verifier,
        claim_scope="advanced_visual",
    )
    missing_magic_blocker = next(
        row
        for row in missing_magic_runtime_receipt["blockers"]
        if row["area"] == "scene_video_provider_runtime"
    )
    assert missing_magic_runtime_receipt["status"] == "blocked"
    assert missing_magic_runtime_receipt["scene_video_readiness"]["ready"] is False
    assert missing_magic_runtime_receipt["scene_video_readiness"]["provider_runtime_ready"] is False
    assert missing_magic_runtime_receipt["scene_video_readiness"]["runtime_missing_required_providers"] == ["magic"]
    assert missing_magic_runtime_receipt["scene_video_readiness"]["missing_required_providers"] == ["magic"]
    assert missing_magic_blocker["runtime_missing_required_providers"] == ["magic"]

    blocked_omagic_vendor_payload = json.loads(vendor_tooling.read_text(encoding="utf-8"))
    blocked_omagic_vendor_payload["omagic_adapter"] = {
        "status": "blocked_runtime_script_missing",
        "ready": False,
        "script_ready": True,
        "runtime_checked": True,
        "runtime_script_ready": False,
        "runtime_script": {
            "available": False,
            "container": "propertyquarry-api",
            "path": "/app/scripts/render_omagic_property_model_walkthrough.py",
        },
        "next_action": "rebuild/redeploy the PropertyQuarry runtime image so /app/scripts/render_omagic_property_model_walkthrough.py exists before claiming OMagic adapter availability",
    }
    blocked_omagic_vendor_tooling = _write_json(
        tmp_path / "vendor-tooling-omagic-adapter-missing.json",
        blocked_omagic_vendor_payload,
    )
    blocked_omagic_adapter_receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_provider_ownership_receipt_path=ownership,
        vendor_tooling_receipt_path=blocked_omagic_vendor_tooling,
        security_posture_receipt_path=security_posture,
        release_hygiene_receipt_path=release_hygiene,
        furniture_style_contract_receipt_path=furniture_style_contract,
        bts_methodology_contract_receipt_path=bts_methodology_contract,
        tour_delivery_contract_receipt_path=tour_delivery_contract,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
        walkthrough_provider_proof_receipt_path=walkthrough_provider_proof,
        scene_video_readiness_receipt_path=scene_video,
        scene_video_readiness_verifier_receipt_path=scene_video_verifier,
        scene_video_provider_refresh_packet_path=scene_video_provider_refresh_packet,
        scene_video_provider_refresh_packet_verifier_receipt_path=scene_video_provider_refresh_packet_verifier,
        claim_scope="advanced_visual",
    )
    omagic_deploy_blocker = next(
        row
        for row in blocked_omagic_adapter_receipt["blockers"]
        if row["area"] == "omagic_model_upload_adapter_deploy"
    )
    omagic_deploy_action = next(
        row
        for row in blocked_omagic_adapter_receipt["next_required_actions"]
        if row.get("area") == "omagic_model_upload_adapter_deploy"
    )
    assert blocked_omagic_adapter_receipt["status"] == "blocked"
    assert blocked_omagic_adapter_receipt["vendor_tooling"]["omagic_adapter"]["runtime_checked"] is True
    assert omagic_deploy_blocker["runtime_script_ready"] is False
    assert omagic_deploy_action["provider"] == "omagic"

    blocked_scene_video = _write_json(tmp_path / "scene-video-readiness-blocked.json", _scene_video_readiness_payload(blocked=True))
    provider_runtime_blocked_receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_provider_ownership_receipt_path=ownership,
        vendor_tooling_receipt_path=vendor_tooling,
        security_posture_receipt_path=security_posture,
        release_hygiene_receipt_path=release_hygiene,
        furniture_style_contract_receipt_path=furniture_style_contract,
        bts_methodology_contract_receipt_path=bts_methodology_contract,
        tour_delivery_contract_receipt_path=tour_delivery_contract,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
        walkthrough_provider_proof_receipt_path=walkthrough_provider_proof,
        scene_video_readiness_receipt_path=blocked_scene_video,
        scene_video_readiness_verifier_receipt_path=scene_video_verifier,
        scene_video_provider_refresh_packet_path=scene_video_provider_refresh_packet,
        scene_video_provider_refresh_packet_verifier_receipt_path=scene_video_provider_refresh_packet_verifier,
        claim_scope="advanced_visual",
    )
    provider_runtime_blocker = next(
        row for row in provider_runtime_blocked_receipt["blockers"] if row["area"] == "scene_video_provider_runtime"
    )
    scene_video_action = next(
        row
        for row in provider_runtime_blocked_receipt["next_required_actions"]
        if row.get("area") == "scene_video_provider_runtime" and row.get("provider") == "magicfit"
    )
    assert provider_runtime_blocked_receipt["status"] == "blocked"
    assert provider_runtime_blocked_receipt["scene_video_readiness"]["actionability_ready"] is True
    assert provider_runtime_blocked_receipt["scene_video_readiness"]["provider_runtime_ready"] is False
    assert provider_runtime_blocked_receipt["scene_video_readiness"]["provider_action_required"] is True
    assert provider_runtime_blocked_receipt["scene_video_readiness"]["blocked_providers"] == [
        "magicfit",
        "magic",
        "omagic",
    ]
    assert provider_runtime_blocker["provider_blocked_count"] == 3
    assert provider_runtime_blocker["blocked_providers"] == ["magicfit", "magic", "omagic"]
    assert scene_video_action["reason"] == "provider_account_visibility_gap"


def test_gold_status_prefers_scene_video_runtime_status_receipt_for_provider_runtime_truth(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    ownership = _write_json(tmp_path / "tour-provider-ownership.json", _tour_provider_ownership_payload())
    vendor_tooling = _write_json(tmp_path / "vendor-tooling.json", {"status": "pass", "next_actions": []})
    security_posture = _write_json(tmp_path / "security-posture.json", _security_posture_payload())
    release_hygiene = _write_json(tmp_path / "release-hygiene.json", _release_hygiene_payload())
    furniture_style_contract = _write_json(tmp_path / "furniture-style-contract.json", _furniture_style_contract_payload())
    bts_methodology_contract = _write_json(tmp_path / "bts-methodology-contract.json", _bts_methodology_contract_payload())
    tour_delivery_contract = _write_json(tmp_path / "tour-delivery-contract.json", _tour_delivery_contract_payload())
    browser_3d_gate = _write_json(tmp_path / "browser-3d-gate.json", _browser_3d_gate_payload())
    runtime_reconstruction = _write_json(tmp_path / "runtime-reconstruction.json", _runtime_reconstruction_payload())
    walkthrough_quality = _write_json(tmp_path / "walkthrough-quality.json", _walkthrough_quality_gate_payload())
    walkthrough_provider_proof = _write_json(
        tmp_path / "walkthrough-provider-proof.json",
        _walkthrough_provider_proof_payload(),
    )
    scene_video = _write_json(tmp_path / "scene-video-readiness.json", _scene_video_readiness_payload())
    scene_video_verifier = _write_json(tmp_path / "scene-video-readiness-verifier.json", _scene_video_readiness_verifier_payload())
    scene_video_runtime_status = _write_json(
        tmp_path / "scene-video-runtime-status-blocked.json",
        _scene_video_runtime_status_payload(blocked=True),
    )
    scene_video_provider_refresh_packet = _write_json(
        tmp_path / "scene-video-provider-refresh-packet.json",
        _scene_video_provider_refresh_packet_payload(),
    )
    scene_video_provider_refresh_packet_verifier = _write_json(
        tmp_path / "scene-video-provider-refresh-packet-verifier.json",
        _scene_video_provider_refresh_packet_verifier_payload(),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_provider_ownership_receipt_path=ownership,
        vendor_tooling_receipt_path=vendor_tooling,
        security_posture_receipt_path=security_posture,
        release_hygiene_receipt_path=release_hygiene,
        furniture_style_contract_receipt_path=furniture_style_contract,
        bts_methodology_contract_receipt_path=bts_methodology_contract,
        tour_delivery_contract_receipt_path=tour_delivery_contract,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
        walkthrough_provider_proof_receipt_path=walkthrough_provider_proof,
        scene_video_readiness_receipt_path=scene_video,
        scene_video_readiness_verifier_receipt_path=scene_video_verifier,
        scene_video_runtime_status_receipt_path=scene_video_runtime_status,
        scene_video_provider_refresh_packet_path=scene_video_provider_refresh_packet,
        scene_video_provider_refresh_packet_verifier_receipt_path=scene_video_provider_refresh_packet_verifier,
        claim_scope="advanced_visual",
    )

    provider_runtime_blocker = next(row for row in receipt["blockers"] if row["area"] == "scene_video_provider_runtime")
    scene_video_action = next(
        row
        for row in receipt["next_required_actions"]
        if row.get("area") == "scene_video_provider_runtime" and row.get("provider") == "magicfit"
    )

    assert receipt["status"] == "blocked"
    assert receipt["scene_video_readiness"]["actionability_ready"] is True
    assert receipt["scene_video_readiness"]["provider_runtime_ready"] is False
    assert receipt["scene_video_readiness"]["provider_action_required"] is True
    assert receipt["scene_video_readiness"]["blocked_providers"] == ["magicfit", "magic", "omagic"]
    assert provider_runtime_blocker["key"] == "scene_video_provider_runtime"
    assert receipt["scene_video_readiness"]["runtime_status"]["summary"]["blocked_count"] == 3
    assert provider_runtime_blocker["provider_blocked_count"] == 3
    assert provider_runtime_blocker["runtime_status_providers"][0]["provider"] == "magicfit"
    assert scene_video_action["reason"] == "provider_account_visibility_gap"
    assert scene_video_action["visible_account_gap"] == 3


    failing_scene_video_provider_refresh_packet_verifier = _write_json(
        tmp_path / "scene-video-provider-refresh-packet-verifier-fail.json",
        _scene_video_provider_refresh_packet_verifier_payload(status="fail"),
    )
    blocked_receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_provider_ownership_receipt_path=ownership,
        vendor_tooling_receipt_path=vendor_tooling,
        security_posture_receipt_path=security_posture,
        release_hygiene_receipt_path=release_hygiene,
        furniture_style_contract_receipt_path=furniture_style_contract,
        bts_methodology_contract_receipt_path=bts_methodology_contract,
        tour_delivery_contract_receipt_path=tour_delivery_contract,
        browser_3d_gate_receipt_path=browser_3d_gate,
        runtime_reconstruction_receipt_path=runtime_reconstruction,
        walkthrough_quality_receipt_path=walkthrough_quality,
        walkthrough_provider_proof_receipt_path=walkthrough_provider_proof,
        scene_video_readiness_receipt_path=scene_video,
        scene_video_readiness_verifier_receipt_path=scene_video_verifier,
        scene_video_provider_refresh_packet_path=scene_video_provider_refresh_packet,
        scene_video_provider_refresh_packet_verifier_receipt_path=failing_scene_video_provider_refresh_packet_verifier,
        claim_scope="advanced_visual",
    )
    refresh_blocker = next(row for row in blocked_receipt["blockers"] if row["area"] == "scene_video_provider_refresh_packet")
    assert blocked_receipt["status"] == "blocked"
    assert blocked_receipt["scene_video_readiness"]["ready"] is False
    assert blocked_receipt["scene_video_readiness"]["provider_refresh_packet"]["ready"] is False
    assert refresh_blocker["verifier_blockers"] == ["omagic_onemin_boundary_missing"]


def test_gold_status_blocks_when_security_posture_receipt_fails(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    security_posture = _write_json(tmp_path / "security-posture.json", _security_posture_payload(status="fail"))

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        security_posture_receipt_path=security_posture,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "production_security_posture")
    assert receipt["status"] == "blocked"
    assert receipt["ready_for_notification"] is False
    assert receipt["production_security_posture"]["status"] == "fail"
    assert "USER ea" in blocker["failures"][0]
    assert "isolated runtime" in blocker["action"]


def test_gold_status_blocks_when_release_hygiene_receipt_fails(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    release_hygiene = _write_json(tmp_path / "release-hygiene.json", _release_hygiene_payload(status="fail"))

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        release_hygiene_receipt_path=release_hygiene,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "release_hygiene")
    assert receipt["status"] == "blocked"
    assert receipt["release_hygiene"]["status"] == "fail"
    assert blocker["key"] == "release_hygiene"
    assert blocker["manifest_runtime_commit"] == "d8426c7"
    assert blocker["head_commit"] == "88cdc13"
    assert "release manifest runtime commit" in blocker["failures"][0]


@pytest.mark.parametrize(
    "mutation",
    (
        "status",
        "schema",
        "plan_caps",
        "helper_plan_caps",
        "availability_mode",
        "pricing_surface_bound",
        "style_count",
        "failures",
    ),
)
def test_gold_status_blocks_when_furniture_style_contract_fails(
    tmp_path: Path,
    mutation: str,
) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    furniture_style_payload = _furniture_style_contract_payload()
    if mutation == "status":
        furniture_style_payload["status"] = "fail"
    elif mutation == "schema":
        furniture_style_payload["schema"] = "propertyquarry.furniture_style_contract_receipt.v1"
    elif mutation == "plan_caps":
        furniture_style_payload["plan_caps"] = {"free": 1, "plus": 5, "agent": 5}
    elif mutation == "helper_plan_caps":
        furniture_style_payload["helper_plan_caps"] = {"free": 1, "plus": 5, "agent": 5}
    elif mutation == "availability_mode":
        furniture_style_payload["availability_mode"] = "saved_search_preference"
    elif mutation == "pricing_surface_bound":
        furniture_style_payload["pricing_surface_bound"] = False
    elif mutation == "style_count":
        furniture_style_payload["style_count"] = 4
    elif mutation == "failures":
        furniture_style_payload["failures"] = ["furniture style catalog missing value urban_jungle"]
    furniture_style_contract = _write_json(
        tmp_path / "furniture-style-contract.json",
        furniture_style_payload,
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        furniture_style_contract_receipt_path=furniture_style_contract,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "furniture_style_variants")
    assert receipt["status"] == "blocked"
    assert receipt["furniture_style_variants"]["status"] == ("fail" if mutation == "status" else "pass")
    assert blocker["style_count"] == (4 if mutation == "style_count" else 5)
    assert blocker["availability_mode"] == (
        "saved_search_preference"
        if mutation == "availability_mode"
        else "per_visual_request"
    )
    assert blocker["pricing_surface_bound"] is (mutation != "pricing_surface_bound")
    assert "all-tier request-time choice" in blocker["action"]


def test_gold_status_blocks_when_bts_methodology_contract_fails(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    bts_methodology_contract = _write_json(
        tmp_path / "bts-methodology-contract.json",
        _bts_methodology_contract_payload(status="fail"),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        bts_methodology_contract_receipt_path=bts_methodology_contract,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "bts_methodology")
    assert receipt["status"] == "blocked"
    assert receipt["bts_methodology"]["status"] == "fail"
    assert blocker["source_section_count"] == 4
    assert "score-PDF provenance" in blocker["action"]


def test_gold_status_blocks_when_tour_delivery_contract_fails(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    tour_delivery_contract = _write_json(
        tmp_path / "tour-delivery-contract.json",
        _tour_delivery_contract_payload(status="fail"),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        tour_delivery_contract_receipt_path=tour_delivery_contract,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "tour_delivery_contract_shape")
    assert receipt["status"] == "blocked"
    assert receipt["tour_delivery_contract_shape"]["status"] == "fail"
    assert blocker["matterport_ready_count"] == 0
    assert "topology-verified Matterport readiness" in blocker["action"]


def test_gold_status_blocks_when_public_sign_in_account_creation_smoke_is_missing(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    public_smoke = _write_json(tmp_path / "public-smoke.json", _public_smoke_payload(include_account_creation=False))
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        public_smoke_receipt_path=public_smoke,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "public_auth_surfaces")
    assert receipt["status"] == "blocked"
    assert receipt["public_auth_surfaces"]["sign_in_checks_ok"] is False
    assert "sign_in_connected_identity_creates_account" in blocker["missing_sign_in_checks"]


def test_gold_status_blocks_when_brilliant_directories_billing_handoff_does_not_resolve(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    live_mobile = _write_json(tmp_path / "live-mobile.json", _live_mobile_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    billing = _write_json(tmp_path / "billing.json", _billing_payload(host_resolves=False, status="blocked"))
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        billing_receipt_path=billing,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["billing_handoff"]["ready"] is False
    assert receipt["billing_handoff"]["host"] == "billing.propertyquarry.com"
    assert receipt["billing_handoff"]["required_dns_record"]["target"] == "members.brilliantdirectories.com"
    assert "create DNS for billing.propertyquarry.com" in receipt["billing_handoff"]["next_action"]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "billing_handoff")
    assert blocker["host_resolves"] is False
    assert blocker["required_dns_record"]["name"] == "billing.propertyquarry.com"
    assert blocker["required_dns_record"]["type"] == "CNAME"
    assert blocker["required_dns_record"]["target"] == "members.brilliantdirectories.com"
    assert "Brilliant Directories" in blocker["action"]


def test_gold_status_blocks_when_brilliant_directories_billing_handoff_only_resolves_but_is_not_proven_usable(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    live_mobile = _write_json(tmp_path / "live-mobile.json", _live_mobile_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    billing = _write_json(tmp_path / "billing.json", _billing_payload(host_resolves=True, status="disabled"))
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        billing_receipt_path=billing,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["billing_handoff"]["ready"] is False
    assert receipt["billing_handoff"]["host_resolves"] is True
    blocker = next(row for row in receipt["blockers"] if row["area"] == "billing_handoff")
    assert "usable external account lane" in blocker["action"]


def test_gold_status_accepts_signed_billing_bridge_when_vendor_account_lane_needs_login(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    authenticated_smoke = _write_json(
        tmp_path / "authenticated-smoke.json",
        _authenticated_smoke_payload(billing_external=True, billing_fail_closed=False),
    )
    live_mobile = _write_json(tmp_path / "live-mobile.json", _live_mobile_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
                "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
                "missing_provider_modes": [],
                "magicfit_playback": _magicfit_playback_payload(),
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    billing = _write_json(tmp_path / "billing.json", _billing_bridge_payload())
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        authenticated_smoke_receipt_path=authenticated_smoke,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        billing_receipt_path=billing,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "pass"
    assert receipt["billing_handoff"]["ready"] is True
    assert receipt["billing_handoff"]["ready_via"] == "sso_bridge"
    assert receipt["billing_handoff"]["direct_account_handoff_usable"] is False
    assert receipt["billing_handoff"]["signed_handoff_usable"] is True
    assert receipt["billing_handoff"]["live_smoke_external_handoff_usable"] is True
    assert receipt["billing_handoff"]["live_smoke_no_second_login"] is True
    assert not any(row["area"] == "billing_handoff" for row in receipt["blockers"])


def test_gold_status_accepts_member_token_handoff_when_sso_bridge_still_needs_login(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    authenticated_payload = _authenticated_smoke_payload(
        billing_external=True,
        billing_fail_closed=False,
        billing_bridge_launch=True,
    )
    billing_row = next(row for row in authenticated_payload["checks"] if row["path"] == "/app/billing")
    billing_row["checks"].append({"name": "billing_bridge_guided_login_assist", "ok": True})
    authenticated_smoke = _write_json(tmp_path / "authenticated-smoke.json", authenticated_payload)
    live_mobile = _write_json(tmp_path / "live-mobile.json", _live_mobile_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
                "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
                "missing_provider_modes": [],
                "magicfit_playback": _magicfit_playback_payload(),
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    billing = _write_json(tmp_path / "billing.json", _billing_member_token_payload())
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        authenticated_smoke_receipt_path=authenticated_smoke,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        billing_receipt_path=billing,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "pass"
    assert receipt["billing_handoff"]["ready"] is True
    assert receipt["billing_handoff"]["ready_via"] == "member_login_token"
    assert receipt["billing_handoff"]["direct_account_handoff_usable"] is False
    assert receipt["billing_handoff"]["signed_handoff_usable"] is True
    assert receipt["billing_handoff"]["member_login_token"]["ready"] is True
    assert not any(row["area"] == "billing_handoff" for row in receipt["blockers"])


def test_gold_status_blocks_when_signed_billing_bridge_is_configured_but_live_surface_only_fails_closed(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    authenticated_smoke = _write_json(
        tmp_path / "authenticated-smoke.json",
        _authenticated_smoke_payload(billing_external=False, billing_fail_closed=True),
    )
    live_mobile = _write_json(tmp_path / "live-mobile.json", _live_mobile_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    billing = _write_json(tmp_path / "billing.json", _billing_bridge_payload())
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        authenticated_smoke_receipt_path=authenticated_smoke,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        billing_receipt_path=billing,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["billing_handoff"]["ready"] is False
    assert receipt["billing_handoff"]["ready_via"] == ""
    assert receipt["billing_handoff"]["signed_handoff_usable"] is False
    blocker = next(row for row in receipt["blockers"] if row["area"] == "billing_handoff")
    assert blocker["ready_via"] == ""
    assert blocker["signed_handoff_usable"] is False
    assert "usable external account lane" in blocker["action"]


def test_gold_status_keeps_internal_account_fallback_safe_but_blocks_gold_billing_handoff(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    authenticated_smoke = _write_json(
        tmp_path / "authenticated-smoke.json",
        _authenticated_smoke_payload(
            billing_external=False,
            billing_fail_closed=False,
            billing_bridge_launch=True,
            billing_internal_account_fallback=True,
        ),
    )
    live_mobile = _write_json(tmp_path / "live-mobile.json", _live_mobile_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    billing = _write_json(tmp_path / "billing.json", _billing_bridge_payload())
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        authenticated_smoke_receipt_path=authenticated_smoke,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        billing_receipt_path=billing,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    customer_surfaces = receipt["authenticated_customer_surfaces"]
    assert customer_surfaces["billing_checks_ok"] is True
    assert customer_surfaces["missing_billing_checks"] == []
    assert receipt["status"] == "blocked"
    assert receipt["billing_handoff"]["ready"] is False
    blocker = next(row for row in receipt["blockers"] if row["area"] == "billing_handoff")
    assert blocker["member_login_token_ready"] is False
    assert "usable external account lane" in blocker["action"]


def test_gold_status_keeps_bridge_guided_login_assist_as_billing_blocker_until_member_handoff_is_ready(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    authenticated_smoke = _write_json(
        tmp_path / "authenticated-smoke.json",
        {
            "status": "pass",
            "failed_count": 0,
            "route_count": 3,
            "checks": [
                {
                    "path": "/app/account",
                    "status_code": 200,
                    "ok": True,
                    "checks": [
                        {"name": "account_notifications", "ok": True},
                        {"name": "account_notification_form", "ok": True},
                        {"name": "account_notification_email_channel", "ok": True},
                        {"name": "account_notification_telegram_channel", "ok": True},
                        {"name": "account_notification_whatsapp_channel", "ok": True},
                        {"name": "account_notification_primary_route", "ok": True},
                        {"name": "account_notification_whatsapp_phone", "ok": True},
                        {"name": "account_notification_save_action", "ok": True},
                    ],
                },
                {
                    "path": "/app/billing",
                    "status_code": 303,
                    "ok": True,
                    "checks": [
                        {"name": "billing_bridge_launch", "ok": True},
                        {"name": "billing_external_handoff", "ok": True},
                        {"name": "billing_external_handoff_resolves", "ok": True},
                        {"name": "billing_external_handoff_usable", "ok": True},
                        {"name": "billing_bridge_guided_login_assist", "ok": True},
                        {"name": "billing_local_board_deleted", "ok": True},
                    ],
                },
            ],
        },
    )
    live_mobile = _write_json(tmp_path / "live-mobile.json", _live_mobile_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    billing = _write_json(tmp_path / "billing.json", _billing_bridge_payload())
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        authenticated_smoke_receipt_path=authenticated_smoke,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        billing_receipt_path=billing,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["authenticated_customer_surfaces"]["billing_checks_ok"] is False
    assert (
        "billing_external_handoff_or_fail_closed_recovery"
        in receipt["authenticated_customer_surfaces"]["missing_billing_checks"]
    )
    assert receipt["billing_handoff"]["ready"] is False
    assert receipt["billing_handoff"]["member_login_token"]["ready"] is False
    assert "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_API_KEY" in receipt["billing_handoff"]["member_login_token"]["required_env"]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "billing_handoff")
    assert blocker["status"] == "dry_verified_configured"
    assert blocker["member_login_token_ready"] is False
    assert "PROPERTYQUARRY_BRILLIANT_DIRECTORIES_MEMBER_LOGIN_TOKEN_SECRET" in blocker["member_login_token_required_env"]
    assert "generate a Brilliant Directories API key" in blocker["admin_action"]
    assert "usable external account lane" in blocker["action"]


def test_gold_status_blocks_when_authenticated_billing_surface_exposes_local_board(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    authenticated_smoke = _write_json(
        tmp_path / "authenticated-smoke.json",
        _authenticated_smoke_payload(billing_external=False, billing_fail_closed=False, local_board_deleted=False),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    billing = _write_json(tmp_path / "billing.json", _billing_payload(host_resolves=True, status="disabled"))
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        authenticated_smoke_receipt_path=authenticated_smoke,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        billing_receipt_path=billing,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "authenticated_customer_surfaces")
    assert receipt["status"] == "blocked"
    assert receipt["authenticated_customer_surfaces"]["billing_checks_ok"] is False
    assert "billing_external_handoff_or_fail_closed_recovery" in blocker["missing_billing_checks"]
    assert any(row["name"] == "billing_local_board_deleted" for row in blocker["failed_billing_checks"])


def test_gold_status_blocks_when_authenticated_notification_surface_loses_routing_form(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    authenticated_smoke = _write_json(
        tmp_path / "authenticated-smoke.json",
        _authenticated_smoke_payload(include_notification_checks=False),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    billing = _write_json(tmp_path / "billing.json", _billing_payload(host_resolves=True, status="disabled"))
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        authenticated_smoke_receipt_path=authenticated_smoke,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        billing_receipt_path=billing,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "authenticated_customer_surfaces")
    assert receipt["status"] == "blocked"
    assert receipt["authenticated_customer_surfaces"]["notification_checks_ok"] is False
    assert "account_notification_telegram_channel" in blocker["missing_notification_checks"]
    assert "notification routing form" in blocker["action"]


def test_gold_status_blocks_when_receipts_are_stale_even_if_checks_pass(tmp_path: Path) -> None:
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    fresh_generated_at = (now - timedelta(minutes=10)).isoformat()
    stale_generated_at = (now - timedelta(hours=3)).isoformat()
    performance_payload = _performance_payload()
    performance_payload["generated_at"] = fresh_generated_at
    provider_matrix_payload = _provider_matrix_payload()
    provider_matrix_payload["generated_at"] = fresh_generated_at
    performance = _write_json(tmp_path / "performance.json", performance_payload)
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "generated_at": stale_generated_at,
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"generated_at": fresh_generated_at, "status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "generated_at": fresh_generated_at,
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", provider_matrix_payload)

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["receipt_freshness"]["status"] == "fail"
    blocker = next(row for row in receipt["blockers"] if row["area"] == "receipt_freshness")
    assert blocker["stale_receipts"] == [
        {
            "area": "tour_controls",
            "status": "stale",
            "generated_at": stale_generated_at,
            "timestamp_source": "generated_at",
            "raw_generated_at": stale_generated_at,
            "age_hours": 3.0,
            "max_age_hours": 1,
        }
    ]


def test_gold_status_blocks_future_dated_receipts(tmp_path: Path) -> None:
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    future_generated_at = (now + timedelta(minutes=5)).isoformat()

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(
            tmp_path,
            generated_at=future_generated_at,
        ),
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["receipt_freshness"]["status"] == "fail"
    assert any(
        row["area"] == "performance"
        and row["status"] == "future_dated"
        and row["future_seconds"] == 300.0
        and row["maximum_future_skew_seconds"] == 30
        for row in receipt["receipt_freshness"]["stale_receipts"]
    )


def test_gold_status_accepts_repair_summary_timestamp_for_freshness(tmp_path: Path) -> None:
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    fresh_generated_at = (now - timedelta(minutes=10)).isoformat()
    performance_payload = _performance_payload()
    performance_payload["generated_at"] = fresh_generated_at
    provider_matrix_payload = _provider_matrix_payload()
    provider_matrix_payload["generated_at"] = fresh_generated_at
    performance = _write_json(tmp_path / "performance.json", performance_payload)
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "generated_at": fresh_generated_at,
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
                "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
                "missing_provider_modes": [],
                "magicfit_playback": _magicfit_playback_payload(),
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"generated_at": fresh_generated_at, "status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
            "repair_summary": {"generated_at": fresh_generated_at},
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", provider_matrix_payload)

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "pass"
    assert receipt["receipt_freshness"]["status"] == "pass"
    assert receipt["receipt_freshness"]["stale_receipts"] == []


def test_gold_status_blocks_when_repair_canary_is_missing_or_failed(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "failed",
            "run_status": "failed",
            "source_repair_status": "",
            "receipt_resolution": "",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert any(row["area"] == "self_healing_repair" for row in receipt["blockers"])


def test_gold_status_blocks_when_provider_matrix_is_not_executed(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(
        tmp_path / "provider-matrix.json",
        _provider_matrix_payload(status="blocked_targeted_search_matrix_not_executed", executed=False),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert any(row["area"] == "provider_targeted_search_matrix" for row in receipt["blockers"])


def test_gold_status_reports_catalog_smoke_separately_from_provider_e2e(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
                "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
                "missing_provider_modes": [],
                "magicfit_playback": _magicfit_playback_payload(),
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    provider_catalog = _write_json(tmp_path / "provider-catalog.json", _provider_catalog_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_catalog_receipt_path=provider_catalog,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "pass"
    assert receipt["provider_catalog_smoke"]["status"] == "pass"
    assert receipt["provider_catalog_smoke"]["raw_status"] == "blocked_targeted_search_matrix_not_executed"
    assert receipt["provider_catalog_smoke"]["targeted_search_matrix_executed"] is False
    assert not any(row["area"] == "provider_catalog_smoke" for row in receipt["blockers"])


def test_gold_status_blocks_when_provider_matrix_scope_lags_catalog(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    matrix_payload = _provider_matrix_payload()
    matrix_payload["targeted_search_matrix"] = [
        {"country_code": "AT", "provider": "willhaben", "status": "pass"},
    ]
    catalog_payload = _provider_catalog_payload()
    catalog_payload["targeted_search_matrix"] = [
        {"country_code": "AT", "provider": "willhaben", "status": "planned"},
        {"country_code": "AT", "provider": "glorit_at", "status": "planned"},
    ]
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", matrix_payload)
    provider_catalog = _write_json(tmp_path / "provider-catalog.json", catalog_payload)

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_catalog_receipt_path=provider_catalog,
        provider_matrix_receipt_path=provider_matrix,
    )

    blocker = next(
        row for row in receipt["blockers"]
        if row["area"] == "provider_targeted_search_matrix"
    )
    assert receipt["status"] == "blocked"
    assert receipt["provider_matrix"]["catalog_scope_ok"] is False
    assert blocker["catalog_scope"]["missing_providers"] == [
        {"country_code": "AT", "provider": "glorit_at"},
    ]


def test_gold_status_blocks_when_provider_catalog_smoke_fails(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    provider_catalog = _write_json(
        tmp_path / "provider-catalog.json",
        _provider_catalog_payload(check_status="fail"),
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_catalog_receipt_path=provider_catalog,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["provider_catalog_smoke"]["status"] == "blocked"
    assert any(row["area"] == "provider_catalog_smoke" for row in receipt["blockers"])


def test_gold_status_blocks_when_cross_country_provider_sanitization_is_missing(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_payload = _provider_matrix_payload()
    provider_payload["cross_country_sanitization_summary"] = {
        "case_count": 1,
        "status_counts": {"fail": 1},
        "sanitization_ok": False,
    }
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", provider_payload)

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "provider_targeted_search_matrix")
    assert receipt["status"] == "blocked"
    assert receipt["provider_matrix"]["cross_country_sanitization_ok"] is False
    assert blocker["cross_country_sanitization_ok"] is False
    assert "wrong-country provider selections are sanitized" in blocker["action"]


def test_gold_status_blocks_when_live_mobile_surface_smoke_fails(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(),
    )
    live_mobile = _write_json(
        tmp_path / "live-mobile.json",
        {"status": "fail", "failed_count": 1, "route_count": 7, "viewport": {"width": 390, "height": 844}},
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["live_mobile_surfaces"]["status"] == "fail"
    assert any(row["area"] == "live_mobile_surfaces" for row in receipt["blockers"])


def test_gold_status_blocks_when_live_mobile_surface_coverage_is_old_or_narrow(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(),
    )
    live_mobile = _write_json(
        tmp_path / "live-mobile.json",
        _live_mobile_payload(
            routes=[
                "/app/search",
                "/app/shortlist",
                "/app/agents",
                "/app/alerts",
                "/app/account",
                "/app/billing",
                "/app/settings/google",
                "/app/research",
                "/app/properties/packets",
            ]
        ),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["live_mobile_surfaces"]["required_route_count"] == 19
    assert "/app/settings/access" in receipt["live_mobile_surfaces"]["missing_routes"]
    assert receipt["live_mobile_surfaces"]["missing_detail_routes"] == [
        "/app/research/",
        "/app/shortlist/run/",
        "/tours/",
    ]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "live_mobile_surfaces")
    assert "/app/settings/invitations" in blocker["missing_routes"]
    assert blocker["missing_detail_routes"] == [
        "/app/research/",
        "/app/shortlist/run/",
        "/tours/",
    ]


def test_gold_status_blocks_when_live_mobile_research_surface_is_missing(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    routes_without_research = [
        route
        for route in _live_mobile_payload()["routes"]
        if not str(route["route"]).startswith("/app/research")
    ]
    live_mobile = _write_json(
        tmp_path / "live-mobile.json",
        {
            "status": "pass",
            "failed_count": 0,
            "route_count": 14,
            "viewport": {"width": 390, "height": 844},
            "routes": routes_without_research,
        },
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert "/app/research" in receipt["live_mobile_surfaces"]["missing_routes"]
    assert receipt["live_mobile_surfaces"]["missing_detail_routes"] == ["/app/research/"]


def test_gold_status_requires_live_mobile_research_detail_not_index_only(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    live_mobile = _write_json(
        tmp_path / "live-mobile.json",
        _live_mobile_payload(
                routes=[
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
                    "/app/shortlist/run/run-gold",
                    "/tours/tour-gold",
                ]
        ),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["live_mobile_surfaces"]["missing_routes"] == []
    assert receipt["live_mobile_surfaces"]["missing_detail_routes"] == ["/app/research/"]


def test_gold_status_blocks_when_live_mobile_coverage_check_fails(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    live_mobile_payload = _live_mobile_payload()
    live_mobile_payload["status"] = "fail"
    live_mobile_payload["failed_count"] = 1
    live_mobile_payload["coverage_checks"] = [
        {
            "name": "research_detail_route_configured",
            "ok": False,
            "required_route_prefix": "/app/research/",
            "reason": "Gold mobile smoke must exercise a current live research detail page, not only /app/research.",
        }
    ]
    live_mobile = _write_json(tmp_path / "live-mobile.json", live_mobile_payload)
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["live_mobile_surfaces"]["failed_coverage_checks"] == [
        {
            "name": "research_detail_route_configured",
            "required_route_prefix": "/app/research/",
            "reason": "Gold mobile smoke must exercise a current live research detail page, not only /app/research.",
        },
        {
            "name": "shortlist_run_route_configured",
            "required_route_prefix": "/app/shortlist/run/",
            "reason": "Live mobile receipt predates the required all-surface coverage contract.",
        },
        {
            "name": "public_tour_route_configured",
            "required_route_prefix": "/tours/",
            "reason": "Live mobile receipt predates the required all-surface coverage contract.",
        },
        {
            "name": "registry_mobile_customer_surfaces_covered",
            "required_route_prefix": "",
            "reason": "Live mobile receipt predates the required all-surface coverage contract.",
        }
    ]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "live_mobile_surfaces")
    assert blocker["failed_coverage_checks"] == receipt["live_mobile_surfaces"]["failed_coverage_checks"]


def test_gold_status_surfaces_whole_project_scope_receipt(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    live_mobile = _write_json(tmp_path / "live-mobile.json", _live_mobile_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    whole_project_scope = _write_json(
        tmp_path / "whole-project-scope.json",
        {
            "schema": "propertyquarry.whole_project_scope_receipt.v1",
            "status": "pass",
            "generated_at": "2026-06-26T09:00:00+00:00",
            "required_overlay_layers": ["summer_heat", "media_attention", "fiber_broadband"],
            "failures": [],
        },
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        whole_project_scope_receipt_path=whole_project_scope,
    )

    assert receipt["whole_project_scope"]["status"] == "pass"
    assert receipt["whole_project_scope"]["schema"] == "propertyquarry.whole_project_scope_receipt.v1"
    assert receipt["whole_project_scope"]["failure_count"] == 0
    assert any(row["area"] == "whole_project_scope" for row in receipt["pass_areas"])


def test_gold_status_blocks_when_whole_project_scope_receipt_fails(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    live_mobile = _write_json(tmp_path / "live-mobile.json", _live_mobile_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    whole_project_scope = _write_json(
        tmp_path / "whole-project-scope.json",
        {
            "schema": "propertyquarry.whole_project_scope_receipt.v1",
            "status": "fail",
            "generated_at": "2026-06-26T09:00:00+00:00",
            "failures": ["evidence overlay registry missing required layers: fiber_broadband"],
        },
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        live_mobile_receipt_path=live_mobile,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        whole_project_scope_receipt_path=whole_project_scope,
    )

    blocker = next(row for row in receipt["blockers"] if row["area"] == "whole_project_scope")
    assert receipt["status"] == "blocked"
    assert blocker["failures"] == ["evidence overlay registry missing required layers: fiber_broadband"]


def test_gold_status_resolves_container_incoming_readme_paths(monkeypatch, tmp_path: Path) -> None:
    incoming_root = tmp_path / "incoming"
    readme = incoming_root / "slug-a" / "3dvista" / "README.propertyquarry-export.txt"
    readme.parent.mkdir(parents=True)
    readme.write_text("ok", encoding="utf-8")
    monkeypatch.setenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR", str(incoming_root))

    from scripts.propertyquarry_gold_status import _host_readme_path

    assert _host_readme_path("/data/incoming_property_tours/slug-a/3dvista/README.propertyquarry-export.txt") == readme


def test_gold_status_requires_operator_readmes_only_for_manifest_providers(tmp_path: Path) -> None:
    from scripts.propertyquarry_gold_status import _operator_drop_readme_status

    prepared: list[dict[str, str]] = []
    bodies = {
        "3dvista": """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example:
Public gold only passes when verify_property_tour_controls reports ready provider modes
Copy the complete 3DVista export folder
tdvplayer
import_3dvista_export.py
""",
        "pano2vr": """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example:
Public gold only passes when verify_property_tour_controls reports ready provider modes
Copy the complete Pano2VR output folder
tour.js
import_pano2vr_export.py
""",
        "krpano": """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example:
Public gold only passes when verify_property_tour_controls reports ready provider modes
cube-face-1
KRPANO_LICENSE_DOMAIN=propertyquarry.com
import_krpano_walkable_scene.py
""",
    }
    for provider, body in bodies.items():
        readme = tmp_path / "incoming" / "slug" / provider / "README.propertyquarry-export.txt"
        readme.parent.mkdir(parents=True)
        readme.write_text(body, encoding="utf-8")
        prepared.append({"provider": provider, "readme": str(readme)})

    ok, count, missing, failures = _operator_drop_readme_status(
        {"providers": ["3dvista", "pano2vr", "krpano"], "prepared_drop_dirs": prepared},
        expected_providers={"3dvista", "pano2vr", "krpano"},
    )

    assert ok is True
    assert count == 3
    assert missing == []
    assert failures == []


def test_gold_status_accepts_operator_readme_artifact_fallback(tmp_path: Path) -> None:
    from scripts.propertyquarry_gold_status import _operator_drop_readme_status

    artifact_readme = tmp_path / "artifacts" / "slug" / "3dvista" / "README.propertyquarry-export.txt"
    artifact_readme.parent.mkdir(parents=True)
    artifact_readme.write_text(
        """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example:
Public gold only passes when verify_property_tour_controls reports ready provider modes
Copy the complete 3DVista export folder
tdvplayer
import_3dvista_export.py
""",
        encoding="utf-8",
    )

    ok, count, missing, failures = _operator_drop_readme_status(
        {
            "providers": ["3dvista"],
            "prepared_drop_dirs": [
                {
                    "provider": "3dvista",
                    "readme": str(tmp_path / "incoming" / "slug" / "3dvista" / "README.propertyquarry-export.txt"),
                    "artifact_readme": str(artifact_readme),
                    "readme_write_error": "PermissionError: drop readme is not writable",
                }
            ],
        },
        expected_providers={"3dvista"},
    )

    assert ok is True
    assert count == 1
    assert missing == []
    assert failures == []


def test_gold_status_accepts_operator_readmes_from_import_rows_when_prepared_rows_are_missing(tmp_path: Path) -> None:
    from scripts.propertyquarry_gold_status import _operator_drop_readme_status

    export_dir = tmp_path / "incoming" / "demo" / "magicfit"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "README.propertyquarry-export.txt").write_text(
        """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example: python /app/scripts/import_magicfit_walkthrough.py --slug demo --video-path drop/magicfit/magicfit-walkthrough.mp4 --source-receipt drop/magicfit/magicfit-receipt.json
Public gold only passes when verify_property_tour_controls reports ready provider modes.
Copy magicfit-walkthrough.mp4 and magicfit-receipt.json into this directory.
""",
        encoding="utf-8",
    )

    ok, count, missing, failures = _operator_drop_readme_status(
        {
            "providers": ["magicfit"],
            "prepared_drop_dirs": [],
            "imports": [
                {
                    "provider": "magicfit",
                    "slug": "demo",
                    "export_dir": str(export_dir),
                    "asset_dir": str(export_dir),
                }
            ],
        },
        expected_providers={"magicfit"},
    )

    assert ok is True
    assert count == 1
    assert missing == []
    assert failures == []


def test_gold_status_blocks_when_performance_receipt_lacks_research_detail_checks(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(include_research_checks=False),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["performance"]["research_detail_checks_ok"] is False
    assert "research_listing_facts" in receipt["performance"]["missing_research_detail_checks"]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "mobile_and_authenticated_surfaces")
    assert "research_mobile_open_property_compact_layout" in blocker["missing_research_detail_checks"]


def test_gold_status_blocks_when_performance_receipt_lacks_search_checks(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(include_search_checks=False),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 2, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["performance"]["search_checks_ok"] is False
    assert receipt["performance"]["missing_search_checks"] == [
        "search_gzip_delivery",
        "search_gzip_vary_accept_encoding",
        "search_compressed_payload_under_budget",
        "what_matters_distance_controls_compact",
        "what_matters_school_distance_controls",
    ]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "mobile_and_authenticated_surfaces")
    assert "what_matters_distance_controls_compact" in blocker["missing_search_checks"]
    assert blocker["status"] == "blocked"
    assert blocker["receipt_status"] == "pass"
    assert blocker["blocking_reason"] == "required_checks_incomplete"


def test_gold_status_blocks_when_performance_receipt_lacks_analytics_privacy_checks(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(include_analytics_checks=False),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 2, "rejected_count": 0})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "blocked"
    assert receipt["analytics"]["status"] == "fail"
    assert receipt["analytics"]["route_count"] == 0
    blocker = next(row for row in receipt["blockers"] if row["area"] == "analytics_privacy")
    assert blocker["missing_checks"][0]["missing_checks"] == [
        "rybbit_no_identify",
        "rybbit_taxonomy_events_only",
        "rybbit_allowed_attributes_only",
        "rybbit_no_private_payload",
    ]


def test_gold_status_blocks_when_operator_drop_readmes_are_stale(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 0, "pano2vr": 0, "krpano": 0, "magicfit": 0},
            "ready_provider_modes": ["matterport"],
            "missing_provider_modes": ["3dvista", "pano2vr", "krpano", "magicfit"],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "blocked_no_verified_exports", "import_count": 0, "rejected_count": 0},
    )
    import_manifest = _write_json(tmp_path / "import-manifest.json", _import_manifest_payload(tmp_path, hardened_readmes=False))
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        import_manifest_receipt_path=import_manifest,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        claim_scope="advanced_visual",
    )

    assert receipt["status"] == "blocked"
    assert receipt["operator_import_manifest"]["ready_for_exports"] is False
    assert receipt["operator_import_manifest"]["hardened_readmes_ok"] is False
    assert sorted(receipt["operator_import_manifest"]["missing_hardened_readme_providers"]) == [
        "3dvista",
        "krpano",
        "magicfit",
        "pano2vr",
    ]
    blocker = next(row for row in receipt["blockers"] if row["area"] == "tour_operator_drop_readmes")
    assert blocker["status"] == "stale_or_missing"
    assert blocker["failures"][0]["status"] == "stale_readme"


def test_gold_status_blocks_when_operator_import_manifest_is_missing(tmp_path: Path) -> None:
    performance = _write_json(
        tmp_path / "performance.json",
        _performance_payload(),
    )
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 4, "rejected_count": 0},
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        import_manifest_receipt_path=tmp_path / "missing-import-manifest.json",
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        claim_scope="advanced_visual",
    )

    assert receipt["status"] == "blocked"
    assert receipt["operator_import_manifest"]["ready_for_exports"] is False
    blocker = next(row for row in receipt["blockers"] if row["area"] == "tour_operator_import_manifest")
    assert blocker["status"] == "missing"


def test_gold_status_does_not_require_operator_drop_prep_when_all_tour_modes_are_ready(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
            "missing_provider_modes": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "ready", "import_count": 0, "rejected_count": 0})
    import_manifest = _write_json(
        tmp_path / "import-manifest.json",
        {
            "status": "pass",
            "import_count": 0,
            "providers": [],
            "drop_status_summary": {"ready_for_import": 0, "waiting_for_assets": 0, "other": 0},
            "prepared_drop_dirs": [],
            "next_command": "python /app/scripts/import_property_tour_exports.py --manifest manifest.json",
        },
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        import_manifest_receipt_path=import_manifest,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        claim_scope="advanced_visual",
    )

    assert receipt["operator_import_manifest"]["ready_for_exports"] is True
    assert receipt["operator_import_manifest"]["missing_prepared_providers"] == []
    assert receipt["operator_import_manifest"]["hardened_readmes_ok"] is True
    assert not any(row["area"] == "tour_operator_import_manifest" for row in receipt["blockers"])


def test_gold_status_treats_blocked_export_discovery_as_ok_when_no_imports_are_needed(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 1},
                "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano", "magicfit"],
                "missing_provider_modes": [],
                "magicfit_playback": _magicfit_playback_payload(),
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "blocked_no_verified_exports", "import_count": 0, "rejected_count": 0},
    )
    import_manifest = _write_json(
        tmp_path / "import-manifest.json",
        {
            "status": "pass",
            "import_count": 0,
            "providers": [],
            "drop_status_summary": {"ready_for_import": 0, "waiting_for_assets": 0, "other": 0},
            "prepared_drop_dirs": [],
            "next_command": "",
        },
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        import_manifest_receipt_path=import_manifest,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "pass"
    assert receipt["operator_import_manifest"]["ready_for_exports"] is True
    assert receipt["export_discovery"]["status"] is None
    assert receipt["next_required_actions"] == []
    assert not any(row["area"] == "tour_operator_import_manifest" for row in receipt["blockers"])
    assert not any(row["area"] == "export_discovery" for row in receipt["blockers"])


def test_gold_status_treats_incomplete_import_manifest_as_ok_when_live_provider_modes_are_ready(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 29, "3dvista": 3, "pano2vr": 2, "krpano": 3, "magicfit": 4},
                "ready_provider_modes": ["3dvista", "krpano", "magicfit", "matterport", "pano2vr"],
                "missing_provider_modes": [],
                "magicfit_playback": _magicfit_playback_payload(),
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "blocked_no_verified_exports", "import_count": 0, "rejected_count": 0},
    )
    import_manifest = _write_json(
        tmp_path / "import-manifest.json",
        {
            "imports": [
                {"provider": "krpano", "slug": "live-tour"},
                {"provider": "magicfit", "slug": "live-tour"},
                {"provider": "pano2vr", "slug": "live-tour"},
            ]
        },
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        import_manifest_receipt_path=import_manifest,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
    )

    assert receipt["status"] == "pass"
    assert receipt["operator_import_manifest"]["ready_for_exports"] is True
    assert not any(row["area"] == "tour_operator_import_manifest" for row in receipt["blockers"])
    assert not any(row["area"] == "tour_export_drop" for row in receipt["blockers"])


def test_gold_status_accepts_optional_fail_closed_id_austria(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 2, "3dvista": 1, "pano2vr": 0, "krpano": 0, "magicfit": 1},
            "provider_blockers": {provider: {"blocked_count": 0, "reasons": []} for provider in ("matterport", "3dvista", "pano2vr", "krpano", "magicfit")},
            "ready_provider_modes": ["matterport", "3dvista", "magicfit"],
            "required_provider_modes": ["matterport", "3dvista", "magicfit"],
            "optional_provider_modes": ["pano2vr", "krpano"],
            "missing_provider_modes": [],
            "magicfit_playback": _magicfit_playback_payload(),
            "delivery_contracts": {},
            "next_required_actions": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "pass"})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    id_austria = _write_json(
        tmp_path / "id-austria.json",
        {
            "provider": "id_austria",
            "status": "disabled",
            "required": False,
            "configured": False,
            "missing_env": [],
            "error": "id_austria_client_id_missing",
            "redirect_uri": "https://propertyquarry.com/id-austria/callback",
        },
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        id_austria_receipt_path=id_austria,
    )

    assert receipt["status"] == "pass"
    assert receipt["id_austria"]["status"] == "disabled"
    assert receipt["id_austria"]["ready"] is True
    assert not any(row["area"] == "id_austria_sign_in" for row in receipt["blockers"])


def test_gold_status_blocks_when_required_id_austria_is_not_configured(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "pass",
            "provider_counts": {"matterport": 2, "3dvista": 1, "pano2vr": 0, "krpano": 0, "magicfit": 1},
            "provider_blockers": {provider: {"blocked_count": 0, "reasons": []} for provider in ("matterport", "3dvista", "pano2vr", "krpano", "magicfit")},
            "ready_provider_modes": ["matterport", "3dvista", "magicfit"],
            "required_provider_modes": ["matterport", "3dvista", "magicfit"],
            "optional_provider_modes": ["pano2vr", "krpano"],
            "missing_provider_modes": [],
            "magicfit_playback": {"playback_ok": True, "playable_count": 1, "ready_count": 1},
            "delivery_contracts": {},
            "next_required_actions": [],
        },
    )
    discovery = _write_json(tmp_path / "discovery.json", {"status": "pass"})
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())
    id_austria = _write_json(
        tmp_path / "id-austria.json",
        {
            "provider": "id_austria",
            "status": "disabled",
            "required": True,
            "configured": False,
            "missing_env": ["PROPERTYQUARRY_ID_AUSTRIA_CLIENT_ID"],
            "error": "id_austria_client_id_missing",
            "redirect_uri": "https://propertyquarry.com/id-austria/callback",
        },
    )

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        id_austria_receipt_path=id_austria,
    )

    assert receipt["status"] == "blocked"
    assert receipt["id_austria"]["status"] == "disabled"
    assert receipt["id_austria"]["ready"] is False
    blocker = next(row for row in receipt["blockers"] if row["area"] == "id_austria_sign_in")
    assert blocker["required"] is True
    assert "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_ID" in blocker["missing_env"]


def test_gold_status_uses_import_rows_as_operator_drop_fallback_for_missing_magicfit_only(tmp_path: Path) -> None:
    performance = _write_json(tmp_path / "performance.json", _performance_payload())
    tour_controls = _write_json(
        tmp_path / "tour-controls.json",
        {
            "status": "blocked_missing_provider_modes",
            "provider_counts": {"matterport": 1, "3dvista": 1, "pano2vr": 1, "krpano": 1, "magicfit": 0},
            "ready_provider_modes": ["matterport", "3dvista", "pano2vr", "krpano"],
            "missing_provider_modes": ["magicfit"],
        },
    )
    discovery = _write_json(
        tmp_path / "discovery.json",
        {"status": "ready", "import_count": 1, "rejected_count": 0},
    )
    export_dir = tmp_path / "incoming" / "demo-flat" / "magicfit"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "README.propertyquarry-export.txt").write_text(
        """
PropertyQuarry provider export drop folder
Do not copy placeholder HTML.
Single-provider dry import example: python /app/scripts/import_magicfit_walkthrough.py --slug demo-flat --video-path drop/magicfit/magicfit-walkthrough.mp4 --source-receipt drop/magicfit/magicfit-receipt.json
Public gold only passes when verify_property_tour_controls reports ready provider modes.
Copy magicfit-walkthrough.mp4 and magicfit-receipt.json into this directory.
""",
        encoding="utf-8",
    )
    import_manifest = _write_json(
        tmp_path / "import-manifest.json",
        {
            "status": "waiting_for_verified_assets",
            "import_count": 1,
            "providers": ["magicfit"],
            "imports": [
                {
                    "provider": "magicfit",
                    "slug": "demo-flat",
                    "title": "Demo Flat",
                    "export_dir": str(export_dir),
                    "asset_dir": str(export_dir),
                    "reason": "missing_magicfit_walkthrough",
                    "action": "render and import a receipt-backed playable MagicFit walkthrough",
                }
            ],
            "drop_status_summary": {"ready_for_import": 0, "waiting_for_assets": 1, "other": 0},
            "prepared_drop_dirs": [],
            "next_command": "python /app/scripts/import_property_tour_exports.py --manifest manifest.json",
        },
    )
    repair_canary = _write_json(
        tmp_path / "repair.json",
        {
            "status": "pass",
            "run_status": "completed_partial",
            "source_repair_status": "returned",
            "receipt_resolution": "provider_quarantined_retry_budget_exhausted",
        },
    )
    provider_matrix = _write_json(tmp_path / "provider-matrix.json", _provider_matrix_payload())

    receipt = build_gold_status_receipt(
        performance_receipt_path=performance,
        tour_control_receipt_path=tour_controls,
        export_discovery_receipt_path=discovery,
        import_manifest_receipt_path=import_manifest,
        repair_canary_receipt_path=repair_canary,
        provider_matrix_receipt_path=provider_matrix,
        claim_scope="advanced_visual",
    )

    assert receipt["status"] == "blocked"
    assert receipt["operator_import_manifest"]["ready_for_exports"] is True
    assert receipt["operator_import_manifest"]["missing_prepared_providers"] == []
    assert receipt["operator_import_manifest"]["hardened_readmes_ok"] is True
    assert {row["area"] for row in receipt["blockers"]} == {
        "advanced_visual_provider_modes",
        "advanced_visual_candidate_binding",
        "magicfit_walkthrough_playback",
        "walkthrough_quality",
        "walkthrough_provider_proof",
    }


def test_gold_status_flagship_rejects_embedded_visual_receipt_digest_tampering(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    generated_at = (now - timedelta(minutes=5)).isoformat()
    flagship_args = _flagship_customer_ux_receipt_args(
        tmp_path,
        generated_at=generated_at,
    )
    continuous_path = Path(flagship_args["continuous_ux_receipt_path"])
    continuous_payload = json.loads(continuous_path.read_text(encoding="utf-8"))
    continuous_payload["visual_baseline"]["browser"]["version"] += "-tampered"
    _write_json(continuous_path, continuous_payload)
    visual_ready, visual_details = gold_status._flagship_continuous_ux_proof(
        continuous_payload,
        expected_release_commit_sha=str(flagship_args["expected_release_commit_sha"]),
    )

    receipt = build_gold_status_receipt(
        **_minimal_gold_receipt_args(tmp_path, generated_at=generated_at),
        **flagship_args,
        readiness_profile="flagship",
        max_receipt_age_hours=1,
        now=now,
    )

    assert receipt["status"] == "blocked"
    assert receipt["continuous_ux"]["flagship_proof_ok"] is False
    assert visual_ready is False
    assert (
        "visual_baseline_receipt_sha256_mismatch"
        in visual_details["contract_errors"]
    )
