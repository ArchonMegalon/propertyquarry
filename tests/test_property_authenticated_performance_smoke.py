from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import yaml

from scripts import propertyquarry_authenticated_performance_smoke as performance_smoke
from scripts.propertyquarry_authenticated_performance_smoke import (
    AUTHENTICATED_PERFORMANCE_SCHEMA,
    PERFORMANCE_SMOKE_MAX_PRICE_EUR,
    build_authenticated_performance_receipt,
    _synthetic_candidate,
    _property_preferences_payload,
    _route_budget_for,
)


SMOKE_SUBPROCESS_TIMEOUT_SECONDS = 120
ROOT = Path(__file__).resolve().parents[1]


def _performance_failure_diagnostics(receipt: dict[str, object]) -> str:
    failed_routes = []
    for route in list(receipt.get("routes") or []):
        if not isinstance(route, dict) or route.get("ok") is True:
            continue
        failed_routes.append(
            {
                "path": route.get("path"),
                "status_code": route.get("status_code"),
                "first_duration_ms": route.get("first_duration_ms"),
                "duration_ms": route.get("duration_ms"),
                "budget_ms": route.get("budget_ms"),
                "cold_budget_ms": route.get("cold_budget_ms"),
                "failed_checks": [
                    check
                    for check in list(route.get("checks") or [])
                    if isinstance(check, dict) and check.get("ok") is not True
                ],
            }
        )
    return json.dumps(
        {
            "status": receipt.get("status"),
            "failed_count": receipt.get("failed_count"),
            "failed_routes": failed_routes,
        },
        ensure_ascii=True,
        sort_keys=True,
    )


def _performance_subprocess_environment() -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "PYTHONPATH",
        "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_PATH",
        "PROPERTYQUARRY_EXPECTED_PERFORMANCE_CHROMIUM_EXECUTABLE_SHA256",
        "PROPERTYQUARRY_EXPECTED_RELEASE_COMMIT_SHA",
        "PROPERTYQUARRY_EXPECTED_RELEASE_DEPLOYMENT_ID",
        "PROPERTYQUARRY_EXPECTED_RELEASE_IMAGE_DIGEST",
        "PROPERTYQUARRY_LIVE_PROBE_SECRET",
        "PROPERTYQUARRY_PERFORMANCE_AUTH_BOOTSTRAP_URL",
        "PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET",
        "PROPERTYQUARRY_PERFORMANCE_TARGET_URL",
    ):
        env.pop(name, None)
    return env


def test_property_authenticated_performance_seeded_preferences_include_budget() -> None:
    payload = _property_preferences_payload()

    assert payload["max_price_eur"] == PERFORMANCE_SMOKE_MAX_PRICE_EUR
    primary_agent = payload["search_agents"][0]
    assert primary_agent["max_price_eur"] == PERFORMANCE_SMOKE_MAX_PRICE_EUR
    assert primary_agent["preferences_json"]["max_price_eur"] == PERFORMANCE_SMOKE_MAX_PRICE_EUR


def test_property_authenticated_performance_synthetic_candidate_preseeds_location_research() -> None:
    candidate = _synthetic_candidate()
    facts = candidate["property_facts"]

    assert candidate["floorplan_url"] == "/assets/propertyquarry/performance-smoke-floorplan.svg"
    assert facts["map_location_precision"] == "address"
    assert facts["map_lat"] == 48.22317
    assert facts["map_lng"] == 16.39594
    assert facts["nearest_playground_m"] == 310
    assert facts["nearest_supermarket_name"] == "BILLA Praterstern"
    assert facts["nearest_subway_name"] == "Praterstern"
    assert facts["nearest_flowing_water_name"] == "Donaukanal"


