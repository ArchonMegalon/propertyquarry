from __future__ import annotations

import io
import json
from pathlib import Path
from urllib.error import HTTPError

import scripts.propertyquarry_live_mobile_surface_smoke as mobile_smoke
from scripts.propertyquarry_live_mobile_surface_smoke import (
    DEFAULT_ROUTES,
    SEEDED_RESEARCH_DETAIL_ROUTE,
    SEED_FIXTURE_TIMEOUT_SECONDS,
    SEED_FIXTURE_USER_AGENT,
    _resolve_mobile_billing_external_handoff,
    browser_probe_attempt_is_transient,
    browser_probe_attempt_quality,
    browser_probe_checks_are_transient,
    browser_probe_failure_is_transient,
    collect_browser_route_metrics_with_retries,
    _seed_research_detail_headers,
    build_seed_fixture_blocked_receipt,
    build_live_mobile_surface_receipt,
    build_mobile_coverage_checks,
    evaluate_mobile_metrics,
    main,
    route_requires_browser_mobile_probe,
    route_is_research_detail,
    seed_research_detail_fixture,
    routes_require_api_auth,
    seeded_research_detail_payload,
    static_mobile_route_metrics_from_html,
)


BILLING_PORTAL_UNAVAILABLE_BODY = (
    "PropertyQuarry Billing portal unavailable. "
    "The billing portal is still being connected. "
    "Your PropertyQuarry access stays active from the account page."
)

BILLING_PORTAL_LOGIN_REQUIRED_BODY = (
    "PropertyQuarry Billing portal unavailable. "
    "This billing account still opens another sign-in, so PropertyQuarry is keeping it closed for now. "
    "Your PropertyQuarry access stays active from the account page."
)


def _base_metrics() -> dict[str, object]:
    return {
        "status_code": 200,
        "body_width": 390,
        "viewport_width": 390,
        "topbar_height": 72,
        "topnav_visible": True,
        "min_action_height": 46,
        "measured_touch_target_height": 46,
        "visible_card_count": 12,
        "heavy_shadow_count": 0,
        "district_picker_available": True,
        "district_map_popup_available": True,
        "district_list_hidden_in_map_mode": True,
        "district_map_modal_opened": True,
        "district_map_click_selected": True,
        "district_map_zoom_changed": True,
        "district_map_pinch_zoom_changed": True,
        "district_map_close_restored_scroll": True,
        "mobile_what_matters_single_open": True,
        "mobile_fold_single_open": True,
        "mobile_what_matters_page_scroll": True,
        "account_logout_strip_visible": True,
        "logout_button_count": 1,
        "account_menu_present": True,
        "account_menu_mobile_sheet": True,
        "account_menu_trigger_compact": True,
        "research_detail_workspace": True,
        "research_detail_decision_precedes_secondary_content": True,
        "research_detail_media_stage": True,
        "research_detail_visual_controls": True,
        "research_detail_fake_visual_ready": False,
        "research_detail_generated_reconstruction_honest": True,
        "research_detail_tour_copy": True,
        "research_detail_walkthrough_evidence_copy": True,
        "research_detail_no_vague_visual_copy": True,
        "research_detail_walkthrough_magicfit_only": True,
        "research_detail_no_walkthrough_provider_chooser": True,
        "research_detail_no_legacy_walkthrough_providers": True,
        "research_detail_mobile_secondary_collapsed": True,
    }


def _failed_names(
    route: str,
    metrics: dict[str, object],
    *,
    require_billing_available: bool = False,
) -> set[str]:
    return {
        str(row["name"])
        for row in evaluate_mobile_metrics(
            route,
            metrics,
            require_billing_available=require_billing_available,
        )
        if not row["ok"]
    }


def test_live_mobile_smoke_accepts_compact_search_surface_metrics() -> None:
    assert _failed_names("/app/search", _base_metrics()) == set()


def test_live_mobile_smoke_limits_browser_probe_to_interactive_routes() -> None:
    assert route_requires_browser_mobile_probe("/app/search") is True
    assert route_requires_browser_mobile_probe("/app/account") is True
    assert route_requires_browser_mobile_probe("/app/research/cand-1?run_id=run-1") is True
    assert route_requires_browser_mobile_probe("/app/properties") is False
    assert route_requires_browser_mobile_probe("/app/shortlist") is False
    assert route_requires_browser_mobile_probe("/app/settings/google") is False


def test_live_mobile_smoke_retries_only_transient_browser_probe_failures() -> None:
    assert browser_probe_failure_is_transient({"error": "route_timeout:/app/account"}) is True
    assert browser_probe_failure_is_transient({"error": "route_worker_no_receipt:/app/account:exitcode=1"}) is True
    assert browser_probe_failure_is_transient({"error": "TimeoutError: waiting for selector"}) is False
    assert browser_probe_failure_is_transient({"status_code": 500}) is False


