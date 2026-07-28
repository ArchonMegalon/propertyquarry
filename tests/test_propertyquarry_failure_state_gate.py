from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.errors import install_error_handlers
from scripts import propertyquarry_failure_state_gate as gate
from scripts import propertyquarry_live_public_smoke as public_smoke


ROOT = Path(__file__).resolve().parents[1]


def _configured_routes() -> dict[str, str]:
    return {state: f"/canary/{state}" for state in gate.REQUIRED_FAILURE_STATES}


def _passing_observation(*, state: str, engine: str) -> dict[str, object]:
    status_code = 404 if state == "not_found" else 500 if state == "internal_error" else 200
    return {
        "state": state,
        "observed_state": state,
        "marker_visible": True,
        "copy": gate.CALM_COPY_TOKENS[state][0],
        "action_text": "Try again",
        "action_href": "/app/search",
        "semantic_status": True,
        "transition_proven": True,
        "customer_data_preserved": True,
        "preservation_probe": {
            "before": {"ok": True, "status_code": 200, "sha256": "a" * 64},
            "after": {"ok": True, "status_code": 200, "sha256": "a" * 64},
            "same_digest": True,
        },
        "status_code": status_code,
        "browser_engine": engine,
        "route": f"/canary/{state}",
        "final_url": f"https://propertyquarry.com/canary/{state}",
        "error": "",
    }


def test_failure_state_gate_builds_full_mock_boundary_matrix_without_claiming_live_proof() -> None:
    def fake_collect(**kwargs):
        engine = kwargs["browser_engine"]
        return [
            _passing_observation(state=state, engine=engine)
            for state in gate.REQUIRED_FAILURE_STATES
        ]

    receipt = gate.build_failure_state_receipt(
        base_url="https://propertyquarry.com",
        scenario_routes=_configured_routes(),
        browser_engines=("chromium", "firefox", "webkit"),
        collect_rows=fake_collect,
    )

    assert receipt["status"] == "pass"
    assert receipt["proof_mode"] == "contract_mock"
    assert receipt["expected_sample_count"] == len(gate.REQUIRED_FAILURE_STATES) * 3
    assert receipt["observed_sample_count"] == len(gate.REQUIRED_FAILURE_STATES) * 3
    assert receipt["checks"][2] == {
        "name": "no_provider_response_mocking",
        "ok": False,
        "applicable_to_flagship": True,
    }
    assert all(row["ok"] is True for row in receipt["rows"])


def test_failure_state_gate_fails_closed_before_browser_when_canary_routes_are_missing() -> None:
    called = False

    def fake_collect(**_kwargs):
        nonlocal called
        called = True
        return []

    receipt = gate.build_failure_state_receipt(
        base_url="https://propertyquarry.com",
        collect_rows=fake_collect,
    )

    assert receipt["status"] == "blocked"
    assert called is False
    assert receipt["rows"] == []
    assert set(receipt["checks"][0]["missing_states"]) == {
        "internal_error",
        "delayed",
        "quota_blocked",
        "payment_failed",
        "empty",
        "partial",
        "provider_blocked",
        "missing_media",
    }


def test_failure_state_gate_rejects_absolute_or_secret_bearing_canary_routes_without_leaking_values() -> None:
    routes = _configured_routes()
    routes["internal_error"] = "https://other.example/boom?token=private-value"

    receipt = gate.build_failure_state_receipt(
        base_url="https://propertyquarry.com",
        scenario_routes=routes,
        collect_rows=lambda **_kwargs: [],
    )

    assert receipt["status"] == "blocked"
    assert receipt["checks"][0]["invalid_routes"]["internal_error"] == "relative_path_required"
    assert "private-value" not in str(receipt)


def test_failure_state_gate_rejects_raw_browser_diagnostics() -> None:
    observation = _passing_observation(state="internal_error", engine="chromium")
    observation["copy"] = "Internal server error. Traceback (most recent call last): RuntimeError"

    checks = {row["name"]: row["ok"] for row in gate.evaluate_failure_state_observation(observation)}

    assert checks["raw_diagnostics_hidden"] is False
    assert checks["calm_customer_copy"] is False


def test_failure_state_gate_fails_row_when_customer_snapshot_changes() -> None:
    observation = _passing_observation(state="delayed", engine="webkit")
    observation["customer_data_preserved"] = False
    observation["preservation_probe"] = {
        "before": {"ok": True, "status_code": 200, "sha256": "a" * 64},
        "after": {"ok": True, "status_code": 200, "sha256": "b" * 64},
        "same_digest": False,
    }

    checks = {row["name"]: row["ok"] for row in gate.evaluate_failure_state_observation(observation)}

    assert checks["customer_data_preserved"] is False