def test_property_authenticated_performance_smoke_ignores_release_provider_env(
    monkeypatch,
) -> None:
    provider_environment = {
        "EA_RUNTIME_MODE": "prod",
        "TEABLE_BASE_URL": "https://app.teable.ai",
        "TEABLE_API_KEY": "teable-test-key",
        "PROPERTYQUARRY_TEABLE_API_KEY": "propertyquarry-teable-test-key",
        "EA_ENV_TEABLE_BASE_ID": "base-test",
        "ONEMIN_AI_API_KEY": "onemin-live-key",
        "ONEMIN_AI_API_KEY_FALLBACK_1": "onemin-live-fallback",
        "GOOGLE_API_KEY_FALLBACK_1": "vertex-live-fallback",
        "BROWSERACT_API_KEY": "browseract-live-key",
        "EA_RESPONSES_MAGICX_API_KEY": "magicx-live-key",
        "EA_GEMINI_VORTEX_COMMAND": "sh",
    }
    for name, value in provider_environment.items():
        monkeypatch.setenv(name, value)

    reset_environment = performance_smoke._reset_authenticated_performance_smoke_env
    scrubbed_environment_snapshots: list[dict[str, str]] = []

    def _audit_scrubbed_environment() -> None:
        scrubbed_environment_snapshots.append(dict(os.environ))
        reset_environment()

    monkeypatch.setattr(
        performance_smoke,
        "_reset_authenticated_performance_smoke_env",
        _audit_scrubbed_environment,
    )

    route_budget_requests: list[tuple[str, int]] = []

    def _provider_isolation_route_budget(
        path: str,
        *,
        route_budget_ms: int,
    ) -> int:
        route_budget_requests.append((path, route_budget_ms))
        return route_budget_ms

    monkeypatch.setattr(
        performance_smoke,
        "_route_budget_for",
        _provider_isolation_route_budget,
    )

    ip_connect_attempts: list[int] = []
    socket_connect = socket.socket.connect

    def _deny_ip_connect(self: socket.socket, address: object) -> object:
        if self.family in {socket.AF_INET, socket.AF_INET6}:
            ip_connect_attempts.append(self.family)
            raise AssertionError("provider_isolation_external_network_invocation")
        return socket_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", _deny_ip_connect)

    process_attempts: list[str] = []

    def _deny_process(*_args: object, **_kwargs: object) -> object:
        # Keep failure receipts secret-safe: provider commands may contain
        # credentials, so record only that a process launch was attempted.
        process_attempts.append("popen")
        raise AssertionError("provider_isolation_external_process_invocation")

    monkeypatch.setattr(subprocess, "Popen", _deny_process)

    receipt = build_authenticated_performance_receipt(
        route_budget_ms=60_000,
        cold_route_budget_ms=60_000,
    )
    diagnostics = _performance_failure_diagnostics(receipt)
    durations_ms = [
        int(measurement["duration_ms"])
        for route in list(receipt.get("routes") or [])
        if isinstance(route, dict)
        for measurement in list((route.get("measurements") or {}).values())
        if isinstance(measurement, dict)
    ]

    assert receipt["status"] == "pass", diagnostics
    assert receipt["failed_count"] == 0, diagnostics
    assert len(scrubbed_environment_snapshots) == 1
    scrubbed_environment = scrubbed_environment_snapshots[0]
    assert set(provider_environment).isdisjoint(scrubbed_environment)
    assert not any(
        name in performance_smoke.PROVIDER_FREE_ENV_NAMES
        or any(
            name.startswith(prefix)
            for prefix in performance_smoke.PROVIDER_FREE_ENV_PREFIXES
        )
        for name in scrubbed_environment
    )
    assert route_budget_requests
    assert all(budget_ms == 60_000 for _path, budget_ms in route_budget_requests)
    assert not ip_connect_attempts
    assert not process_attempts
    assert len(durations_ms) == int(receipt["route_count"]) * 2
    assert all(duration_ms >= 0 for duration_ms in durations_ms)
    assert any(duration_ms > 0 for duration_ms in durations_ms)
    assert {
        name: os.environ.get(name)
        for name in provider_environment
    } == provider_environment