def test_live_mobile_smoke_retries_search_scroll_restore_metric_once() -> None:
    checks = [{"name": "district_map_close_restores_scroll", "ok": False}]
    assert browser_probe_checks_are_transient("/app/search", checks) is True
    assert browser_probe_checks_are_transient("/app/account", checks) is False
    assert browser_probe_checks_are_transient(
        "/app/search",
        [
            {"name": "district_map_close_restores_scroll", "ok": False},
            {"name": "district_map_modal_opens", "ok": False},
        ],
    ) is False
    assert browser_probe_attempt_is_transient("/app/search", {"status_code": 200}, checks) is True
    assert browser_probe_attempt_quality({"status_code": 200}, checks) > browser_probe_attempt_quality(
        {"status_code": 0, "error": "route_timeout:/app/search"},
        evaluate_mobile_metrics("/app/search", {"status_code": 0, "error": "route_timeout:/app/search"}),
    )


def test_live_mobile_smoke_browser_probe_keeps_best_attempt_after_transient_timeout() -> None:
    attempts: list[dict[str, object]] = []
    first_metrics = _base_metrics()
    first_metrics["district_map_close_restored_scroll"] = False
    timeout_metrics = {
        "status_code": 0,
        "viewport_width": 390,
        "body_width": 0,
        "topbar_height": 0,
        "min_action_height": 0,
        "error": "route_timeout:/app/search",
    }
    passing_metrics = _base_metrics()

    def _fake_collect(route: str, url: str) -> tuple[int, dict[str, object]]:
        del url
        attempts.append({"route": route})
        if len(attempts) == 1:
            return 200, dict(first_metrics)
        if len(attempts) == 2:
            return 0, dict(timeout_metrics)
        return 200, dict(passing_metrics)

    status_code, metrics, checks = collect_browser_route_metrics_with_retries(
        route="/app/search",
        url="http://propertyquarry.test/app/search",
        collect_once=_fake_collect,
        attempts=3,
    )

    assert len(attempts) == 3
    assert status_code == 200
    assert metrics["status_code"] == 200
    assert [check["name"] for check in checks if not check["ok"]] == []


def test_live_mobile_smoke_browser_worker_uses_selected_shared_engine_runtime(monkeypatch) -> None:
    from types import SimpleNamespace

    from playwright import sync_api
    from scripts import propertyquarry_live_mobile_surface_smoke as smoke

    observed: dict[str, object] = {}

    class Page:
        url = "https://propertyquarry.test/app/search"

        def set_default_timeout(self, _timeout: int) -> None:
            pass

        def set_default_navigation_timeout(self, _timeout: int) -> None:
            pass

        def goto(self, *_args, **_kwargs):
            return SimpleNamespace(status=200)

        def wait_for_load_state(self, *_args, **_kwargs) -> None:
            pass

        def wait_for_timeout(self, _timeout: int) -> None:
            pass

        def evaluate(self, _script: str) -> dict[str, object]:
            return {"viewport_width": 390}

    class Context:
        def route(self, pattern: str, handler) -> None:
            observed["route_pattern"] = pattern
            observed["route_handler"] = handler

        def new_page(self) -> Page:
            return Page()

        def close(self) -> None:
            pass

    class Browser:
        def new_context(self, **kwargs):
            observed["context"] = kwargs
            return Context()

        def close(self) -> None:
            pass

    class BrowserType:
        def launch(self, **kwargs):
            observed["launch"] = kwargs
            return Browser()

    class PlaywrightContext:
        def __enter__(self):
            return SimpleNamespace(webkit=BrowserType())

        def __exit__(self, *_args) -> None:
            pass

    class Queue:
        def put(self, payload: dict[str, object]) -> None:
            observed["payload"] = payload

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: PlaywrightContext())
    monkeypatch.setattr(
        smoke,
        "playwright_engine_launch_kwargs",
        lambda _playwright, *, engine, args: {
            "headless": True,
            "executable_path": f"/configured/{engine}",
        },
    )

    smoke._playwright_route_metrics_worker(
        Queue(),
        url="https://propertyquarry.test/app/search",
        headers={"X-EA-Principal-ID": "test"},
        authorized_origin="https://propertyquarry.test",
        browser_args=["--no-sandbox"],
        viewport_width=390,
        viewport_height=844,
        route_timeout_ms=5_000,
        browser_engine="webkit",
    )

    assert observed["launch"] == {
        "headless": True,
        "executable_path": "/configured/webkit",
    }
    assert observed["context"].get("extra_http_headers") is None
    assert observed["route_pattern"] == "**/*"
    assert observed["payload"] == {
        "ok": True,
        "status_code": 200,
        "metrics": {
            "viewport_width": 390,
            "viewport_height": 844,
            "browser_probe": True,
            "browser_engine": "webkit",
            "proof_mode": "playwright",
            "navigation_committed": True,
            "requested_url": "https://propertyquarry.test/app/search",
            "final_url": "https://propertyquarry.test/app/search",
        },
    }


def test_live_mobile_smoke_accepts_static_html_probe_for_simple_routes() -> None:
    metrics = static_mobile_route_metrics_from_html(
        html='<nav aria-label="PropertyQuarry sections"></nav><main><section class="pqx-card"></section></main>',
        status_code=200,
        viewport_width=390,
    )

    assert _failed_names("/app/properties", metrics) == set()
    assert metrics["static_html_probe"] is True