def test_preservation_snapshot_emits_only_canonical_digest_and_sizes() -> None:
    secret_value = "private-customer-preference"

    class FakeResponse:
        status = 200

        def body(self) -> bytes:
            return b'{"saved":{"country":"AT"}}'

        def json(self) -> dict[str, object]:
            return {"saved": {"note": secret_value, "country": "AT"}}

    class FakeRequest:
        def get(self, *_args, **_kwargs) -> FakeResponse:
            return FakeResponse()

    class FakeContext:
        request = FakeRequest()

    snapshot = gate._preservation_snapshot(
        FakeContext(),
        base_url="https://propertyquarry.com",
        route=gate.DEFAULT_PRESERVATION_PROBE_ROUTE,
        headers={"X-EA-Principal-ID": "flagship"},
        timeout_ms=1_000,
    )

    assert snapshot["ok"] is True
    assert len(str(snapshot["sha256"])) == 64
    assert snapshot["body_bytes"] > 0
    assert snapshot["canonical_bytes"] > 0
    assert secret_value not in str(snapshot)


def test_failure_state_gate_rejects_secret_bearing_preservation_route() -> None:
    receipt = gate.build_failure_state_receipt(
        base_url="https://propertyquarry.com",
        scenario_routes=_configured_routes(),
        preservation_probe_route="/v1/onboarding/property-search/preferences?token=private-value",
        collect_rows=lambda **_kwargs: [],
    )

    assert receipt["status"] == "blocked"
    assert receipt["checks"][0]["preservation_probe_route_error"] == "sensitive_query_forbidden:token"
    assert "private-value" not in str(receipt)


def _error_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/missing")
    def missing() -> None:
        raise HTTPException(status_code=404, detail="private_missing_detail")

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("private runtime detail")

    @app.get("/permission")
    def permission() -> None:
        raise PermissionError(13, "Permission denied", "/data/private/tour/viewer.html")

    @app.get("/app/search")
    def auth_required() -> None:
        raise HTTPException(status_code=401, detail="auth_required")

    return app


def test_propertyquarry_document_errors_are_calm_html_while_api_clients_keep_json() -> None:
    client = TestClient(_error_app(), base_url="https://propertyquarry.com", raise_server_exceptions=False)

    missing = client.get("/missing", headers={"accept": "text/html"})
    router_missing = client.get("/does-not-exist", headers={"accept": "text/html"})
    boom = client.get("/boom", headers={"accept": "text/html"})
    api_missing = client.get("/missing", headers={"accept": "application/json"})
    permission = client.get("/permission", headers={"accept": "text/html"})
    permission_api = client.get("/permission", headers={"accept": "application/json"})

    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("text/html")
    assert missing.headers["cache-control"] == "no-store"
    assert 'data-pq-failure-state="not_found"' in missing.text
    assert "private_missing_detail" not in missing.text
    assert router_missing.status_code == 404
    assert router_missing.headers["content-type"].startswith("text/html")
    assert 'data-pq-failure-state="not_found"' in router_missing.text
    assert boom.status_code == 500
    assert 'data-pq-failure-state="internal_error"' in boom.text
    assert "private runtime detail" not in boom.text
    assert api_missing.headers["content-type"].startswith("application/json")
    assert api_missing.headers["cache-control"] == "no-store"
    assert api_missing.json()["error"]["code"] == "private_missing_detail"
    assert permission.status_code == 500
    assert permission.headers["content-type"].startswith("text/html")
    assert permission.headers["cache-control"] == "no-store"
    assert 'data-pq-failure-state="internal_error"' in permission.text
    assert "/data/private/tour/viewer.html" not in permission.text
    assert permission_api.status_code == 500
    assert permission_api.headers["cache-control"] == "no-store"
    assert permission_api.json()["error"] == {
        "code": "internal_error",
        "message": "internal server error",
        "details": "permission_error",
        "correlation_id": permission_api.headers["x-correlation-id"],
    }
    assert "/data/private/tour/viewer.html" not in permission_api.text