def test_property_authenticated_performance_smoke_receipt_passes() -> None:
    receipt = build_authenticated_performance_receipt(route_budget_ms=1200)
    diagnostics = _performance_failure_diagnostics(receipt)

    assert receipt["status"] == "pass", diagnostics
    assert receipt["failed_count"] == 0, diagnostics
    assert receipt["schema"] == AUTHENTICATED_PERFORMANCE_SCHEMA
    assert receipt["flagship_status"] == "blocked"
    assert receipt["flagship_blockers"] == [
        "constrained_authenticated_browser_evidence_missing_or_blocked",
        "signed_release_probe_authentication_missing_or_blocked",
        "exact_live_release_identity_missing_or_mismatched",
    ]
    assert receipt["server_request_evidence"]["status"] == "pass"
    assert receipt["constrained_client_evidence"]["status"] == "not_run"
    routes = {str(row["path"]).split("?", 1)[0]: row for row in receipt["routes"]}
    expected_mobile_surfaces = {
        "/sign-in",
        "/app/search",
        "/app/agents",
        "/app/properties",
        "/app/shortlist",
        "/app/research/perf-candidate-1020",
        "/app/alerts",
        "/app/account",
        "/app/billing",
    }
    settings_mobile_surfaces = {
        "/app/settings/google",
        "/app/settings/access",
        "/app/settings/usage",
        "/app/settings/support",
        "/app/settings/trust",
        "/app/settings/invitations",
    }
    assert expected_mobile_surfaces.issubset(routes)
    assert all(tuple(row["measurements"]) == ("cold", "warm") for row in routes.values())
    assert all(
        row["first_duration_ms"] == row["measurements"]["cold"]["duration_ms"]
        and row["duration_ms"] == row["measurements"]["warm"]["duration_ms"]
        for row in routes.values()
    )
    assert routes["/app/agents"]["duration_ms"] <= routes["/app/agents"]["budget_ms"]
    assert routes["/app/research/perf-candidate-1020"]["duration_ms"] <= routes["/app/research/perf-candidate-1020"]["budget_ms"]
    content_first_mobile_surfaces = {
        "/app/agents",
        "/app/alerts",
        "/app/account",
        "/app/billing",
    }
    for route in routes.values():
        check_names = {str(check["name"]): bool(check["ok"]) for check in route["checks"]}
        route_path = str(route["path"]).split("?", 1)[0]
        assert check_names["no_visible_internal_proof_copy"]
        if route_path == "/app/billing" and (
            check_names.get("billing_external_handoff_redirect") or check_names.get("billing_fail_closed_recovery")
            or check_names.get("billing_internal_account_fallback")
        ):
            continue
        assert check_names["mobile_viewport_meta"]
        if route_path == "/sign-in":
            assert check_names["public_auth_surface"]
            continue
        assert check_names["shared_top_navigation"]
        assert check_names["property_app_shell"]
        if route_path in content_first_mobile_surfaces:
            assert check_names["mobile_content_first_surface"]
            assert check_names["mobile_static_switch_suppressed"]
        elif route_path in settings_mobile_surfaces:
            assert check_names["mobile_settings_surface"]
        else:
            assert check_names["mobile_top_navigation_only"]
            assert check_names["mobile_top_navigation_touch_targets"]
        assert check_names["rybbit_no_identify"]
        assert check_names["rybbit_taxonomy_events_only"]
        assert check_names["rybbit_allowed_attributes_only"]
        assert check_names["rybbit_no_private_payload"]
    assert any(check["name"] == "map_only_thumbnails" and check["ok"] for check in routes["/app/agents"]["checks"])
    assert any(check["name"] == "media_requests_explicit" and check["ok"] for check in routes["/app/research/perf-candidate-1020"]["checks"])
    assert any(check["name"] == "research_visual_cards_present" and check["ok"] for check in routes["/app/research/perf-candidate-1020"]["checks"])
    assert any(check["name"] == "research_visual_requests_honest" and check["ok"] for check in routes["/app/research/perf-candidate-1020"]["checks"])
    assert any(check["name"] == "research_no_fake_visual_ready" and check["ok"] for check in routes["/app/research/perf-candidate-1020"]["checks"])
    assert any(check["name"] == "research_listing_facts" and check["ok"] for check in routes["/app/research/perf-candidate-1020"]["checks"])
    assert any(check["name"] == "research_listed_price_signal" and check["ok"] for check in routes["/app/research/perf-candidate-1020"]["checks"])
    assert any(check["name"] == "research_ranking_only_no_compare_cards" and check["ok"] for check in routes["/app/research/perf-candidate-1020"]["checks"])
    assert any(check["name"] == "research_mobile_open_property_compact_layout" and check["ok"] for check in routes["/app/research/perf-candidate-1020"]["checks"])
    assert any(check["name"] == "research_mobile_visual_frame_compact" and check["ok"] for check in routes["/app/research/perf-candidate-1020"]["checks"])
    assert any(check["name"] == "results_ranking_only_no_compare_cards" and check["ok"] for check in routes["/app/properties"]["checks"])
    assert any(check["name"] == "results_ranking_only_no_compare_cards" and check["ok"] for check in routes["/app/shortlist"]["checks"])
    assert any(check["name"] == "results_ranked_not_compare_copy" and check["ok"] for check in routes["/app/properties"]["checks"])
    assert any(check["name"] == "search_gzip_delivery" and check["ok"] for check in routes["/app/search"]["checks"])
    assert any(check["name"] == "search_gzip_vary_accept_encoding" and check["ok"] for check in routes["/app/search"]["checks"])
    payload_budget_check = next(
        check for check in routes["/app/search"]["checks"] if check["name"] == "search_compressed_payload_under_budget"
    )
    assert payload_budget_check["ok"]
    assert 0 < int(payload_budget_check["compressed_bytes"]) <= int(payload_budget_check["max_bytes"])
    assert any(check["name"] == "what_matters_distance_controls_compact" and check["ok"] for check in routes["/app/search"]["checks"])
    assert any(check["name"] == "what_matters_school_distance_controls" and check["ok"] for check in routes["/app/search"]["checks"])
    assert any(check["name"] == "delivery_controls" and check["ok"] for check in routes["/app/alerts"]["checks"])
    assert any(check["name"] == "connected_identity_implicit_account_creation" and check["ok"] for check in routes["/sign-in"]["checks"])
    assert any(check["name"] == "connected_identity_copy_is_customer_safe" and check["ok"] for check in routes["/sign-in"]["checks"])
    assert any(
        check["name"] in {"billing_external_handoff_redirect", "billing_fail_closed_recovery", "billing_internal_account_fallback"} and check["ok"]
        for check in routes["/app/billing"]["checks"]
    )
    assert any(check["name"] == "notification_destination_controls" and check["ok"] for check in routes["/app/account"]["checks"])
    assert any(check["name"] == "notification_primary_channel_controls" and check["ok"] for check in routes["/app/account"]["checks"])
    assert any(check["name"] == "notification_opt_in_copy" and check["ok"] for check in routes["/app/account"]["checks"])
    assert any(check["name"] == "notification_secret_safe" and check["ok"] for check in routes["/app/account"]["checks"])
    assert any(check["name"] == "account_direct_logout_strip" and check["ok"] for check in routes["/app/account"]["checks"])
    assert any(check["name"] == "account_single_logout_action" and check["ok"] for check in routes["/app/account"]["checks"])
    assert any(check["name"] == "account_no_top_dropdown_duplicate_logout" and check["ok"] for check in routes["/app/account"]["checks"])
    assert any(check["name"] == "account_logout_mobile_target" and check["ok"] for check in routes["/app/account"]["checks"])
    assert any(check["name"] == "implicit_account_creation_copy" and check["ok"] for check in routes["/app/settings/google"]["checks"])
    assert any(check["name"] == "account_access_controls" and check["ok"] for check in routes["/app/settings/access"]["checks"])
    assert any(check["name"] == "usage_metrics_visible" and check["ok"] for check in routes["/app/settings/usage"]["checks"])
    assert any(check["name"] == "support_recovery_controls" and check["ok"] for check in routes["/app/settings/support"]["checks"])
    assert any(check["name"] == "trust_evidence_visible" and check["ok"] for check in routes["/app/settings/trust"]["checks"])
    assert any(check["name"] == "invitation_controls_visible" and check["ok"] for check in routes["/app/settings/invitations"]["checks"])