def test_live_mobile_smoke_standard_mode_keeps_simple_routes_on_static_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        mobile_smoke,
        "_http_get_for_smoke",
        lambda *_args, **_kwargs: {
            "status_code": 200,
            "headers": {},
            "url": "http://localhost:8097/app/properties",
            "text": '<nav aria-label="PropertyQuarry sections"></nav><main><a class="pqx-button">Open</a></main>',
        },
    )
    monkeypatch.setattr(
        mobile_smoke,
        "collect_playwright_route_metrics",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("standard simple route must not launch Playwright")),
    )
    monkeypatch.setattr(
        mobile_smoke,
        "_registry_mobile_surface_coverage_checks",
        lambda **_kwargs: [{"name": "registry_mobile_customer_surfaces_covered", "ok": True}],
    )

    receipt = build_live_mobile_surface_receipt(
        base_url="http://localhost:8097",
        api_token="secret-token",
        principal_id="pq-mobile-test",
        routes=("/app/properties",),
    )

    assert receipt["status"] == "pass"
    assert receipt["proof_mode"] == mobile_smoke.STANDARD_PROOF_MODE
    assert receipt["routes"][0]["proof_mode"] == "static_html"


def test_live_mobile_smoke_flagship_browser_all_probes_every_route_and_viewport(monkeypatch) -> None:
    calls: list[tuple[str, str, int, int]] = []

    def fake_browser_probe(**kwargs):
        calls.append((kwargs["browser_engine"], kwargs["route"], kwargs["viewport_width"], kwargs["viewport_height"]))
        metrics = _base_metrics()
        metrics.update(
            {
                "viewport_width": kwargs["viewport_width"],
                "viewport_height": kwargs["viewport_height"],
                "body_width": kwargs["viewport_width"],
                "browser_probe": True,
                "browser_engine": kwargs["browser_engine"],
                "proof_mode": "playwright",
                "navigation_committed": True,
                "touch_capable": True,
                "focus_navigation_ok": True,
                "requested_url": kwargs["url"],
                "final_url": kwargs["url"],
            }
        )
        return 200, metrics

    monkeypatch.setattr(mobile_smoke, "collect_playwright_route_metrics", fake_browser_probe)
    monkeypatch.setattr(
        mobile_smoke,
        "_registry_mobile_surface_coverage_checks",
        lambda **_kwargs: [{"name": "registry_mobile_customer_surfaces_covered", "ok": True}],
    )
    routes = (
        "/app/properties",
        "/app/settings/access",
        "/app/research/current-result?run_id=run-flagship",
    )

    receipt = build_live_mobile_surface_receipt(
        base_url="http://localhost:8097",
        api_token="secret-token",
        principal_id="pq-mobile-test",
        routes=routes,
        proof_mode="browser-all",
        supported_viewports=((390, 844), (412, 915)),
    )

    assert receipt["status"] == "pass"
    assert receipt["proof_mode"] == mobile_smoke.FLAGSHIP_PROOF_MODE
    assert receipt["browser_proof"]["ready"] is True
    assert receipt["browser_proof"]["proven_sample_count"] == 18
    assert receipt["required_browser_engines"] == ["chromium", "firefox", "webkit"]
    assert receipt["browser_proof"]["missing_browser_engines"] == []
    assert set(calls) == {
        (engine, route, width, height)
        for engine in mobile_smoke.FLAGSHIP_BROWSER_ENGINES
        for route in routes
        for width, height in ((390, 844), (412, 915))
    }
    assert all(row["proof_mode"] == "playwright" for row in receipt["routes"])


def test_live_mobile_smoke_flagship_browser_all_rejects_static_probe_fallback(monkeypatch) -> None:
    def fake_static_fallback(**kwargs):
        metrics = _base_metrics()
        metrics.update(
            {
                "viewport_width": kwargs["viewport_width"],
                "viewport_height": kwargs["viewport_height"],
                "body_width": kwargs["viewport_width"],
                "proof_mode": "static_html",
                "static_html_probe": True,
            }
        )
        return 200, metrics

    monkeypatch.setattr(mobile_smoke, "collect_playwright_route_metrics", fake_static_fallback)
    monkeypatch.setattr(
        mobile_smoke,
        "_registry_mobile_surface_coverage_checks",
        lambda **_kwargs: [{"name": "registry_mobile_customer_surfaces_covered", "ok": True}],
    )

    receipt = build_live_mobile_surface_receipt(
        base_url="http://localhost:8097",
        api_token="secret-token",
        principal_id="pq-mobile-test",
        routes=("/app/properties", "/app/research/current-result?run_id=run-flagship"),
        proof_mode="flagship",
        supported_viewports=((390, 844),),
    )

    assert receipt["status"] == "fail"
    assert receipt["browser_proof"]["ready"] is False
    assert receipt["browser_proof"]["static_fallbacks"]
    proof_check = next(
        row for row in receipt["coverage_checks"]
        if row["name"] == "flagship_browser_all_playwright_proof"
    )
    assert proof_check["ok"] is False