def test_active_app_auth_redirect_marks_expired_session_with_safe_return_path() -> None:
    client = TestClient(_error_app(), base_url="https://propertyquarry.com", raise_server_exceptions=False)

    response = client.get(
        "/app/search?run_id=run-safe",
        headers={"accept": "text/html", "referer": "https://propertyquarry.com/app/search"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/sign-in?")
    assert "session=expired" in response.headers["location"]
    assert "return_to=%2Fapp%2Fsearch%3Frun_id%3Drun-safe" in response.headers["location"]


def test_public_and_accessibility_defaults_cover_every_sitemap_information_route() -> None:
    from scripts import propertyquarry_accessibility_gate as accessibility
    from scripts import propertyquarry_gold_status as gold_status

    assert set(public_smoke.PUBLIC_SITEMAP_ROUTES).issubset(public_smoke.DEFAULT_ROUTES)
    assert set(public_smoke.PUBLIC_INFORMATION_ROUTES).issubset(accessibility.DEFAULT_ACCESSIBILITY_ROUTES)
    assert tuple(public_smoke.PUBLIC_INFORMATION_ROUTES) == tuple(gold_status.REQUIRED_PUBLIC_INFORMATION_ROUTES)


def test_every_public_information_route_renders_with_its_strict_copy_contract(
    monkeypatch,
) -> None:
    from tests.product_test_helpers import build_property_client

    # This is the configured public-surface contract. Keep it independent of
    # provider-environment mutations performed by earlier test modules; the
    # fail-closed unconfigured state has its own explicit coverage.
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setenv(
        "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI",
        "https://propertyquarry.com/google/callback",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
        "test-propertyquarry-google-state-secret",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
        "test-propertyquarry-identity-session-secret",
    )
    client = build_property_client(principal_id="pq-public-information-contract")
    failures: dict[str, list[str]] = {}
    for route in public_smoke.PUBLIC_INFORMATION_ROUTES:
        response = client.get(route)
        checks = public_smoke._route_checks(
            path=route,
            status_code=response.status_code,
            final_url=str(response.url),
            text=response.text,
        )
        failed = [name for name, ok in checks if not ok]
        if response.status_code >= 500 or failed:
            failures[route] = failed or [f"status_{response.status_code}"]

    sitemap = client.get("/sitemap.xml")
    sitemap_failed = [
        name
        for name, ok in public_smoke._route_checks(
            path="/sitemap.xml",
            status_code=sitemap.status_code,
            final_url=str(sitemap.url),
            text=sitemap.text,
        )
        if not ok
    ]
    if sitemap.status_code != 200 or sitemap_failed:
        failures["/sitemap.xml"] = sitemap_failed or [f"status_{sitemap.status_code}"]

    assert failures == {}


def test_expired_session_sign_in_state_keeps_saved_work_and_next_action_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.product_test_helpers import build_property_client

    monkeypatch.setenv("EMAILIT_API_KEY", "propertyquarry-failure-state-emailit-key")
    client = build_property_client(principal_id="pq-expired-session-contract")
    response = client.get("/sign-in?session=expired&return_to=%2Fapp%2Fsearch")

    assert response.status_code == 200
    assert 'data-pq-failure-state="expired_session"' in response.text
    assert "Your search is still saved." in response.text
    assert "data-pq-next-action" in response.text
    assert "data-focus-sign-in-options" in response.text
    assert "Show sign-in options" in response.text


def test_payment_failure_state_is_rendered_from_durable_billing_truth() -> None:
    from tests.product_test_helpers import build_property_client, start_workspace

    principal_id = "pq-payment-failure-state"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Payment Failure State")
    stored = client.app.state.container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "property_search_enabled": True,
            "country_code": "AT",
            "listing_mode": "rent",
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "payment_failed",
                "last_payment_status": "failed",
                "last_billing_event_type": "subscription.payment_failed",
            },
        },
        trusted_commercial_update=True,
    )
    assert (
        stored["property_search_preferences"]["property_commercial"]["status"]
        == "payment_failed"
    )

    response = client.get("/app/account?billing=1")

    assert response.status_code == 200
    assert 'data-pq-failure-state="payment_failed"' in response.text
    assert "The latest payment did not complete." in response.text
    assert "existing account access and saved work are unchanged" in response.text
    assert "data-pq-next-action" in response.text


def test_workbench_failure_states_expose_semantic_markers_and_calm_network_recovery() -> None:
    template = (ROOT / "ea/app/templates/app/property_decision_workbench.html").read_text(encoding="utf-8")
    script = (ROOT / "ea/app/templates/app/_property_workbench_script.html").read_text(encoding="utf-8")
    selected_review = (ROOT / "ea/app/templates/app/_property_selected_review_panel.html").read_text(encoding="utf-8")
    bundle = "\n".join((template, script, selected_review))

    for state in (
        "offline",
        "delayed",
        "quota_blocked",
        "partial",
        "provider_blocked",
        "empty",
        "stale",
        "missing_packet",
        "missing_media",
    ):
        assert state in bundle
    assert "payment_failed" in bundle
    assert 'data-pq-failure-state="offline" role="status"' in template
    assert 'data-pq-failure-state="stale" role="status"' in template
    assert 'data-pq-failure-state="missing_packet" role="status"' in template
    assert "window.addEventListener('offline', syncNetworkState)" in script
    assert "recoverExpiredSession(response)" in script
    assert "The connection dropped. Reconnect, then try again. Your saved work is unchanged." in script
    assert "Failed to fetch" not in script


def test_failure_state_gate_has_a_narrow_non_live_ci_contract() -> None:
    workflow = (ROOT / ".github/workflows/smoke-runtime.yml").read_text(encoding="utf-8")

    assert "propertyquarry-failure-state-contracts:" in workflow
    assert "tests/test_propertyquarry_failure_state_gate.py" in workflow
    job = workflow.split("propertyquarry-failure-state-contracts:", 1)[1].split("\n  propertyquarry-", 1)[0]
    assert "environment:" not in job
    assert "python scripts/propertyquarry_failure_state_gate.py" not in job