def test_property_authenticated_performance_smoke_script_emits_receipt() -> None:
    env = _performance_subprocess_environment()
    result = subprocess.run(
        [sys.executable, "scripts/propertyquarry_authenticated_performance_smoke.py"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=SMOKE_SUBPROCESS_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stderr
    assert '"status": "pass"' in result.stdout
    assert '"flagship_status": "blocked"' in result.stdout
    assert '"cold"' in result.stdout
    assert '"warm"' in result.stdout
    assert '"constrained_client_evidence"' in result.stdout
    assert '"field_core_web_vitals": false' in result.stdout
    assert '"physical_device_performance": false' in result.stdout
    assert '"/sign-in"' in result.stdout
    assert '"/app/agents"' in result.stdout
    assert '"/app/alerts' in result.stdout
    assert '"/app/settings/google"' in result.stdout
    assert '"/app/settings/access"' in result.stdout
    assert '"/app/settings/usage"' in result.stdout
    assert '"/app/settings/support"' in result.stdout
    assert '"/app/settings/trust"' in result.stdout
    assert '"/app/settings/invitations"' in result.stdout
    assert '"shared_top_navigation"' in result.stdout
    assert '"mobile_top_navigation_only"' in result.stdout
    assert '"mobile_top_navigation_touch_targets"' in result.stdout
    assert '"mobile_content_first_surface"' in result.stdout
    assert '"mobile_static_switch_suppressed"' in result.stdout
    assert '"mobile_settings_surface"' in result.stdout
    assert (
        '"billing_external_handoff_redirect"' in result.stdout
        or '"billing_fail_closed_recovery"' in result.stdout
        or '"billing_internal_account_fallback"' in result.stdout
    )
    assert '"research_mobile_open_property_compact_layout"' in result.stdout
    assert '"research_mobile_visual_frame_compact"' in result.stdout
    assert '"connected_identity_implicit_account_creation"' in result.stdout
    assert '"research_visual_requests_honest"' in result.stdout
    assert '"research_no_fake_visual_ready"' in result.stdout
    assert '"research_ranking_only_no_compare_cards"' in result.stdout
    assert '"results_ranking_only_no_compare_cards"' in result.stdout
    assert '"search_gzip_delivery"' in result.stdout
    assert '"search_gzip_vary_accept_encoding"' in result.stdout
    assert '"search_compressed_payload_under_budget"' in result.stdout
    assert '"what_matters_distance_controls_compact"' in result.stdout
    assert '"what_matters_school_distance_controls"' in result.stdout
    assert '"notification_destination_controls"' in result.stdout
    assert '"account_direct_logout_strip"' in result.stdout
    assert '"account_single_logout_action"' in result.stdout
    assert '"rybbit_taxonomy_events_only"' in result.stdout
    assert '"rybbit_no_private_payload"' in result.stdout
    assert '"no_visible_internal_proof_copy"' in result.stdout


def test_launch_release_gate_requires_secret_safe_constrained_browser_inputs() -> None:
    release_gate = (
        ROOT / "scripts" / "property_release_gates.sh"
    ).read_text(encoding="utf-8")

    assert "PROPERTYQUARRY_PERFORMANCE_TARGET_URL" in release_gate
    assert (
        'expected_release_manifest_sha256="${PROPERTYQUARRY_EXPECTED_RELEASE_MANIFEST_SHA256:-}"'
        in release_gate
    )
    assert (
        'performance_release_probe_secret="${PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET:-${PROPERTYQUARRY_LIVE_PROBE_SECRET:-}}"'
        in release_gate
    )
    bootstrap_unset = "unset PROPERTYQUARRY_PERFORMANCE_AUTH_BOOTSTRAP_URL"
    assert bootstrap_unset in release_gate
    secret_unset = (
        "unset PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET "
        "PROPERTYQUARRY_LIVE_PROBE_SECRET"
    )
    assert secret_unset in release_gate
    unset_index = release_gate.index(secret_unset)
    ea_root_index = release_gate.index('EA_ROOT="$(cd ')
    assert unset_index < ea_root_index
    assert release_gate.index(bootstrap_unset) < ea_root_index
    assert unset_index < release_gate.index("$(")
    performance_command_index = release_gate.index(
        "scripts/propertyquarry_authenticated_performance_smoke.py"
    )
    assert unset_index < performance_command_index
    performance_command_end = release_gate.index(
        "live_mobile_base_url=",
        performance_command_index,
    )
    performance_command_start = release_gate.rfind(
        "PROPERTYQUARRY_PERFORMANCE_TARGET_URL=",
        0,
        performance_command_index,
    )
    assert performance_command_start >= 0
    performance_command = release_gate[
        performance_command_start:performance_command_end
    ]
    assert "PROPERTYQUARRY_PERFORMANCE_RELEASE_PROBE_SECRET=" not in performance_command
    assert "PROPERTYQUARRY_LIVE_PROBE_SECRET=" not in performance_command
    assert "PROPERTYQUARRY_PLAYWRIGHT_CHROMIUM_EXECUTABLE=" not in performance_command
    assert "--release-probe-secret-stdin" in performance_command
    assert (
        '--expected-chromium-executable-path "${expected_performance_chromium_path}"'
        in performance_command
    )
    assert (
        '--expected-chromium-executable-sha256 "${expected_performance_chromium_sha256}"'
        in performance_command
    )
    manifest_binding_flag = (
        '--expected-release-manifest-sha256 "${expected_release_manifest_sha256}"'
    )
    assert manifest_binding_flag in performance_command
    assert release_gate.count(manifest_binding_flag) == 2
    assert (
        '<<<"${performance_release_probe_secret}" >/dev/null'
        in performance_command
    )
    assert 'PROPERTYQUARRY_LIVE_PROBE_SECRET="${performance_release_probe_secret}" \\\n' in release_gate
    assert "unset performance_release_probe_secret" in release_gate
    assert "--constrained-client-authentication-bootstrap-url" not in release_gate

    policy = (ROOT / ".github" / "NO_GITHUB_ACTIONS.md").read_text(encoding="utf-8")
    normalized_policy = " ".join(policy.split())
    workflows_dir = ROOT / ".github" / "workflows"
    assert not workflows_dir.exists() or not any(
        path.suffix.lower() in {".yml", ".yaml"}
        for path in workflows_dir.iterdir()
    )
    assert "governed local Docker host" in normalized_policy
    assert (
        "Adding a `.yml` or `.yaml` file under `.github/workflows/` is a "
        "fail-closed repository-role violation."
        in normalized_policy
    )


def test_secret_bearing_hosted_workflows_are_retired_fail_closed() -> None:
    policy = (ROOT / ".github" / "NO_GITHUB_ACTIONS.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    workflows_dir = ROOT / ".github" / "workflows"
    assert not workflows_dir.exists() or not any(
        path.suffix.lower() in {".yml", ".yaml"}
        for path in workflows_dir.iterdir()
    )
    assert "GitHub as a source repository, not as an execution" in policy
    assert "property-release-gates:" in makefile
    assert "propertyquarry-release-preflight:" in makefile


def test_property_authenticated_performance_smoke_script_writes_receipt(tmp_path) -> None:
    env = _performance_subprocess_environment()
    receipt_path = tmp_path / "property-auth-performance.json"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/propertyquarry_authenticated_performance_smoke.py",
            "--write",
            str(receipt_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=SMOKE_SUBPROCESS_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stderr
    assert receipt_path.exists()
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert payload["status"] == "pass"
    assert payload["failed_count"] == 0
    assert payload["route_count"] == 15
    assert '"status": "pass"' in result.stdout


def test_property_authenticated_performance_smoke_budget_override_applies_to_default_routes() -> None:
    assert _route_budget_for("/app/search", route_budget_ms=250) == 250
    assert _route_budget_for("/sign-in", route_budget_ms=250) == 250
    assert _route_budget_for("/app/agents", route_budget_ms=250) == 250
    assert _route_budget_for("/app/alerts?run_id=abc", route_budget_ms=250) == 250
    assert _route_budget_for("/app/settings/google", route_budget_ms=250) == 250
    assert _route_budget_for("/app/settings/usage", route_budget_ms=250) == 250
    assert _route_budget_for("/app/settings/support", route_budget_ms=250) == 250
    assert _route_budget_for("/app/settings/trust", route_budget_ms=250) == 250
    assert _route_budget_for("/app/settings/invitations", route_budget_ms=250) == 250
    assert _route_budget_for("/app/research/perf-candidate-1020?run_id=abc", route_budget_ms=250) == 250


def test_property_authenticated_performance_smoke_script_rejects_invalid_tight_budget() -> None:
    env = _performance_subprocess_environment()
    result = subprocess.run(
        [sys.executable, "scripts/propertyquarry_authenticated_performance_smoke.py", "--route-budget-ms", "1"],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=SMOKE_SUBPROCESS_TIMEOUT_SECONDS,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "route_budget_ms_out_of_range:50:60000" in result.stderr