def test_live_mobile_smoke_resolves_signed_redirect_before_browser_navigation(monkeypatch) -> None:
    browser_urls: list[str] = []

    monkeypatch.setattr(
        mobile_smoke,
        "_http_get_for_smoke",
        lambda url, **_kwargs: {
            "status_code": 200,
            "headers": {},
            "url": (
                "https://propertyquarry.com/app/search"
                if url.endswith("/app/properties")
                else url
            ),
            "text": "",
        },
    )

    def fake_browser_probe(**kwargs):
        browser_urls.append(str(kwargs["url"]))
        metrics = _base_metrics()
        metrics.update(
            {
                "browser_probe": True,
                "browser_engine": kwargs["browser_engine"],
                "proof_mode": "playwright",
                "navigation_committed": True,
                "touch_capable": True,
                "focus_navigation_ok": True,
                "requested_url": kwargs["url"],
                "final_url": kwargs["url"],
            }
        )
        return 200, metrics

    monkeypatch.setattr(mobile_smoke, "collect_playwright_route_metrics", fake_browser_probe)
    monkeypatch.setattr(
        mobile_smoke,
        "_registry_mobile_surface_coverage_checks",
        lambda **_kwargs: [{"name": "registry_mobile_customer_surfaces_covered", "ok": True}],
    )

    receipt = build_live_mobile_surface_receipt(
        base_url="https://propertyquarry.com",
        api_token="",
        principal_id="propertyquarry-release-probe",
        release_probe_secret="dedicated-probe-secret",
        routes=(
            "/app/properties",
            "/app/research/current-result?run_id=run-flagship",
        ),
        proof_mode="flagship",
        supported_viewports=((390, 844),),
        required_browser_engines=("chromium",),
    )

    assert receipt["status"] == "pass"
    assert browser_urls == [
        "https://propertyquarry.com/app/search",
        "https://propertyquarry.com/app/research/current-result?run_id=run-flagship",
    ]
    assert receipt["routes"][0]["metrics"]["release_probe_redirect_resolved"] is True


def test_live_mobile_smoke_flagship_billing_is_strict_only_for_paid_persona(monkeypatch) -> None:
    monkeypatch.setattr(
        mobile_smoke,
        "_http_get_for_smoke",
        lambda url, **_kwargs: {
            "status_code": 503 if url.endswith("/app/billing") else 200,
            "headers": {},
            "url": url,
            "text": (
                BILLING_PORTAL_UNAVAILABLE_BODY
                if url.endswith("/app/billing")
                else ""
            ),
        },
    )

    def fake_browser_probe(**kwargs):
        metrics = _base_metrics()
        status_code = 503 if kwargs["route"] == "/app/billing" else 200
        metrics.update(
            {
                "status_code": status_code,
                "browser_probe": True,
                "browser_engine": kwargs["browser_engine"],
                "proof_mode": "playwright",
                "navigation_committed": True,
                "touch_capable": True,
                "focus_navigation_ok": True,
            }
        )
        return status_code, metrics

    monkeypatch.setattr(mobile_smoke, "collect_playwright_route_metrics", fake_browser_probe)
    monkeypatch.setattr(
        mobile_smoke,
        "_registry_mobile_surface_coverage_checks",
        lambda **_kwargs: [{"name": "registry_mobile_customer_surfaces_covered", "ok": True}],
    )
    kwargs = {
        "base_url": "https://propertyquarry.com",
        "api_token": "",
        "principal_id": "propertyquarry-release-probe",
        "release_probe_secret": "dedicated-probe-secret",
        "routes": (
            "/app/billing",
            "/app/research/current-result?run_id=run-flagship",
        ),
        "proof_mode": "flagship",
        "supported_viewports": ((390, 844),),
        "required_browser_engines": ("chromium",),
    }

    free_receipt = build_live_mobile_surface_receipt(
        **kwargs,
        expected_plan_label="Free standard research",
    )
    paid_receipt = build_live_mobile_surface_receipt(
        **kwargs,
        expected_plan_label="Agent",
    )

    assert free_receipt["status"] == "pass"
    assert free_receipt["billing_readiness"]["paid_persona"] is False
    assert free_receipt["billing_readiness"]["strict_required"] is False
    assert paid_receipt["status"] == "fail"
    assert paid_receipt["billing_readiness"]["paid_persona"] is True
    assert paid_receipt["billing_readiness"]["strict_required"] is True


def test_propertyquarry_mobile_topnav_fallback_keeps_touch_targets_at_least_44px() -> None:
    template = (
        Path(__file__).resolve().parents[1]
        / "ea/app/templates/app/property_decision_workbench.html"
    ).read_text(encoding="utf-8")

    assert "item.style.setProperty('min-height', '44px', 'important');" in template


def test_live_mobile_smoke_accepts_empty_shortlist_with_top_navigation_only() -> None:
    metrics = _base_metrics()

    assert _failed_names("/app/shortlist", metrics) == set()


def test_live_mobile_smoke_accepts_external_billing_handoff() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "status_code": 303,
            "redirect_location": "https://billing.propertyquarry.com/account",
            "billing_handoff_host_resolves": True,
            "billing_handoff_usable": True,
        }
    )

    assert _failed_names("/app/billing", metrics) == set()
    assert metrics["billing_readiness_state"] == "available"
    assert _failed_names("/app/billing", metrics, require_billing_available=True) == set()


def test_live_mobile_smoke_accepts_fail_closed_billing_recovery() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "status_code": 503,
            "billing_visible_text": BILLING_PORTAL_UNAVAILABLE_BODY,
        }
    )

    assert _failed_names("/app/billing", metrics) == set()
    assert metrics["billing_readiness_state"] == "unavailable"
    assert _failed_names("/app/billing", metrics, require_billing_available=True) == {
        "billing_flagship_no_second_login_handoff"
    }


