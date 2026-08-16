from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.principal_identity import principal_assertion_signature

from app.propertyquarry_release_probe import (
    PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER,
)

from scripts.property_live_provider_smoke import build_live_provider_smoke_receipt
from scripts import property_live_provider_smoke as provider_smoke
from app.services.property_market_catalog import (
    CUSTOMER_SEARCH_COUNTRY_ORDER,
    default_platforms_for_country_listing_mode,
    provider_options,
)


def _search_ready_provider_count(country_code: str) -> int:
    return sum(
        1
        for row in provider_options(country_code=country_code)
        if bool(row.get("search_ready")) and not bool(row.get("coming_soon"))
    )


def _all_search_ready_countries() -> tuple[str, ...]:
    return tuple(
        code
        for code in CUSTOMER_SEARCH_COUNTRY_ORDER
        if _search_ready_provider_count(code) > 0
    )


def _sanitized_cross_country_response(payload: dict[str, object], _timeout: float = 0.0) -> dict[str, object]:
    requested = [
        str(value or "").strip()
        for value in list(payload.get("selected_platforms") or [])
        if str(value or "").strip()
    ]
    if len(requested) < 2:
        return {
            "run_id": "run-sanitized",
            "status_url": "/app/api/property/search-runs/run-sanitized",
            "status": "queued",
            "selected_platforms": requested,
            "summary": {
                "provider_country_filter_applied": False,
                "provider_country_filter_removed": [],
            },
        }
    return {
        "run_id": "run-sanitized",
        "status_url": "/app/api/property/search-runs/run-sanitized",
        "status": "queued",
        "selected_platforms": [requested[1]],
        "summary": {
            "provider_country_filter_applied": True,
            "provider_country_filter_removed": [requested[0]],
        },
    }


def test_live_provider_smoke_is_skipped_by_default(monkeypatch) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", raising=False)

    receipt = build_live_provider_smoke_receipt(countries=("AT", "CR"))

    assert receipt["status"] == "skipped"
    assert receipt["enabled"] is False
    assert len(receipt["checks"]) == 2
    assert all(row["provider_count"] > 0 for row in receipt["checks"])
    assert all(row["requires_floorplan_receipt"] is True for row in receipt["checks"])
    assert receipt["targeted_search_matrix_status"] == "skipped"
    assert receipt["targeted_search_matrix_count"] == 2 * (
        _search_ready_provider_count("AT") + _search_ready_provider_count("CR")
    )
    summary = receipt["targeted_search_matrix_summary"]
    assert summary["executed"] is False
    assert summary["skipped_case_count"] == receipt["targeted_search_matrix_count"]
    assert summary["all_search_ready_providers_covered"] is True
    assert summary["all_search_ready_provider_modes_passed"] is True
    assert summary["missing_passed_mode_pairs"] == []
    assert summary["missing_passed_mode_pair_count"] == 0
    assert summary["agent_unlimited_results_ok"] is True
    assert receipt["country_scope"] == "explicit"


def test_live_provider_smoke_can_expand_to_all_search_ready_countries(monkeypatch) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_LIVE_PROVIDER_ALL_SEARCH_READY_COUNTRIES", raising=False)

    receipt = build_live_provider_smoke_receipt(countries=(), all_search_ready_countries=True)

    countries = _all_search_ready_countries()
    provider_total = sum(_search_ready_provider_count(country) for country in countries)
    assert receipt["status"] == "skipped"
    assert receipt["country_scope"] == "all_search_ready"
    assert receipt["targeted_search_matrix_count"] == 2 * provider_total
    assert receipt["targeted_search_matrix_summary"]["country_codes"] == list(countries)
    assert receipt["targeted_search_matrix_summary"]["all_search_ready_providers_covered"] is True
    assert receipt["targeted_search_matrix_summary"]["strict_case_count"] == provider_total
    assert receipt["targeted_search_matrix_summary"]["soft_filter_case_count"] == provider_total
    assert receipt["targeted_search_matrix_summary"]["target_context_country_scope_ok"] is True
    assert all(
        row["country_code"] == "AT" or "Vienna" not in str(row.get("location_query") or "")
        for row in receipt["targeted_search_matrix"]
    )
    assert {row.get("country_code") for row in receipt["targeted_search_matrix"]} == set(countries)
    assert all(row["payload_contract_ok"] is True for row in receipt["targeted_search_matrix"])