def test_live_mobile_smoke_accepts_internal_account_fallback_redirect() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "status_code": 303,
            "redirect_location": "/app/account?billing=1#delivery",
        }
    )

    assert _failed_names("/app/billing", metrics) == set()
    assert metrics["billing_readiness_state"] == "degraded"
    assert _failed_names("/app/billing", metrics, require_billing_available=True) == {
        "billing_flagship_no_second_login_handoff"
    }


def test_live_mobile_smoke_accepts_login_required_fail_closed_billing_recovery() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "status_code": 503,
            "billing_visible_text": BILLING_PORTAL_LOGIN_REQUIRED_BODY,
        }
    )

    assert _failed_names("/app/billing", metrics) == set()


def test_live_mobile_smoke_rejects_local_billing_page() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "status_code": 503,
            "billing_visible_text": "PropertyQuarry Plan Agent Billing history Compare plans View plans",
        }
    )

    assert _failed_names("/app/billing", metrics) == {
        "billing_fail_closed_recovery",
        "billing_local_page_deleted",
    }


def test_live_mobile_smoke_rejects_local_billing_redirect_loop() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "status_code": 303,
            "redirect_location": "/app/billing",
            "billing_handoff_host_resolves": False,
            "billing_handoff_usable": False,
        }
    )

    assert _failed_names("/app/billing", metrics) == {
        "billing_external_handoff",
        "billing_external_handoff_resolves",
        "billing_external_handoff_usable",
    }


def test_live_mobile_smoke_rejects_billing_handoff_that_requires_second_login() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "status_code": 303,
            "redirect_location": "https://billing.propertyquarry.com/account",
            "billing_handoff_host_resolves": True,
            "billing_handoff_usable": False,
        }
    )

    assert _failed_names("/app/billing", metrics) == {"billing_external_handoff_usable"}


def test_live_mobile_smoke_resolves_signed_bridge_launch_to_external_billing_host(monkeypatch) -> None:  # noqa: ANN001
    def _fake_http_get(  # noqa: ANN001
        url: str,
        *,
        headers=None,
        timeout_seconds=0,
        follow_redirects=True,
        authorized_origin="",
    ):
        assert url == "http://localhost:8097/app/api/property/billing/bridge-launch"
        assert headers == {"Host": "propertyquarry.com"}
        assert timeout_seconds == 5.0
        assert follow_redirects is False
        assert authorized_origin == "http://localhost:8097"
        return {
            "status_code": 303,
            "headers": {"location": "https://billing.propertyquarry.com/sso/propertyquarry?pq_bridge=token"},
            "url": url,
            "text": "",
        }

    monkeypatch.setattr(mobile_smoke, "_http_get_for_smoke", _fake_http_get)

    resolved = _resolve_mobile_billing_external_handoff(
        base_url="http://localhost:8097",
        redirect_location="/app/api/property/billing/bridge-launch",
        request_headers={"Host": "propertyquarry.com"},
        timeout_ms=5000,
    )

    assert resolved == {
        "external_location": "https://billing.propertyquarry.com/sso/propertyquarry?pq_bridge=token",
        "bridge_launch_used": True,
        "bridge_launch_url": "http://localhost:8097/app/api/property/billing/bridge-launch",
        "bridge_launch_status_code": 303,
    }


def test_live_mobile_smoke_redacts_sensitive_billing_urls_in_receipts() -> None:
    redacted = mobile_smoke._redact_sensitive_receipt_value(
        {
            "redirect_location": (
                "https://billing.propertyquarry.com/login/token/mobile-secret/home"
                "?state=mobile-state&code=mobile-code&pq_bridge=mobile-bridge"
            ),
            "nested": [
                {
                    "url": "https://billing.propertyquarry.com/sso/propertyquarry?token=session-secret",
                }
            ],
        }
    )
    serialized = json.dumps(redacted, sort_keys=True)

    assert "mobile-secret" not in serialized
    assert "mobile-state" not in serialized
    assert "mobile-code" not in serialized
    assert "mobile-bridge" not in serialized
    assert "session-secret" not in serialized
    assert "/login/token/[redacted]/home" in serialized
    assert "state=[redacted]" in serialized
    assert "code=[redacted]" in serialized
    assert "pq_bridge=[redacted]" in serialized
    assert "token=[redacted]" in serialized


def test_live_mobile_smoke_accepts_research_and_packets_surfaces_without_search_controls() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "district_picker_available": False,
            "district_map_popup_available": False,
            "district_list_hidden_in_map_mode": False,
        }
    )

    assert _failed_names("/app/research", metrics) == set()
    assert _failed_names("/app/properties/packets", metrics) == set()


def test_live_mobile_smoke_requires_real_research_detail_layout() -> None:
    assert _failed_names("/app/research/perf-candidate-1020?run_id=run-gold", _base_metrics()) == set()
    metrics = _base_metrics()
    metrics.update(
        {
            "research_detail_workspace": False,
            "research_detail_decision_precedes_secondary_content": False,
            "research_detail_media_stage": False,
            "research_detail_visual_controls": False,
            "research_detail_fake_visual_ready": True,
            "research_detail_generated_reconstruction_honest": False,
            "research_detail_tour_copy": False,
            "research_detail_walkthrough_evidence_copy": False,
            "research_detail_no_vague_visual_copy": False,
            "research_detail_walkthrough_magicfit_only": False,
            "research_detail_no_walkthrough_provider_chooser": False,
            "research_detail_no_legacy_walkthrough_providers": False,
            "research_detail_mobile_secondary_collapsed": False,
        }
    )

    assert _failed_names("/app/research/perf-candidate-1020?run_id=run-gold", metrics) == {
        "research_detail_workspace",
        "research_detail_decision_precedes_secondary_content",
        "research_detail_media_stage",
        "research_detail_visual_controls",
        "research_detail_no_fake_visual_ready",
        "research_detail_generated_reconstruction_honest",
        "research_detail_tour_copy",
        "research_detail_walkthrough_evidence_copy",
        "research_detail_no_vague_visual_copy",
        "research_detail_walkthrough_magicfit_only",
        "research_detail_no_walkthrough_provider_chooser",
        "research_detail_no_legacy_walkthrough_providers",
        "research_detail_mobile_secondary_collapsed",
    }


def test_live_mobile_smoke_rejects_generated_reconstruction_without_verified_tour_path() -> None:
    metrics = _base_metrics()
    metrics["research_detail_generated_reconstruction_honest"] = False

    assert _failed_names("/app/research/perf-candidate-1020?run_id=run-gold", metrics) == {
        "research_detail_generated_reconstruction_honest",
    }


def test_live_mobile_smoke_rejects_vague_research_detail_visual_copy() -> None:
    metrics = _base_metrics()
    metrics["research_detail_no_vague_visual_copy"] = False

    assert _failed_names("/app/research/perf-candidate-1020?run_id=run-gold", metrics) == {
        "research_detail_no_vague_visual_copy",
    }


def test_live_mobile_smoke_requires_compact_mobile_research_detail_secondary_sections() -> None:
    metrics = _base_metrics()
    metrics["research_detail_mobile_secondary_collapsed"] = False

    assert _failed_names("/app/research/perf-candidate-1020?run_id=run-gold", metrics) == {
        "research_detail_mobile_secondary_collapsed",
    }


def test_live_mobile_smoke_requires_magicfit_only_walkthrough_controls() -> None:
    metrics = _base_metrics()
    metrics["research_detail_walkthrough_magicfit_only"] = False

    assert _failed_names("/app/research/perf-candidate-1020?run_id=run-gold", metrics) == {
        "research_detail_walkthrough_magicfit_only",
    }


def test_live_mobile_smoke_rejects_walkthrough_provider_chooser_and_legacy_provider_noise() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "research_detail_no_walkthrough_provider_chooser": False,
            "research_detail_no_legacy_walkthrough_providers": False,
        }
    )

    assert _failed_names("/app/research/perf-candidate-1020?run_id=run-gold", metrics) == {
        "research_detail_no_walkthrough_provider_chooser",
        "research_detail_no_legacy_walkthrough_providers",
    }


def test_live_mobile_smoke_default_routes_cover_representative_customer_surfaces() -> None:
    assert {
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
        "/app/research",
        "/app/properties/packets",
    }.issubset(set(DEFAULT_ROUTES))


def test_live_mobile_smoke_blocks_app_routes_without_api_token_before_playwright() -> None:
    assert routes_require_api_auth(("/app/search",)) is True
    assert routes_require_api_auth(("/pricing",)) is False

    receipt = build_live_mobile_surface_receipt(
        base_url="http://localhost:8097",
        api_token="",
        principal_id="pq-live-mobile-smoke",
        routes=("/app/search",),
    )

    assert receipt["status"] == "blocked"
    assert receipt["routes"] == []
    assert receipt["coverage_checks"] == [
        {
            "name": "api_token_present_for_app_routes",
            "ok": False,
            "reason": "Live mobile app-surface smoke requires EA_API_TOKEN or --api-token; otherwise protected pages render sign-in redirects instead of the app UI.",
        }
    ]


def test_live_mobile_smoke_can_require_current_research_detail_route() -> None:
    assert route_is_research_detail("/app/research") is False
    assert route_is_research_detail("/app/research/current-result?run_id=run-gold") is True

    missing = build_mobile_coverage_checks(DEFAULT_ROUTES, require_research_detail=True)
    assert missing[0] == {
        "name": "research_detail_route_configured",
        "ok": False,
        "required_route_prefix": "/app/research/",
        "reason": "Gold mobile smoke must exercise a current live research detail page, not only /app/research.",
    }
    registry_check = missing[1]
    assert registry_check["name"] == "registry_mobile_customer_surfaces_covered"
    assert registry_check["ok"] is False
    assert set(registry_check["missing_surface_keys"]) == {
        "property_research_detail",
        "floorplan_and_tour_control",
        "video_walkthrough",
    }

    covered = build_mobile_coverage_checks(
        (*DEFAULT_ROUTES, "/app/research/current-result?run_id=run-gold"),
        require_research_detail=True,
    )
    assert covered == [
        {
            "name": "research_detail_route_configured",
            "ok": True,
            "required_route_prefix": "/app/research/",
            "reason": "Gold mobile smoke must exercise a current live research detail page, not only /app/research.",
        },
        {
            "name": "registry_mobile_customer_surfaces_covered",
            "ok": True,
            "covered_surface_count": 18,
            "missing_surface_keys": [],
            "reason": "Live mobile smoke routes must cover every customer-visible /app surface declared in the PropertyQuarry surface registry.",
        },
    ]