def test_live_provider_smoke_explicit_countries_can_override_all_country_env(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_ALL_SEARCH_READY_COUNTRIES", "1")

    receipt = build_live_provider_smoke_receipt(countries=("AT",), all_search_ready_countries=False)

    assert receipt["country_scope"] == "explicit"
    assert receipt["targeted_search_matrix_summary"]["country_codes"] == ["AT"]
    assert receipt["targeted_search_matrix_count"] == 2 * _search_ready_provider_count("AT")


def test_live_provider_smoke_dry_run_proves_at_and_cr_catalogs(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "1")

    receipt = build_live_provider_smoke_receipt(countries=("AT", "CR"))

    assert receipt["status"] == "dry_run"
    rows = {row["country_code"]: row for row in receipt["checks"]}
    assert rows["AT"]["default_provider_count"] > 0
    assert rows["CR"]["default_provider_count"] > 0
    assert rows["AT"]["requires_filter_pushdown_receipt"] is True
    assert rows["CR"]["requires_location_boundary_receipt"] is True
    assert receipt["targeted_search_matrix_status"] == "dry_run"
    matrix = receipt["targeted_search_matrix"]
    assert len(matrix) == 2 * (_search_ready_provider_count("AT") + _search_ready_provider_count("CR"))
    assert {row["mode"] for row in matrix} == {"targeted_no_soft_filters", "targeted_soft_filters"}
    assert all(row["payload_contract_ok"] is True for row in matrix)
    assert all(row["provider_country_code"] == row["country_code"] for row in matrix)
    assert all(row["agent_unlimited_results"] is True for row in matrix)
    assert all(row["target_context_country_scope_ok"] is True for row in matrix)
    assert all(row["country_code"] == "AT" or "Vienna" not in str(row.get("location_query") or "") for row in matrix)
    assert all(row["status"] == "dry_run" for row in matrix)
    assert all(row["soft_filters_present"] is (row["mode"] == "targeted_soft_filters") for row in matrix)
    summary = receipt["targeted_search_matrix_summary"]
    assert summary["case_count"] == len(matrix)
    assert summary["strict_case_count"] == _search_ready_provider_count("AT") + _search_ready_provider_count("CR")
    assert summary["soft_filter_case_count"] == _search_ready_provider_count("AT") + _search_ready_provider_count("CR")
    assert summary["dry_run_case_count"] == len(matrix)
    assert summary["payload_contracts_ok"] is True
    assert summary["provider_country_scope_ok"] is True
    assert summary["target_context_country_scope_ok"] is True
    assert summary["strict_without_soft_filters_ok"] is True
    assert summary["soft_filters_present_ok"] is True


def test_live_provider_smoke_can_limit_targeted_matrix_to_selected_provider_scope(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "1")

    receipt = build_live_provider_smoke_receipt(
        countries=("AT",),
        provider_keys=("willhaben",),
        max_providers=1,
    )

    assert receipt["status"] == "dry_run"
    assert receipt["targeted_search_matrix_count"] == 2
    matrix = receipt["targeted_search_matrix"]
    assert {row["provider"] for row in matrix} == {"willhaben"}
    summary = receipt["targeted_search_matrix_summary"]
    assert summary["provider_scope_filtered"] is True
    assert summary["selected_provider_keys"] == ["willhaben"]
    assert summary["max_providers"] == 1
    assert summary["selected_provider_scope_count_by_country"] == {"AT": 1}
    assert summary["full_search_ready_provider_count_by_country"]["AT"] > 1
    assert summary["all_search_ready_providers_covered"] is False
    assert summary["selected_provider_scope_covered"] is True


def test_live_provider_smoke_live_mode_probes_runtime_catalog(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")

    payloads = {
        "AT": {
            "country_code": "AT",
            "listing_mode": "rent",
            "property_type": "any",
            "default_platforms": list(default_platforms_for_country_listing_mode("AT", "rent")),
            "providers": [{"value": row.get("value"), "country_code": row.get("country_code")} for row in provider_options(country_code="AT")],
        },
        "CR": {
            "country_code": "CR",
            "listing_mode": "rent",
            "property_type": "any",
            "default_platforms": list(default_platforms_for_country_listing_mode("CR", "rent")),
            "providers": [{"value": row.get("value"), "country_code": row.get("country_code")} for row in provider_options(country_code="CR")],
        },
    }

    def _fetcher(country: str, _timeout: float) -> dict[str, object]:
        return payloads[country]

    receipt = build_live_provider_smoke_receipt(
        countries=("AT", "CR"),
        fetcher=_fetcher,
        search_executor=_sanitized_cross_country_response,
    )

    assert receipt["status"] == "blocked_targeted_search_matrix_not_executed"
    rows = {row["country_code"]: row for row in receipt["checks"]}
    assert rows["AT"]["status"] == "pass"
    assert rows["AT"]["runtime_provider_count_ok"] is True
    assert rows["AT"]["runtime_provider_set_ok"] is True
    assert rows["AT"]["runtime_missing_providers"] == []
    assert rows["AT"]["runtime_unexpected_providers"] == []
    assert rows["AT"]["runtime_defaults_present_ok"] is True
    assert rows["AT"]["runtime_provider_country_scope_ok"] is True
    assert rows["AT"]["runtime_country_code"] == "AT"
    assert rows["CR"]["status"] == "pass"
    assert rows["CR"]["runtime_provider_count_ok"] is True
    assert rows["CR"]["runtime_defaults_present_ok"] is True
    assert rows["CR"]["runtime_provider_country_scope_ok"] is True
    assert receipt["targeted_search_matrix_status"] == "planned"
    assert receipt["targeted_search_matrix_executed"] is False
    summary = receipt["targeted_search_matrix_summary"]
    assert summary["execution_requested"] is False
    assert summary["executed"] is False
    assert summary["planned_case_count"] == receipt["targeted_search_matrix_count"]
    assert summary["all_search_ready_providers_covered"] is True
    assert summary["target_context_country_scope_ok"] is True


def test_live_provider_smoke_live_mode_reports_runtime_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")

    receipt = build_live_provider_smoke_receipt(
        countries=("AT",),
        fetcher=lambda _country, _timeout: {
            "country_code": "AT",
            "default_platforms": ["willhaben"],
            "providers": [{"value": "willhaben"}],
        },
    )

    assert receipt["status"] == "fail"
    row = receipt["checks"][0]
    assert row["status"] == "fail"
    assert row["runtime_provider_count_ok"] is False
    assert row["runtime_provider_set_ok"] is False
    assert "willhaben" not in row["runtime_missing_providers"]
    assert row["runtime_unexpected_providers"] == []
    assert row["runtime_defaults_present_ok"] is False


def test_live_provider_smoke_can_probe_catalog_without_dispatching_searches(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")
    catalog_payload = {
        "country_code": "AT",
        "listing_mode": "rent",
        "property_type": "any",
        "default_platforms": list(default_platforms_for_country_listing_mode("AT", "rent")),
        "providers": [
            {"value": row.get("value"), "country_code": row.get("country_code")}
            for row in provider_options(country_code="AT")
        ],
    }

    def _unexpected_dispatch(_payload: dict[str, object], _timeout: float) -> dict[str, object]:
        raise AssertionError("read-only catalog probe dispatched a search run")

    receipt = build_live_provider_smoke_receipt(
        countries=("AT",),
        fetcher=lambda _country, _timeout: catalog_payload,
        execute_search_matrix=False,
        execute_cross_country_sanitization=False,
        search_executor=_unexpected_dispatch,
    )

    assert receipt["status"] == "blocked_targeted_search_matrix_not_executed"
    assert receipt["checks"][0]["status"] == "pass"
    assert receipt["targeted_search_matrix_executed"] is False
    assert receipt["cross_country_sanitization_executed"] is False
    assert receipt["cross_country_sanitization_summary"]["status_counts"] == {
        "skipped_no_cross_country_case": 1
    }


def test_live_provider_smoke_rejects_same_count_provider_substitution(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")
    options = [dict(row) for row in provider_options(country_code="AT")]
    replaced_provider = str(options[0]["value"])
    runtime_providers = [
        {"value": row.get("value"), "country_code": row.get("country_code")}
        for row in options[1:]
    ]
    runtime_providers.append({"value": "unexpected_at", "country_code": "AT"})

    receipt = build_live_provider_smoke_receipt(
        countries=("AT",),
        fetcher=lambda _country, _timeout: {
            "country_code": "AT",
            "listing_mode": "rent",
            "property_type": "any",
            "default_platforms": list(default_platforms_for_country_listing_mode("AT", "rent")),
            "providers": runtime_providers,
        },
        execute_cross_country_sanitization=False,
    )

    row = receipt["checks"][0]
    assert receipt["status"] == "fail"
    assert row["runtime_provider_count_ok"] is True
    assert row["runtime_provider_set_ok"] is False
    assert row["runtime_missing_providers"] == [replaced_provider]
    assert row["runtime_unexpected_providers"] == ["unexpected_at"]


def test_live_provider_smoke_default_api_token_reads_dotenv(monkeypatch, tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("EA_API_TOKEN=dotenv-live-token\n", encoding="utf-8")
    monkeypatch.delenv("EA_API_TOKEN", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_LIVE_API_TOKEN", raising=False)

    assert provider_smoke._default_api_token(dotenv_paths=(dotenv,)) == "dotenv-live-token"


def test_live_provider_smoke_request_headers_include_all_supported_api_token_headers() -> None:
    headers = provider_smoke._request_headers(
        user_agent="test-agent",
        principal_id="cf-email:test@example.com",
        api_token="live-token",
        content_type="application/json",
    )

    assert headers["Authorization"] == "Bearer live-token"
    assert headers["X-EA-API-Token"] == "live-token"
    assert headers["X-API-Token"] == "live-token"
    assert headers["X-EA-Principal-ID"] == "cf-email:test@example.com"
    assert headers["Content-Type"] == "application/json"


def test_live_provider_smoke_request_headers_can_sign_prod_edge_assertion(monkeypatch) -> None:
    secret = "edge-assertion-test-secret-that-is-long-enough-0001"
    audience = "propertyquarry-edge"
    monkeypatch.setattr(provider_smoke.time, "time", lambda: 1_777_777_777)
    monkeypatch.setattr(provider_smoke.secrets, "token_urlsafe", lambda _size: "nonce-for-live-provider-smoke")

    headers = provider_smoke._request_headers(
        user_agent="test-agent",
        principal_id="cf-email:test@example.com",
        api_token="live-token",
        content_type="application/json",
        method="POST",
        url="https://propertyquarry.com/app/api/property/search-runs?dispatch=1",
        edge_assertion_secret=secret,
        edge_assertion_audience=audience,
    )

    assert headers["x-ea-principal-assertion-timestamp"] == "1777777777"
    assert headers["x-ea-principal-assertion-nonce"] == "nonce-for-live-provider-smoke"
    assert headers["x-ea-principal-assertion-audience"] == audience
    assert headers["x-ea-principal-assertion-signature"] == principal_assertion_signature(
        secret=secret,
        method="POST",
        path="/app/api/property/search-runs",
        query_string="dispatch=1",
        principal_id="cf-email:test@example.com",
        timestamp=1_777_777_777,
        nonce="nonce-for-live-provider-smoke",
        audience=audience,
    )
    assert secret not in json.dumps(headers)


def test_live_provider_smoke_rejects_incomplete_or_conflicting_signed_auth() -> None:
    with pytest.raises(ValueError, match="edge_assertion_configuration_incomplete"):
        build_live_provider_smoke_receipt(
            countries=("AT",),
            edge_assertion_secret="edge-secret",
        )
    with pytest.raises(ValueError, match="release_probe_and_edge_assertion_modes_conflict"):
        build_live_provider_smoke_receipt(
            countries=("AT",),
            release_probe_secret="release-secret",
            edge_assertion_secret="edge-secret",
            edge_assertion_audience="propertyquarry-edge",
        )


def test_live_provider_catalog_release_probe_is_signed_and_drops_legacy_auth(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return b'{"country_code":"AT","providers":[]}'

    def _urlopen(request, *, timeout: float):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr(provider_smoke.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(
        provider_smoke,
        "_default_api_token",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("release probe must not read a fallback API token")
        ),
    )

    payload = provider_smoke._fetch_provider_payload(
        base_url="https://propertyquarry.com",
        country_code="AT",
        timeout_seconds=3,
        api_token="legacy-token-must-be-dropped",
        principal_id="caller-controlled-principal",
        release_probe_secret="release-probe-secret-that-is-long-enough-0001",
    )

    request = captured["request"]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert payload["country_code"] == "AT"
    assert captured["timeout"] == 3
    assert request.full_url.endswith("/app/api/property/providers?country=AT")
    assert "authorization" not in headers
    assert "x-api-token" not in headers
    assert "x-ea-api-token" not in headers
    assert "x-ea-principal-id" not in headers
    assert headers[PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER]
    assert headers[PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER]
    assert headers[PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER]


def test_live_provider_release_probe_refuses_mutating_modes(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")

    with pytest.raises(ValueError, match="release_probe_mode_is_read_only"):
        build_live_provider_smoke_receipt(
            countries=("AT",),
            execute_search_matrix=False,
            execute_cross_country_sanitization=True,
            release_probe_secret="release-probe-secret-that-is-long-enough-0001",
        )

    with pytest.raises(ValueError, match="release_probe_mode_is_read_only"):
        build_live_provider_smoke_receipt(
            countries=("AT",),
            execute_search_matrix=True,
            execute_cross_country_sanitization=False,
            release_probe_secret="release-probe-secret-that-is-long-enough-0001",
        )


def test_live_provider_smoke_live_mode_rejects_cross_country_runtime_provider(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")

    at_options = [dict(row) for row in provider_options(country_code="AT")]
    payload = {
        "country_code": "AT",
        "listing_mode": "rent",
        "property_type": "any",
        "default_platforms": list(default_platforms_for_country_listing_mode("AT", "rent")),
        "providers": [
            *[
                {"value": row.get("value"), "country_code": row.get("country_code")}
                for row in at_options
                if row.get("value") != "willhaben"
            ],
            {"value": "willhaben", "country_code": "PL"},
        ],
    }

    receipt = build_live_provider_smoke_receipt(countries=("AT",), fetcher=lambda _country, _timeout: payload)

    assert receipt["status"] == "fail"
    row = receipt["checks"][0]
    assert row["runtime_provider_count_ok"] is True
    assert row["runtime_defaults_present_ok"] is True
    assert row["runtime_provider_country_scope_ok"] is False
    assert row["runtime_provider_country_mismatches"] == ["willhaben"]


def test_live_provider_smoke_can_execute_targeted_search_matrix(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_E2E", "1")

    catalog_payload = {
        "country_code": "AT",
        "listing_mode": "rent",
        "property_type": "apartment",
        "default_platforms": list(default_platforms_for_country_listing_mode("AT", "rent")),
        "providers": [{"value": row.get("value")} for row in provider_options(country_code="AT")],
    }
    observed_payloads: list[dict[str, object]] = []
    observed_status_reads: list[tuple[str, str]] = []
    observed_search_timeouts: list[float] = []
    observed_status_timeouts: list[float] = []
    checkpoint_path = tmp_path / "provider-matrix-checkpoint.json"

    def _search_executor(payload: dict[str, object], timeout: float) -> dict[str, object]:
        observed_search_timeouts.append(timeout)
        selected_platforms = list(payload.get("selected_platforms") or [])
        if len(selected_platforms) > 1:
            return _sanitized_cross_country_response(payload)
        observed_payloads.append(dict(payload))
        provider = str((payload.get("selected_platforms") or ["provider"])[0])
        preferences = dict(payload.get("property_preferences") or {})
        mode = str(preferences.get("search_mode") or "strict")
        return {
            "run_id": f"run-{provider}-{mode}",
            "status_url": f"/app/api/property/search-runs/run-{provider}-{mode}",
            "status": "queued",
        }

    def _status_fetcher(run_id: str, status_url: str, timeout: float) -> dict[str, object]:
        observed_status_timeouts.append(timeout)
        observed_status_reads.append((run_id, status_url))
        return {
            "run_id": run_id,
            "status_url": status_url,
            "status": "queued",
            "candidate_count": 0,
        }

    receipt = build_live_provider_smoke_receipt(
        countries=("AT",),
        fetcher=lambda _country, _timeout: catalog_payload,
        search_executor=_search_executor,
        status_fetcher=_status_fetcher,
        checkpoint_path=checkpoint_path,
    )

    assert receipt["status"] == "ready_for_live_probe"
    assert receipt["targeted_search_matrix_status"] == "pass"
    assert receipt["targeted_search_matrix_executed"] is True
    summary = receipt["targeted_search_matrix_summary"]
    assert summary["execution_requested"] is True
    assert summary["executed"] is True
    assert summary["executed_case_count"] == 2 * _search_ready_provider_count("AT")
    assert summary["passed_case_count"] == 2 * _search_ready_provider_count("AT")
    assert summary["failed_case_count"] == 0
    assert summary["failed_cases"] == []
    assert summary["dispatch_accepted_count"] == 2 * _search_ready_provider_count("AT")
    assert summary["dispatch_acceptance_complete"] is True
    assert summary["status_readback_required"] is True
    assert summary["status_readback_case_count"] == 2 * _search_ready_provider_count("AT")
    assert summary["status_readback_ok_count"] == 2 * _search_ready_provider_count("AT")
    assert summary["status_readback_complete"] is True
    assert summary["all_search_ready_providers_covered"] is True
    assert summary["all_search_ready_provider_modes_passed"] is True
    assert summary["missing_passed_mode_pair_count"] == 0
    assert summary["missing_passed_mode_pairs"] == []
    assert summary["agent_unlimited_results_ok"] is True
    assert summary["provider_country_scope_ok"] is True
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["checkpoint"] is True
    assert checkpoint["complete"] is False
    assert checkpoint["targeted_search_matrix_status"] == "running"
    assert checkpoint["targeted_search_matrix_count"] == 2 * _search_ready_provider_count("AT")
    assert len(observed_payloads) == 2 * _search_ready_provider_count("AT")
    assert len(observed_status_reads) == len(observed_payloads)
    assert all(row["status_probe_ok"] is True for row in receipt["targeted_search_matrix"])
    assert all(row["status_probe_status"] == "queued" for row in receipt["targeted_search_matrix"])
    assert all(payload.get("dispatch_only") is True for payload in observed_payloads)
    assert all("max_results_per_source" not in payload for payload in observed_payloads)
    assert set(observed_search_timeouts) == {60.0}
    assert set(observed_status_timeouts) == {60.0}
    assert {
        dict(payload.get("property_preferences") or {}).get("search_mode")
        for payload in observed_payloads
    } == {"strict", "discovery"}
    assert all(
        dict(payload.get("property_preferences") or {}).get("property_commercial", {}).get("active_plan_key") == "agent"
        for payload in observed_payloads
    )
    sanitization_summary = receipt["cross_country_sanitization_summary"]
    assert sanitization_summary["sanitization_ok"] is True
    assert sanitization_summary["status_counts"] == {
        "skipped_no_cross_country_case": 1
    }
    sanitization_row = receipt["cross_country_sanitization_checks"][0]
    assert sanitization_row["status"] == "skipped_no_cross_country_case"
    assert sanitization_row["foreign_provider"] == ""
    assert sanitization_row["sanitization_ok"] is True


def test_live_provider_smoke_can_use_explicit_search_run_timeout(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_E2E", "1")

    catalog_payload = {
        "country_code": "AT",
        "listing_mode": "rent",
        "property_type": "apartment",
        "default_platforms": list(default_platforms_for_country_listing_mode("AT", "rent")),
        "providers": [{"value": row.get("value")} for row in provider_options(country_code="AT")],
    }
    observed_search_timeouts: list[float] = []
    observed_status_timeouts: list[float] = []

    def _search_executor(payload: dict[str, object], timeout: float) -> dict[str, object]:
        observed_search_timeouts.append(timeout)
        selected_platforms = list(payload.get("selected_platforms") or [])
        if len(selected_platforms) > 1:
            return _sanitized_cross_country_response(payload)
        provider = str(selected_platforms[0])
        mode = str(dict(payload.get("property_preferences") or {}).get("search_mode") or "strict")
        return {
            "run_id": f"run-{provider}-{mode}",
            "status_url": f"/app/api/property/search-runs/run-{provider}-{mode}",
            "status": "queued",
        }

    def _status_fetcher(run_id: str, status_url: str, timeout: float) -> dict[str, object]:
        observed_status_timeouts.append(timeout)
        return {"run_id": run_id, "status_url": status_url, "status": "queued"}

    receipt = build_live_provider_smoke_receipt(
        countries=("AT",),
        timeout_seconds=20,
        search_run_timeout_seconds=60,
        provider_keys=("willhaben",),
        fetcher=lambda _country, _timeout: catalog_payload,
        search_executor=_search_executor,
        status_fetcher=_status_fetcher,
    )

    assert receipt["status"] == "ready_for_live_probe"
    assert receipt["provider_catalog_timeout_seconds"] == 20
    assert receipt["search_run_timeout_seconds"] == 60
    assert set(observed_search_timeouts) == {60}
    assert set(observed_status_timeouts) == {60}


def test_live_provider_smoke_uses_deploy_timeout_floor_by_default(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_DEPLOY_PROVIDER_SEARCH_RUN_TIMEOUT_SECONDS", "75")

    from scripts import property_live_provider_smoke as smoke

    assert smoke._effective_search_run_timeout_seconds(8.0, enabled=True, dry_run=False) == 75.0


def test_live_provider_smoke_executes_filtered_provider_scope(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_E2E", "1")

    catalog_payload = {
        "country_code": "AT",
        "listing_mode": "rent",
        "property_type": "apartment",
        "default_platforms": list(default_platforms_for_country_listing_mode("AT", "rent")),
        "providers": [{"value": row.get("value")} for row in provider_options(country_code="AT")],
    }
    observed_payloads: list[dict[str, object]] = []

    def _search_executor(payload: dict[str, object], _timeout: float) -> dict[str, object]:
        selected_platforms = list(payload.get("selected_platforms") or [])
        if len(selected_platforms) > 1:
            return _sanitized_cross_country_response(payload)
        observed_payloads.append(dict(payload))
        provider = str(selected_platforms[0])
        mode = str(dict(payload.get("property_preferences") or {}).get("search_mode") or "strict")
        return {
            "run_id": f"run-{provider}-{mode}",
            "status_url": f"/app/api/property/search-runs/run-{provider}-{mode}",
            "status": "queued",
        }

    receipt = build_live_provider_smoke_receipt(
        countries=("AT",),
        provider_keys=("willhaben",),
        max_providers=1,
        fetcher=lambda _country, _timeout: catalog_payload,
        search_executor=_search_executor,
        status_fetcher=lambda run_id, status_url, _timeout: {"run_id": run_id, "status_url": status_url, "status": "queued"},
    )

    assert receipt["status"] == "ready_for_live_probe"
    assert len(observed_payloads) == 2
    assert {tuple(payload.get("selected_platforms") or []) for payload in observed_payloads} == {("willhaben",)}
    summary = receipt["targeted_search_matrix_summary"]
    assert summary["executed"] is True
    assert summary["executed_case_count"] == 2
    assert summary["passed_case_count"] == 2
    assert summary["provider_scope_filtered"] is True
    assert summary["all_search_ready_providers_covered"] is False
    assert summary["selected_provider_scope_covered"] is True


def test_live_provider_smoke_fails_when_cross_country_sanitization_does_not_remove_foreign_provider(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")

    def _search_executor(payload: dict[str, object], _timeout: float) -> dict[str, object]:
        requested = [
            str(value or "").strip()
            for value in list(payload.get("selected_platforms") or [])
            if str(value or "").strip()
        ]
        return {
            "run_id": "run-unsanitized",
            "status_url": "/app/api/property/search-runs/run-unsanitized",
            "status": "queued",
            "selected_platforms": requested,
            "summary": {
                "provider_country_filter_applied": False,
                "provider_country_filter_removed": [],
            },
        }

    receipt = build_live_provider_smoke_receipt(
        countries=("AT", "CR"),
        fetcher=lambda country, _timeout: {
            "country_code": country,
            "listing_mode": "rent",
            "property_type": "apartment",
            "default_platforms": list(default_platforms_for_country_listing_mode(country, "rent")),
            "providers": [{"value": row.get("value")} for row in provider_options(country_code=country)],
        },
        search_executor=_search_executor,
    )

    assert receipt["status"] == "fail"
    assert receipt["cross_country_sanitization_summary"]["sanitization_ok"] is False
    row = next(
        item
        for item in receipt["cross_country_sanitization_checks"]
        if item["status"] == "fail"
    )
    assert row["status"] == "fail"
    assert row["foreign_provider"] in row["sanitized_platforms"]
    assert row["summary_filter_applied"] is False


def test_live_provider_smoke_can_resume_passed_targeted_search_cases(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_E2E", "1")

    first_provider = str(provider_options(country_code="AT")[0]["value"])
    resume_path = tmp_path / "provider-matrix-resume.json"
    resume_path.write_text(
        json.dumps(
            {
                "targeted_search_matrix": [
                    {
                        "country_code": "AT",
                        "provider": first_provider,
                        "mode": "targeted_no_soft_filters",
                        "status": "pass",
                        "run_id": "resumed-run",
                        "status_url": "/app/api/property/search-runs/resumed-run",
                        "runtime_status": "queued",
                        "status_probe_ok": True,
                        "status_probe_status": "queued",
                        "status_probe_candidate_count": 0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    catalog_payload = {
        "country_code": "AT",
        "listing_mode": "rent",
        "property_type": "apartment",
        "default_platforms": list(default_platforms_for_country_listing_mode("AT", "rent")),
        "providers": [{"value": row.get("value")} for row in provider_options(country_code="AT")],
    }
    observed_payloads: list[dict[str, object]] = []

    def _search_executor(payload: dict[str, object], _timeout: float) -> dict[str, object]:
        selected_platforms = list(payload.get("selected_platforms") or [])
        if len(selected_platforms) > 1:
            return _sanitized_cross_country_response(payload)
        observed_payloads.append(dict(payload))
        provider = str((payload.get("selected_platforms") or ["provider"])[0])
        preferences = dict(payload.get("property_preferences") or {})
        mode = str(preferences.get("search_mode") or "strict")
        return {
            "run_id": f"run-{provider}-{mode}",
            "status_url": f"/app/api/property/search-runs/run-{provider}-{mode}",
            "status": "queued",
        }

    receipt = build_live_provider_smoke_receipt(
        countries=("AT",),
        fetcher=lambda _country, _timeout: catalog_payload,
        search_executor=_search_executor,
        status_fetcher=lambda run_id, status_url, _timeout: {"run_id": run_id, "status_url": status_url, "status": "queued"},
        resume_checkpoint_path=resume_path,
    )

    expected_case_count = 2 * _search_ready_provider_count("AT")
    summary = receipt["targeted_search_matrix_summary"]
    assert receipt["status"] == "ready_for_live_probe"
    assert receipt["resume_source"] == str(resume_path)
    assert summary["executed_case_count"] == expected_case_count
    assert summary["resumed_case_count"] == 1
    assert summary["passed_case_count"] == expected_case_count
    assert len(observed_payloads) == expected_case_count - 1
    resumed_rows = [row for row in receipt["targeted_search_matrix"] if row.get("resumed_from_checkpoint")]
    assert len(resumed_rows) == 1
    assert resumed_rows[0]["run_id"] == "resumed-run"


def test_live_provider_smoke_execution_fails_when_status_probe_is_unreadable(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE", "1")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SMOKE_DRY_RUN", "0")
    monkeypatch.setenv("PROPERTYQUARRY_LIVE_PROVIDER_SEARCH_E2E", "1")

    catalog_payload = {
        "country_code": "AT",
        "listing_mode": "rent",
        "property_type": "apartment",
        "default_platforms": list(default_platforms_for_country_listing_mode("AT", "rent")),
        "providers": [{"value": row.get("value")} for row in provider_options(country_code="AT")],
    }

    def _search_executor(payload: dict[str, object], _timeout: float) -> dict[str, object]:
        provider = str((payload.get("selected_platforms") or ["provider"])[0])
        return {
            "run_id": f"run-{provider}",
            "status_url": f"/app/api/property/search-runs/run-{provider}",
            "status": "queued",
        }

    receipt = build_live_provider_smoke_receipt(
        countries=("AT",),
        fetcher=lambda _country, _timeout: catalog_payload,
        search_executor=_search_executor,
        status_fetcher=lambda _run_id, _status_url, _timeout: {"run_id": "wrong-run", "status": "missing"},
    )

    assert receipt["status"] == "fail"
    assert receipt["targeted_search_matrix_status"] == "fail"
    summary = receipt["targeted_search_matrix_summary"]
    assert summary["execution_requested"] is True
    assert summary["failed_case_count"] == 2 * _search_ready_provider_count("AT")
    assert summary["dispatch_accepted_count"] == 2 * _search_ready_provider_count("AT")
    assert summary["dispatch_acceptance_complete"] is True
    assert summary["status_readback_required"] is True
    assert summary["status_readback_ok_count"] == 0
    assert summary["status_readback_complete"] is False
    assert summary["all_search_ready_providers_covered"] is True
    assert summary["all_search_ready_provider_modes_passed"] is False
    assert summary["missing_passed_mode_pair_count"] == _search_ready_provider_count("AT")
    assert len(summary["failed_cases"]) == 25
    assert summary["failed_case_sample_count"] == 25
    assert summary["failed_case_sample_limit"] == 25
    assert {
        row["mode"]
        for row in summary["failed_cases"]
    } == {"targeted_no_soft_filters", "targeted_soft_filters"}
    assert all(row["status_probe_ok"] is False for row in receipt["targeted_search_matrix"])