def test_live_mobile_smoke_rejects_missing_registry_mobile_surface() -> None:
    routes_without_run_home = tuple(route for route in DEFAULT_ROUTES if route != "/app/properties")

    checks = build_mobile_coverage_checks(routes_without_run_home, require_research_detail=False)
    registry_check = next(check for check in checks if check["name"] == "registry_mobile_customer_surfaces_covered")

    assert registry_check["ok"] is False
    assert registry_check["missing_surface_keys"] == ["run_home", "fleet_repair"]


def test_live_mobile_smoke_seeded_research_detail_payload_is_valid_detail_fixture() -> None:
    payload = seeded_research_detail_payload()
    candidates = list(payload["saved_shortlist_candidates"])
    candidate = dict(candidates[0])

    assert route_is_research_detail(SEEDED_RESEARCH_DETAIL_ROUTE) is True
    assert payload["location_query"] == "1020 Vienna"
    assert candidate["candidate_ref"] == "perf-candidate-1020"
    assert candidate["saved_from_run_id"] == "run-gold-mobile"
    assert candidate["packet_url"] == "/app/research/perf-candidate-1020"
    assert dict(candidate["property_facts"])["listing_fact_confirmation"]["status"] == "confirmed"


def test_live_mobile_smoke_seed_headers_include_public_edge_safe_metadata() -> None:
    headers = _seed_research_detail_headers(
        base_url="https://propertyquarry.com",
        api_token="secret-token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        host_header="propertyquarry.com",
    )

    assert headers == {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": SEED_FIXTURE_USER_AGENT,
        "X-EA-Principal-ID": "cf-email:tibor.girschele@gmail.com",
        "Origin": "https://propertyquarry.com",
        "Referer": "https://propertyquarry.com/app/search",
        "Host": "propertyquarry.com",
        "Authorization": "Bearer secret-token",
        "X-EA-API-Token": "secret-token",
        "X-API-Token": "secret-token",
    }


def test_live_mobile_smoke_seed_fixture_posts_with_browser_like_headers(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        status = 200

        def read(self, _size: int = -1) -> bytes:
            return b'{"ok":true}'

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    def fake_urlopen(request, timeout: int = 0):
        captured["timeout"] = timeout
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {key.title(): value for key, value in request.header_items()}
        captured["body"] = json.loads((request.data or b"{}").decode("utf-8"))
        return _Response()

    monkeypatch.setattr(mobile_smoke._HTTP_SMOKE_NO_REDIRECT_OPENER, "open", fake_urlopen)

    route = seed_research_detail_fixture(
        base_url="https://propertyquarry.com",
        api_token="secret-token",
        principal_id="cf-email:tibor.girschele@gmail.com",
        host_header="propertyquarry.com",
    )

    assert route == SEEDED_RESEARCH_DETAIL_ROUTE
    assert captured["timeout"] == SEED_FIXTURE_TIMEOUT_SECONDS
    assert captured["url"] == "https://propertyquarry.com/v1/onboarding/property-search/preferences"
    assert captured["method"] == "POST"
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": SEED_FIXTURE_USER_AGENT,
        "X-Ea-Principal-Id": "cf-email:tibor.girschele@gmail.com",
        "Origin": "https://propertyquarry.com",
        "Referer": "https://propertyquarry.com/app/search",
        "Host": "propertyquarry.com",
        "Authorization": "Bearer secret-token",
        "X-Ea-Api-Token": "secret-token",
        "X-Api-Token": "secret-token",
    }
    candidate = dict(captured["body"]["saved_shortlist_candidates"][0])
    assert candidate["candidate_ref"] == "perf-candidate-1020"


def test_live_mobile_smoke_seed_fixture_raises_for_http_error(monkeypatch) -> None:
    def fake_urlopen(request, timeout: int = 0):
        raise HTTPError(
            url=request.full_url,
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"forbidden"),
        )

    monkeypatch.setattr(mobile_smoke._HTTP_SMOKE_NO_REDIRECT_OPENER, "open", fake_urlopen)

    try:
        seed_research_detail_fixture(
            base_url="https://propertyquarry.com",
            api_token="secret-token",
            principal_id="cf-email:tibor.girschele@gmail.com",
            host_header="propertyquarry.com",
        )
    except HTTPError as exc:
        assert exc.code == 403
    else:  # pragma: no cover - guard against silently swallowing live seeding failures.
        raise AssertionError("expected HTTPError")


def test_live_mobile_smoke_builds_blocked_receipt_when_seed_fixture_cannot_be_created() -> None:
    receipt = build_seed_fixture_blocked_receipt(
        base_url="https://propertyquarry.com",
        host_header="propertyquarry.com",
        principal_id="pq-live-mobile-smoke",
        viewport_width=390,
        viewport_height=844,
        error="seed_research_detail_fixture_failed:TimeoutError: timed out",
    )

    assert receipt["status"] == "blocked"
    assert receipt["route_count"] == 0
    assert receipt["failed_count"] == 1
    assert receipt["error"] == "seed_research_detail_fixture_failed:TimeoutError: timed out"
    assert receipt["coverage_checks"] == [
        {
            "name": "research_detail_seed_fixture_ready",
            "ok": False,
            "reason": "Live mobile smoke could not seed the saved research-detail fixture, so it cannot honestly prove the open-property surface.",
            "error": "seed_research_detail_fixture_failed:TimeoutError: timed out",
        }
    ]


def test_live_mobile_smoke_seed_fixture_blocked_receipt_redacts_reflected_api_token() -> None:
    api_token = "sentinel-reflected-seed-token"
    receipt = build_seed_fixture_blocked_receipt(
        base_url="https://propertyquarry.com",
        host_header="propertyquarry.com",
        principal_id="pq-live-mobile-smoke",
        viewport_width=390,
        viewport_height=844,
        error=f"seed_research_detail_fixture_failed: upstream reflected {api_token}",
        api_token=api_token,
    )

    serialized = json.dumps(receipt, sort_keys=True)
    assert api_token not in serialized
    assert "[redacted-secret]" in serialized


def test_live_mobile_smoke_main_writes_blocked_receipt_when_seed_fixture_times_out(monkeypatch, tmp_path) -> None:
    out_path = tmp_path / "live-mobile-timeout.json"

    monkeypatch.setattr(
        "scripts.propertyquarry_live_mobile_surface_smoke.seed_research_detail_fixture",
        lambda **kwargs: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    monkeypatch.setattr(
        "scripts.propertyquarry_live_mobile_surface_smoke.build_live_mobile_surface_receipt",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("receipt builder should not run when seeding fails")),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "propertyquarry_live_mobile_surface_smoke.py",
            "--base-url",
            "https://propertyquarry.com",
            "--api-token",
            "secret-token",
            "--seed-research-detail-fixture",
            "--write",
            str(out_path),
        ],
    )

    exit_code = main()

    assert exit_code == 1
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["error"] == "seed_research_detail_fixture_failed:TimeoutError: timed out"


def test_live_mobile_smoke_rejects_horizontal_overflow_and_noisy_chrome() -> None:
    metrics = _base_metrics()
    metrics.update({"body_width": 420, "topbar_height": 140, "heavy_shadow_count": 5})

    assert _failed_names("/app/search", metrics) == {
        "no_horizontal_overflow",
        "compact_topbar",
        "low_shadow_noise",
    }


def test_live_mobile_smoke_requires_search_district_picker_popup() -> None:
    metrics = _base_metrics()
    metrics.update({"district_map_popup_available": False, "district_list_hidden_in_map_mode": False})

    assert _failed_names("/app/search", metrics) == {
        "district_map_popup_available",
        "district_list_not_visible_in_map_mode",
    }


def test_live_mobile_smoke_requires_interactive_search_district_map() -> None:
    metrics = _base_metrics()
    metrics.update(
        {
            "district_map_modal_opened": False,
            "district_map_click_selected": False,
            "district_map_zoom_changed": False,
            "district_map_pinch_zoom_changed": False,
            "district_map_close_restored_scroll": False,
        }
    )

    assert _failed_names("/app/search", metrics) == {
        "district_map_modal_opens",
        "district_map_click_selects_shape",
        "district_map_zoom_toggle_changes_scale",
        "district_map_pinch_zoom_changes_scale",
        "district_map_close_restores_scroll",
    }


def test_live_mobile_smoke_requires_single_open_what_matters_group() -> None:
    metrics = _base_metrics()
    metrics.update({"mobile_what_matters_single_open": False})

    assert _failed_names("/app/search", metrics) == {"mobile_what_matters_single_open_section"}


def test_live_mobile_smoke_requires_single_open_generic_mobile_fold() -> None:
    metrics = _base_metrics()
    metrics.update({"mobile_fold_single_open": False})

    assert _failed_names("/app/alerts", metrics) == {"mobile_fold_single_open"}


def test_live_mobile_smoke_requires_page_scrolling_what_matters_surface() -> None:
    metrics = _base_metrics()
    metrics.update({"mobile_what_matters_page_scroll": False})

    assert _failed_names("/app/search", metrics) == {"mobile_what_matters_page_scroll"}


def test_live_mobile_smoke_requires_single_account_logout() -> None:
    metrics = _base_metrics()
    metrics.update({"logout_button_count": 2})

    assert _failed_names("/app/account", metrics) == {"single_logout_action"}


def test_live_mobile_smoke_requires_compact_account_menu_sheet() -> None:
    metrics = _base_metrics()
    metrics.update({"account_menu_mobile_sheet": False, "account_menu_trigger_compact": False})

    assert _failed_names("/app/account", metrics) == {
        "account_menu_mobile_sheet",
        "account_menu_trigger_compact",
    }


def test_live_mobile_smoke_accepts_dedicated_account_logout_without_dropdown() -> None:
    metrics = _base_metrics()
    metrics.update({"account_menu_present": False, "account_menu_mobile_sheet": False, "account_menu_trigger_compact": False})

    assert _failed_names("/app/account", metrics) == set()


def test_live_mobile_smoke_rejects_small_packet_touch_targets() -> None:
    metrics = _base_metrics()
    metrics.update({"min_action_height": 40})

    assert _failed_names("/app/properties/packets", metrics) == {"primary_touch_targets"}


def test_live_mobile_smoke_accepts_current_research_detail_visual_copy_contract() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts/propertyquarry_live_mobile_surface_smoke.py").read_text(
        encoding="utf-8"
    )

    assert "available in matterport." in source
    assert "3d tour available." in source
    assert "no 3d tour yet." in source
    assert "walkthrough available." in source
    assert "no walkthrough yet." in source
