from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from tests.product_test_helpers import (
    generated_reconstruction_style_fixture as _generated_reconstruction_style_fixture,
)


@pytest.fixture(scope="module", autouse=True)
def _isolate_provider_contract_persistence(
    tmp_path_factory: pytest.TempPathFactory,
):
    isolated_root = tmp_path_factory.mktemp("provider-contract-persistence")
    keys = ("EA_LTD_MARKDOWN_PATH",)
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["EA_LTD_MARKDOWN_PATH"] = str(isolated_root / "LTDs.md")
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module", autouse=True)
def _isolate_provider_contract_persistence(
    tmp_path_factory: pytest.TempPathFactory,
):
    isolated_root = tmp_path_factory.mktemp("provider-contract-persistence")
    keys = ("EA_LTD_MARKDOWN_PATH",)
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["EA_LTD_MARKDOWN_PATH"] = str(isolated_root / "LTDs.md")
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _client(*, principal_id: str, operator: bool = False) -> TestClient:
    os.environ["EA_STORAGE_BACKEND"] = "memory"
    os.environ.pop("EA_LEDGER_BACKEND", None)
    os.environ.pop("EA_DEFAULT_PRINCIPAL_ID", None)
    os.environ["PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES"] = "1"
    if operator:
        os.environ["EA_API_TOKEN"] = "test-token"
        os.environ["EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER"] = "1"
        os.environ["EA_OPERATOR_PRINCIPAL_IDS"] = principal_id
    else:
        os.environ["EA_API_TOKEN"] = ""
        os.environ.pop("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER", None)
        os.environ.pop("EA_OPERATOR_PRINCIPAL_IDS", None)
    from app.api.app import create_app

    client = TestClient(create_app())
    if operator:
        client.headers.update({"Authorization": "Bearer test-token"})
    client.headers.update({"X-EA-Principal-ID": principal_id})
    return client


def _write_3dvista_private_receipt(
    bundle_dir: Path,
    *,
    slug: str,
    browser_rendered: bool,
    provider_url: str = "",
    export_dir: Path | None = None,
    entry_relpath: str = "",
) -> None:
    from scripts.property_tour_3dvista_provenance import export_tree_sha256, sha256_text

    if bool(provider_url) == bool(export_dir):
        raise AssertionError("3DVista fixture requires exactly one hosted URL or local export")
    provenance: dict[str, object] = {
        "schema": "propertyquarry.3dvista_target_provenance.v1",
        "status": "pass",
        "provider": "3dvista",
        "target_slug": slug,
        "authorization": {
            "status": "approved",
            "reference": "test-fixture:licensed-3dvista",
        },
        "review": {
            "property_match": "pass",
            "visual_match": "pass",
            "reviewed_by": "propertyquarry-test-suite",
            "reviewed_at": "2026-07-19T00:00:00+00:00",
        },
    }
    private_receipt: dict[str, object] = {
        "three_d_vista_import": {"source_project": "propertyquarry"},
        "three_d_vista_white_label_proof": {
            "source_project": "propertyquarry",
            "private_viewer_verified": True,
            "non_trial_export_verified": True,
            "propertyquarry_tour_metadata": True,
            "trial_branding_checked": True,
            "trial_branding_present": False,
        },
    }
    if browser_rendered:
        private_receipt["three_d_vista_browser_render_proof"] = {
            "provider": "3dvista",
            "status": "pass",
            "rendered_viewer": True,
        }
    if provider_url:
        private_receipt["three_d_vista_url"] = provider_url
        provenance["artifact"] = {
            "kind": "hosted_url",
            "sha256": sha256_text(provider_url),
        }
    else:
        assert export_dir is not None
        entry_parts = Path(entry_relpath).parts
        if len(entry_parts) < 2:
            raise AssertionError("local 3DVista fixture entry must include its export subdirectory")
        target_subdir = entry_parts[0]
        private_receipt["three_d_vista_entry_relpath"] = entry_relpath
        private_receipt["three_d_vista_import"] = {
            "source_project": "propertyquarry",
            "target_subdir": target_subdir,
        }
        provenance["target_subdir"] = target_subdir
        provenance["artifact"] = {
            "kind": "local_export",
            "sha256": export_tree_sha256(export_dir),
            "entry_relpath": "/".join(entry_parts[1:]),
        }
    private_receipt["three_d_vista_target_provenance"] = provenance
    private_path = bundle_dir / "tour.private.json"
    private_path.write_text(json.dumps(private_receipt, ensure_ascii=False), encoding="utf-8")
    private_path.chmod(0o600)


def _assert_no_product_drift(text: str) -> None:
    lower = text.lower()
    assert "chummer" not in lower
    assert "gm_creator_ops" not in lower
    assert "gm / creator / campaign ops" not in lower
    assert "campaign or community ops" not in lower


def _internal_links(html: str) -> list[str]:
    refs = sorted(set(re.findall(r'href="([^"]+)"', html)))
    return [ref for ref in refs if ref.startswith("/") and not ref.startswith("//")]


def test_public_tour_security_headers_exclude_marketing_analytics_and_broad_sources() -> None:
    from app.api.routes.public_tours import _public_tour_security_headers

    headers = _public_tour_security_headers()
    csp = headers["Content-Security-Policy"]
    report_only_csp = headers["Content-Security-Policy-Report-Only"]

    assert "https://app.rybbit.io" not in csp
    assert "https://js.clickrank.ai" not in csp
    assert "'wasm-unsafe-eval'" not in csp
    assert "media-src 'self' data: blob: https:;" not in csp
    assert "connect-src 'self' data: blob:" not in csp
    assert "script-src-attr 'none'" in csp
    assert "style-src-attr 'none'" in csp
    assert "worker-src 'self' blob:" in csp
    assert "frame-ancestors 'self'" in csp
    assert "frame-ancestors" not in report_only_csp


def test_public_tour_matterport_discover_url_normalizes_to_embeddable_show_url() -> None:
    from app.api.routes.public_tours import _safe_matterport_external_url

    assert (
        _safe_matterport_external_url("https://discover.matterport.com/space/uoRT7VqgY7E")
        == "https://my.matterport.com/show/?m=uoRT7VqgY7E"
    )
    assert (
        _safe_matterport_external_url("https://my.matterport.com/models/PcXQfDidkkp")
        == "https://my.matterport.com/show/?m=PcXQfDidkkp"
    )


def test_clickrank_only_runs_on_public_marketing_and_editorial_routes() -> None:
    from app.services.public_clickrank import clickrank_route_allowed

    assert clickrank_route_allowed("/")
    assert clickrank_route_allowed("/pricing")
    assert clickrank_route_allowed("/guides/vienna-rental-search")
    assert not clickrank_route_allowed("/app/search")
    assert not clickrank_route_allowed("/results/movie-demo")
    assert not clickrank_route_allowed("/tours/movie-demo")


def test_propertyquarry_app_sets_default_browser_security_headers() -> None:
    client = _client(principal_id="exec-browser-security")

    response = client.get("/", headers={"host": "propertyquarry.com", "x-forwarded-proto": "https"})

    assert response.status_code == 200
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'self'" in csp
    assert "https://app.rybbit.io" in csp
    assert "https://js.clickrank.ai" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in response.headers["Permissions-Policy"]
    assert response.headers["Strict-Transport-Security"].startswith("max-age=31536000")


def test_propertyquarry_app_compresses_large_product_surfaces() -> None:
    from starlette.middleware.gzip import GZipMiddleware

    client = _client(principal_id="exec-browser-compression")

    assert any(middleware.cls is GZipMiddleware for middleware in client.app.user_middleware)


def test_responses_dotenv_lookup_caches_missing_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.services import responses_upstream

    missing_path = tmp_path / "missing.env"
    stat_calls = {"count": 0}
    original_stat = responses_upstream.Path.stat
    responses_upstream._DOTENV_CACHE.clear()
    monkeypatch.setattr(responses_upstream, "_DOTENV_STAT_TTL_SECONDS", 60.0)

    def fake_stat(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == missing_path:
            stat_calls["count"] += 1
            raise FileNotFoundError(str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(responses_upstream.Path, "stat", fake_stat)

    assert responses_upstream._dotenv_values_from_path(missing_path) == {}
    assert responses_upstream._dotenv_values_from_path(missing_path) == {}
    assert stat_calls["count"] == 1


def test_public_tour_surface_supports_initial_pane_query() -> None:
    import inspect

    from app.api.routes.public_tours import _tour_html

    source = inspect.getsource(_tour_html)

    assert "const initialPane = initialParams.get('pane');" in source
    assert "initialPane === 'flythrough-pane'" in source
    assert "initialPane === 'floorplan-pane'" in source


def test_onemin_browseract_failure_code_detects_invalid_credentials() -> None:
    from app.api.routes import providers as providers_route

    assert providers_route._onemin_browseract_failure_code(
        "ui_lane_failure:onemin_billing_usage:invalid_credentials"
    ) == "invalid_credentials"
    assert providers_route._onemin_browseract_failure_code(
        "The email or password you entered is incorrect. Please try again."
    ) == "invalid_credentials"


def test_onemin_browseract_failure_code_detects_onemin_auth_cors_block_as_auth_request_failed() -> None:
    from app.api.routes import providers as providers_route

    assert providers_route._onemin_browseract_failure_code(
        "template_worker_failed: Submit Login:auth_request_failed:console:Access to XMLHttpRequest at "
        "'https://api.1min.ai/auth/login' from origin 'https://app.1min.ai' has been blocked by "
        "CORS policy: No 'Access-Control-Allow-Origin' header is present on the requested resource."
    ) == "auth_request_failed"


def test_onemin_browseract_failure_code_detects_onemin_auth_csp_block_as_auth_request_failed() -> None:
    from app.api.routes import providers as providers_route

    assert providers_route._onemin_browseract_failure_code(
        "template_worker_failed: Submit Login:auth_request_failed:console:[Report Only] Refused to connect to "
        "'https://api.1min.ai/auth/login' because it violates the following Content Security Policy directive: "
        "\"connect-src 'none'\"."
    ) == "auth_request_failed"


def test_onemin_direct_api_opener_hashes_proxy_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ONEMIN_DIRECT_API_PROXY_POOL",
        "http://ea-fastestvpn-proxy-01:3128,http://ea-fastestvpn-proxy-02:3128",
    )
    from app.api.routes import providers as providers_route

    opener = providers_route._onemin_direct_api_opener(proxy_subject="ONEMIN_AI_API_KEY_FALLBACK_68")
    proxy_handler = next(
        handler for handler in opener.handlers if isinstance(handler, providers_route.urllib.request.ProxyHandler)
    )

    assert proxy_handler.proxies["http"] == providers_route.upstream._onemin_direct_api_proxy_url_for_subject(  # type: ignore[attr-defined]
        "ONEMIN_AI_API_KEY_FALLBACK_68"
    )


def test_provider_bindings_are_principal_scoped_and_support_probe_updates() -> None:
    owner = _client(principal_id="exec-1")
    created = owner.post(
        "/v1/providers/bindings",
        json={
            "provider_key": "browseract",
            "status": "enabled",
            "priority": 15,
            "scope_json": {"allowed_tools": ["browseract.extract_account_inventory"]},
            "probe_state": "ready",
            "probe_details_json": {"last_check": "unit"},
        },
    )
    assert created.status_code == 200
    created_body = created.json()
    assert created_body["principal_id"] == "exec-1"
    assert created_body["provider_key"] == "browseract"
    assert created_body["probe_state"] == "ready"
    binding_id = created_body["binding_id"]

    listed = owner.get("/v1/providers/bindings")
    assert listed.status_code == 200
    rows = listed.json()
    assert len(rows) >= 1
    assert any(row["binding_id"] == binding_id for row in rows)

    updated_probe = owner.post(
        f"/v1/providers/bindings/{binding_id}/probe",
        json={"probe_state": "degraded", "probe_details_json": {"reason": "quota_depleted"}},
    )
    assert updated_probe.status_code == 200
    assert updated_probe.json()["probe_state"] == "degraded"
    assert updated_probe.json()["probe_details_json"]["reason"] == "quota_depleted"

    state = owner.get("/v1/providers/states/browseract")
    assert state.status_code == 200
    state_body = state.json()
    assert state_body["provider_key"] == "browseract"
    assert state_body["binding_id"] == binding_id
    assert state_body["health_state"] == "degraded"

    denied = owner.get(
        f"/v1/providers/bindings/{binding_id}",
        headers={"X-EA-Principal-ID": "exec-2"},
    )
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "provider_binding_not_found"


def test_google_oauth_routes_create_and_disconnect_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/v1/providers/google/oauth/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")

    owner = _client(principal_id="exec-google")

    started = owner.post("/v1/providers/google/oauth/start", json={"scope_bundle": "send"})
    assert started.status_code == 200
    started_body = started.json()
    assert started_body["provider_key"] == "google_gmail"
    assert "https://accounts.google.com/o/oauth2/v2/auth" in started_body["auth_url"]
    assert "https://www.googleapis.com/auth/gmail.send" in started_body["requested_scopes"]

    from app.services import google_oauth as google_service

    monkeypatch.setattr(
        google_service,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_service,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-123",
            "email": "person@example.test",
            "hd": "gmail.example",
        },
    )

    callback = owner.get(
        "/v1/providers/google/oauth/callback",
        params={"code": "code-123", "state": started_body["state"]},
    )
    assert callback.status_code == 200
    callback_body = callback.json()
    assert callback_body["principal_id"] == "exec-google"
    assert callback_body["google_email"] == "person@example.test"
    assert callback_body["consent_stage"] == "send"
    assert callback_body["token_status"] == "active"
    assert callback_body["connector_binding_id"]

    accounts = owner.get("/v1/providers/google/accounts")
    assert accounts.status_code == 200
    rows = accounts.json()
    assert len(rows) == 1
    assert rows[0]["is_primary"] is True
    assert rows[0]["google_subject"] == "google-sub-123"
    assert rows[0]["granted_scopes"] == ["email", "https://www.googleapis.com/auth/gmail.send", "openid", "profile"]

    monkeypatch.setattr(
        google_service,
        "_refresh_google_access_token",
        lambda **kwargs: {
            "access_token": "fresh-access-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_service,
        "_gmail_send_message",
        lambda **kwargs: "gmail-message-123",
    )

    smoke = owner.post("/v1/providers/google/gmail/smoke-test", json={})
    assert smoke.status_code == 200
    smoke_body = smoke.json()
    assert smoke_body["sender_email"] == "person@example.test"
    assert smoke_body["recipient_email"] == "person@example.test"
    assert smoke_body["gmail_message_id"] == "gmail-message-123"
    assert smoke_body["rfc822_message_id"].startswith("<ea-smoke-")

    disconnected = owner.post("/v1/providers/google/oauth/disconnect", json={})
    assert disconnected.status_code == 200
    disconnected_body = disconnected.json()
    assert disconnected_body["token_status"] == "revoked"
    assert disconnected_body["reauth_required_reason"] == "disconnected_by_operator"


def test_google_oauth_routes_support_second_google_account_on_same_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/v1/providers/google/oauth/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")

    owner = _client(principal_id="exec-google-multi")

    from app.services import google_oauth as google_service

    monkeypatch.setattr(
        google_service,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
            "expires_in": 3600,
        },
    )

    started_primary = owner.post("/v1/providers/google/oauth/start", json={"scope_bundle": "send"})
    assert started_primary.status_code == 200
    monkeypatch.setattr(
        google_service,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-1",
            "email": "tibor@girschele.com",
            "hd": "girschele.com",
        },
    )
    primary_callback = owner.get(
        "/v1/providers/google/oauth/callback",
        params={"code": "code-primary", "state": started_primary.json()["state"]},
    )
    assert primary_callback.status_code == 200
    primary_body = primary_callback.json()
    assert primary_body["binding_id"] == "exec-google-multi:google_gmail"

    started_secondary = owner.post("/v1/providers/google/oauth/start", json={"scope_bundle": "core"})
    assert started_secondary.status_code == 200
    monkeypatch.setattr(
        google_service,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-2",
            "email": "office@girschele.com",
            "hd": "girschele.com",
        },
    )
    secondary_callback = owner.get(
        "/v1/providers/google/oauth/callback",
        params={"code": "code-secondary", "state": started_secondary.json()["state"]},
    )
    assert secondary_callback.status_code == 200
    secondary_body = secondary_callback.json()
    assert secondary_body["binding_id"] == "exec-google-multi:google_gmail:acct:google-sub-2"
    assert secondary_body["google_email"] == "office@girschele.com"

    accounts = owner.get("/v1/providers/google/accounts")
    assert accounts.status_code == 200
    rows = accounts.json()
    assert [row["google_email"] for row in rows] == ["tibor@girschele.com", "office@girschele.com"]

    monkeypatch.setattr(
        google_service,
        "_refresh_google_access_token",
        lambda **kwargs: {
            "access_token": f"fresh-{kwargs['refresh_token']}",
            "expires_in": 3600,
        },
    )

    captured: dict[str, str] = {}

    def _fake_send(**kwargs):
        captured["access_token"] = kwargs["access_token"]
        return "gmail-message-200"

    monkeypatch.setattr(google_service, "_gmail_send_message", _fake_send)

    smoke = owner.post(
        "/v1/providers/google/gmail/smoke-test",
        json={"binding_id": "exec-google-multi:google_gmail:acct:google-sub-2"},
    )
    assert smoke.status_code == 200
    smoke_body = smoke.json()
    assert smoke_body["sender_email"] == "office@girschele.com"
    assert captured["access_token"] == "fresh-refresh-token"

    promoted = owner.post(
        "/v1/providers/google/accounts/exec-google-multi:google_gmail:acct:google-sub-2/make-primary",
        params={"principal_id": "exec-google-multi"},
    )
    assert promoted.status_code == 200
    promoted_body = promoted.json()
    assert promoted_body["binding_id"] == "exec-google-multi:google_gmail"
    assert promoted_body["google_email"] == "office@girschele.com"
    assert promoted_body["is_primary"] is True

    accounts_after = owner.get("/v1/providers/google/accounts")
    assert accounts_after.status_code == 200
    rows_after = accounts_after.json()
    assert [row["google_email"] for row in rows_after] == ["office@girschele.com", "tibor@girschele.com"]
    assert rows_after[0]["is_primary"] is True
    assert rows_after[1]["is_primary"] is False


def test_onboarding_google_start_reports_missing_oauth_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EA_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("EA_GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("EA_GOOGLE_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.delenv("EA_PROVIDER_SECRET_KEY", raising=False)
    owner = _client(principal_id="exec-onboarding-missing")

    owner.post(
        "/v1/onboarding/start",
        json={
            "workspace_name": "No Config",
            "workspace_mode": "personal",
            "region": "AT",
            "language": "en",
            "timezone": "Europe/Vienna",
            "selected_channels": ["google"],
        },
    )

    google = owner.post("/v1/onboarding/google/start", json={"scope_bundle": "identity"})
    assert google.status_code == 200
    body = google.json()
    google_start = dict(body["google_start"])
    assert google_start["ready"] is False
    assert google_start["error"] == "google_oauth_client_id_missing"
    assert "Set EA_GOOGLE_OAUTH_CLIENT_ID and EA_GOOGLE_OAUTH_CLIENT_SECRET." in google_start["detail"]
    assert body["channels"]["google"]["status"] == "credentials_missing"


def test_onboarding_flagship_start_bootstraps_google_telegram_whatsapp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/v1/providers/google/oauth/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")

    owner = _client(principal_id="exec-flagship")

    payload = owner.post(
        "/v1/onboarding/flagship/start",
        json={
            "workspace_name": "Flagship Suite",
            "workspace_mode": "executive_ops",
            "telegram_ref": "@ops-suite",
            "whatsapp_export_label": "Ops Suite Export",
            "selected_channels": ["google", "telegram", "whatsapp"],
            "scope_bundle": "identity",
        },
    )
    assert payload.status_code == 200
    body = payload.json()
    assert body["workspace"]["name"] == "Flagship Suite"
    assert body["workspace"]["mode"] == "executive_ops"
    assert body["selected_channels"] == ["google", "telegram", "whatsapp"]
    assert body["channels"]["google"]["status"] == "ready_to_connect"
    assert body["channels"]["telegram"]["status"] == "guided_manual"
    assert body["channels"]["whatsapp"]["status"] == "export_planned"
    assert body["flagship_start"]["profile"] == "executive_flagship"
    assert body["flagship_start"]["google_bundle"] == "identity"
    assert body["flagship_start"]["telegram_started"] is True
    assert body["flagship_start"]["whatsapp_export_started"] is True
    assert body["google_start"]["ready"] is True
    assert body["google_start"]["oauth_bundle"] == "identity"
    assert body["google_start"]["requested_scopes"] == ["openid", "email", "profile"]
    assert body["telegram_start"]["status"] == "guided_manual"
    assert body["whatsapp_export"]["status"] == "export_planned"


def test_onboarding_flagship_start_continues_without_google_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EA_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("EA_GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("EA_GOOGLE_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.delenv("EA_PROVIDER_SECRET_KEY", raising=False)

    owner = _client(principal_id="exec-flagship-missing-google")

    payload = owner.post(
        "/v1/onboarding/flagship/start",
        json={
            "workspace_name": "Flagship Recovery",
            "scope_bundle": "identity",
            "selected_channels": ["telegram", "whatsapp"],
        },
    )
    assert payload.status_code == 200
    body = payload.json()
    assert body["flagship_start"]["profile"] == "executive_flagship"
    assert body["flagship_start"]["stage"] == "partial"
    assert body["flagship_start"]["telegram_started"] is True
    assert body["flagship_start"]["whatsapp_export_started"] is True
    assert "google" not in body["selected_channels"]
    assert body["channels"]["telegram"]["status"] == "guided_manual"
    assert body["channels"]["whatsapp"]["status"] == "export_planned"


def test_onboarding_flagship_start_marks_partial_if_google_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EA_GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("EA_GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    monkeypatch.delenv("EA_GOOGLE_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.delenv("EA_PROVIDER_SECRET_KEY", raising=False)

    owner = _client(principal_id="exec-flagship-google-missing")

    payload = owner.post(
        "/v1/onboarding/flagship/start",
        json={
            "workspace_name": "Flagship Recovery",
            "scope_bundle": "identity",
            "selected_channels": ["google", "telegram"],
        },
    )
    assert payload.status_code == 200
    body = payload.json()
    assert body["flagship_start"]["profile"] == "executive_flagship"
    assert body["flagship_start"]["google_bundle"] == "identity"
    assert body["flagship_start"]["stage"] == "partial"
    assert body["channels"]["google"]["status"] == "credentials_missing"
    assert body["google_start"]["ready"] is False
    assert body["google_start"]["error"] in {"google_oauth_client_id_missing", "google_oauth_client_secret_missing"}
    assert body["channels"]["telegram"]["status"] == "guided_manual"


def test_onboarding_routes_persist_workspace_and_honest_channel_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/v1/providers/google/oauth/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")

    owner = _client(principal_id="exec-onboarding")

    started = owner.post(
        "/v1/onboarding/start",
        json={
            "workspace_name": "Ops Desk",
            "workspace_mode": "team",
            "region": "AT",
            "language": "en",
            "timezone": "Europe/Vienna",
            "selected_channels": ["google", "telegram", "whatsapp"],
        },
    )
    assert started.status_code == 200
    started_body = started.json()
    assert started_body["status"] == "started"
    assert started_body["workspace"]["name"] == "Ops Desk"
    assert started_body["selected_channels"] == ["google", "telegram", "whatsapp"]

    google = owner.post("/v1/onboarding/google/start", json={"scope_bundle": "identity"})
    assert google.status_code == 200
    google_body = google.json()
    assert google_body["google_start"]["ready"] is True
    assert google_body["google_start"]["requested_bundle"] == "identity"
    assert google_body["google_start"]["oauth_bundle"] == "identity"
    assert google_body["google_start"]["requested_scopes"] == ["openid", "email", "profile"]
    assert google_body["google_start"]["bundle_label"] == "Google sign-in"
    assert google_body["channels"]["google"]["status"] == "ready_to_connect"
    google_query = urllib.parse.parse_qs(urllib.parse.urlparse(google_body["google_start"]["auth_url"]).query)
    assert google_query["redirect_uri"][0] == "https://ea.example/v1/providers/google/oauth/callback"

    telegram = owner.post(
        "/v1/onboarding/telegram/start",
        json={
            "telegram_ref": "@opsdesk",
            "history_mode": "future_only",
            "assistant_surfaces": ["dm", "group"],
        },
    )
    assert telegram.status_code == 200
    telegram_body = telegram.json()
    assert telegram_body["telegram_start"]["status"] == "guided_manual"
    assert telegram_body["channels"]["telegram"]["status"] == "guided_manual"

    whatsapp = owner.post(
        "/v1/onboarding/whatsapp/import-export",
        json={
            "export_label": "March export",
            "selected_chat_labels": ["Family", "Ops"],
            "include_media": True,
        },
    )
    assert whatsapp.status_code == 200
    whatsapp_body = whatsapp.json()
    assert whatsapp_body["whatsapp_export"]["status"] == "export_planned"
    assert whatsapp_body["channels"]["whatsapp"]["status"] == "export_planned"

    finalized = owner.post(
        "/v1/onboarding/finalize",
        json={
            "retention_mode": "metadata_first",
            "metadata_only_channels": ["telegram"],
            "allow_drafts": True,
            "allow_action_suggestions": True,
            "allow_auto_briefs": True,
            "auto_brief_cadence": "weekdays_morning",
            "auto_brief_delivery_time_local": "07:30",
            "auto_brief_quiet_hours_start": "21:00",
            "auto_brief_quiet_hours_end": "06:30",
            "auto_brief_recipient_email": "briefs@example.com",
        },
    )
    assert finalized.status_code == 200
    finalized_body = finalized.json()
    assert finalized_body["status"] == "ready_for_brief"
    assert finalized_body["privacy"]["retention_mode"] == "metadata_first"
    assert finalized_body["privacy"]["metadata_only_channels"] == ["telegram"]
    assert finalized_body["brief_preview"]["headline"].startswith("Ops Desk")
    assert finalized_body["brief_preview"]["top_themes"]
    assert finalized_body["brief_preview"]["first_brief_preview"]
    assert finalized_body["delivery_preferences"]["morning_memo"]["cadence"] == "weekdays_morning"
    assert finalized_body["delivery_preferences"]["morning_memo"]["delivery_time_local"] == "07:30"
    assert finalized_body["delivery_preferences"]["morning_memo"]["quiet_hours_start"] == "21:00"
    assert finalized_body["delivery_preferences"]["morning_memo"]["quiet_hours_end"] == "06:30"
    assert finalized_body["delivery_preferences"]["morning_memo"]["recipient_email"] == "briefs@example.com"
    assert finalized_body["delivery_preferences"]["morning_memo"]["resolved_recipient_email"] == "briefs@example.com"
    stored_preferences = owner.app.state.container.memory_runtime.list_delivery_preferences(  # type: ignore[attr-defined]
        principal_id="exec-onboarding",
        limit=10,
    )
    assert len(stored_preferences) == 1
    assert stored_preferences[0].status == "active"
    assert stored_preferences[0].format_json["schedule_kind"] == "morning_memo"
    assert stored_preferences[0].quiet_hours_json["delivery_time_local"] == "07:30"

    status = owner.get("/v1/onboarding/status")
    assert status.status_code == 200
    status_body = status.json()
    assert status_body["workspace"]["mode"] == "team"
    assert status_body["channels"]["google"]["status"] == "ready_to_connect"
    assert status_body["channels"]["telegram"]["status"] == "guided_manual"
    assert status_body["channels"]["telegram"]["bot_path"] == "PropertyQuarry bot"
    assert "Official assistant bot" not in json.dumps(status_body)
    assert "generic history" not in json.dumps(status_body)
    assert "EA host" not in json.dumps(status_body)
    assert status_body["channels"]["whatsapp"]["status"] == "export_planned"
    assert status_body["next_step"] == "Complete Google sign-in to finish Google account linking."
    assert status_body["storage_posture"]["source_of_truth"] == "PropertyQuarry Postgres"
    assert status_body["delivery_preferences"]["morning_memo"]["recipient_email"] == "briefs@example.com"
    assert status_body["brief_preview"]["first_brief"] == status_body["brief_preview"]["first_brief_preview"]


def test_onboarding_telegram_bind_chat_promotes_live_bot_binding() -> None:
    owner = _client(principal_id="exec-onboarding-telegram-bind")

    bound = owner.post(
        "/v1/onboarding/telegram/bind-chat",
        json={
            "chat_ref": "1354554303",
            "bot_handle": "tibor_concierge_bot",
            "bot_key": "default",
        },
    )
    assert bound.status_code == 200
    body = bound.json()
    assert body["telegram_bot"]["status"] == "enabled"
    assert body["telegram_bot"]["default_chat_ref"] == "1354554303"
    assert body["channels"]["telegram"]["status"] == "enabled"

    bindings = owner.app.state.container.tool_runtime.list_connector_bindings("exec-onboarding-telegram-bind", limit=20)
    by_connector = {item.connector_name: item for item in bindings}
    assert str(by_connector["telegram_identity"].external_account_ref) == "1354554303"
    assert dict(by_connector["telegram_identity"].auth_metadata_json or {})["default_chat_ref"] == "1354554303"
    assert str(by_connector["telegram_official_bot"].external_account_ref) == "tibor_concierge_bot"
    assert dict(by_connector["telegram_official_bot"].auth_metadata_json or {})["default_chat_ref"] == "1354554303"


def test_onboarding_status_reflects_fallback_telegram_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_DEFAULT_PRINCIPAL_ID", "local-user")
    owner = _client(principal_id="cf-email:person@example.test")
    owner.app.state.container.tool_runtime.upsert_connector_binding(
        principal_id="local-user",
        connector_name="telegram_identity",
        external_account_ref="1354554303",
        auth_metadata_json={"default_chat_ref": "1354554303", "bot_key": "default", "bot_handle": "tibor_concierge_bot"},
        scope_json={"assistant_surfaces": ["dm"]},
        status="enabled",
    )

    status = owner.get("/v1/onboarding/status")
    assert status.status_code == 200
    body = status.json()
    assert body["channels"]["telegram"]["status"] == "enabled"


def test_onboarding_google_callback_returns_api_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/v1/onboarding/google/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")

    owner = _client(principal_id="exec-onboarding-callback")

    started = owner.post(
        "/v1/onboarding/google/start",
        json={"scope_bundle": "identity"},
    )
    assert started.status_code == 200
    started_body = started.json()
    assert started_body["google_start"]["ready"] is True
    state = urllib.parse.parse_qs(urllib.parse.urlparse(started_body["google_start"]["auth_url"]).query)["state"][0]

    from app.services import google_oauth as google_service

    monkeypatch.setattr(
        google_service,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "scope": "openid email profile",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_service,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-onboarding",
            "email": "person@example.test",
            "hd": "gmail.example",
        },
    )

    callback = owner.get(
        "/v1/onboarding/google/callback",
        params={"code": "code-123", "state": state},
    )
    assert callback.status_code == 200
    callback_body = callback.json()
    assert callback_body["provider_key"] == "google_gmail"
    assert callback_body["principal_id"] == "exec-onboarding-callback"
    assert callback_body["google_email"] == "person@example.test"
    assert callback_body["connector_binding_id"]
    assert callback_body["granted_scopes"] == ["email", "openid", "profile"]
    assert callback_body["consent_stage"] == "identity"


def test_telegram_ingest_rejects_missing_secret_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    client = _client(principal_id="exec-telegram-ingest", operator=True)
    created = client.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "telegram_identity",
            "external_account_ref": "42",
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    resp = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "update": {
                "message": {
                    "chat": {"id": 42},
                    "text": "hello",
                    "message_id": 7,
                    "date": 123,
                }
            }
        },
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "telegram_secret_invalid"


def test_telegram_ingest_accepts_telegram_secret_header_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    client = _client(principal_id="exec-telegram-ingest-ok", operator=True)
    created = client.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "telegram_identity",
            "external_account_ref": "42",
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update": {
                "message": {
                    "chat": {"id": 42},
                    "text": "hello",
                    "message_id": 7,
                    "date": 123,
                }
            }
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "telegram"
    assert body["event_type"] == "telegram.message"


def test_telegram_ingest_secret_header_bypasses_global_api_token_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.setenv("EA_API_TOKEN", "test-token")
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-prod-webhook")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    from app.api.app import create_app

    client = TestClient(create_app())
    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update": {
                "message": {
                    "chat": {"id": 42},
                    "text": "hello",
                    "message_id": 7,
                    "date": 123,
                }
            }
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["principal_id"] == "exec-telegram-prod-webhook"
    assert body["channel"] == "telegram"
    assert body["event_type"] == "telegram.message"


def test_telegram_ingest_auto_binds_unknown_chat_without_operator_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-autobind")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    client = _client(principal_id="", operator=False)

    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update": {
                "message": {
                    "chat": {"id": 99},
                    "text": "/start",
                    "message_id": 11,
                    "date": 123,
                }
            }
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["principal_id"] == "exec-telegram-autobind"
    app = client.app
    bindings = app.state.container.tool_runtime.list_connector_bindings("exec-telegram-autobind", limit=20)
    assert any(
        binding.connector_name == "telegram_identity"
        and binding.external_account_ref == "99"
        and dict(binding.auth_metadata_json or {}).get("auto_bound") is True
        for binding in bindings
    )


def test_telegram_ingest_sends_start_reply_for_keyed_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "EA_TELEGRAM_BOT_REGISTRY_JSON",
        json.dumps(
            {
                "girschele": {
                    "token": "telegram-token-2",
                    "handle": "Girschele_Bot",
                    "secret": "tg-secret-2",
                    "default_principal_id": "exec-telegram-girschele",
                    "auto_bind_unknown_chat": True,
                }
            }
        ),
    )
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 1}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append({"url": request.full_url, "payload": payload, "timeout": timeout})
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)

    resp = client.post(
        "/v1/channels/telegram/ingest/girschele",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret-2"},
        json={
            "update": {
                "message": {
                    "chat": {"id": 1234},
                    "text": "/start",
                    "message_id": 11,
                    "date": 123,
                }
            }
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["principal_id"] == "exec-telegram-girschele"
    assert body["reply_sent"] is True
    assert "connected to PropertyQuarry" in body["reply_text"]
    assert "Executive Assistant" not in body["reply_text"]
    assert "Tibor" not in body["reply_text"]
    assert "Telegram is selected in Account" in body["reply_text"]
    assert sent and sent[0]["url"] == "https://api.telegram.org/bottelegram-token-2/sendMessage"
    assert sent[0]["payload"]["chat_id"] == "1234"
    assert "Girschele_Bot" in sent[0]["payload"]["text"]

    status = client.post(
        "/v1/channels/telegram/ingest/girschele",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret-2"},
        json={
            "update": {
                "message": {
                    "chat": {"id": 1234},
                    "text": "/status",
                    "message_id": 12,
                    "date": 124,
                }
            }
        },
    )
    assert status.status_code == 200
    status_body = status.json()
    assert status_body["reply_sent"] is True
    assert "PropertyQuarry is online." in status_body["reply_text"]
    assert "Telegram messages are connected." in status_body["reply_text"]
    assert "Telegram ingest" not in status_body["reply_text"]
    assert "Tibor" not in status_body["reply_text"]


def test_telegram_ingest_sends_math_reply_for_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-math")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-math")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 2}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append({"url": request.full_url, "payload": payload, "timeout": timeout})
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)

    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update": {
                "message": {
                    "chat": {"id": 5678},
                    "text": "2+2=?",
                    "message_id": 12,
                    "date": 123,
                }
            }
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_sent"] is True
    assert body["reply_text"] == "2+2 = 4"
    assert sent and sent[0]["payload"]["text"] == "2+2 = 4"


def test_telegram_ingest_sends_plain_language_math_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-math-words")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-math-words")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 1}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append({"url": request.full_url, "payload": payload, "timeout": timeout})
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)

    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update": {
                "message": {
                    "chat": {"id": 4321},
                    "text": "2 plus 2?",
                    "message_id": 12,
                    "date": 123,
                }
            }
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_sent"] is True
    assert body["reply_text"] == "2 + 2 = 4"
    assert sent and sent[0]["payload"]["text"] == "2 + 2 = 4"


def test_telegram_ingest_accepts_raw_telegram_webhook_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-raw")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-raw")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 3}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)

    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 2,
            "message": {
                "chat": {"id": 9090},
                "text": "really?",
                "message_id": 13,
                "date": 123,
            },
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["principal_id"] == "exec-telegram-raw"
    assert body["reply_sent"] is True
    assert "captured your message" in body["reply_text"]


def test_telegram_ingest_really_followup_uses_recent_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-really")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-really")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 4}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)

    first = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 10,
            "message": {
                "chat": {"id": 9191},
                "text": "2+2=?",
                "message_id": 14,
                "date": 123,
            },
        },
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 11,
            "message": {
                "chat": {"id": 9191},
                "text": "really?",
                "message_id": 15,
                "date": 124,
            },
        },
    )
    assert second.status_code == 200
    assert second.json()["reply_text"] == "Yes. 2+2 = 4"


def test_telegram_ingest_answers_short_time_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-time")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-time")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 8}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)
    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 60,
            "message": {
                "chat": {"id": 9494},
                "text": "time?",
                "message_id": 18,
                "date": 123,
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["reply_sent"] is True
    assert resp.json()["reply_text"].startswith("It is ")
    assert resp.json()["reply_text"].endswith(" in Vienna.")


def test_telegram_ingest_answers_weather_tomorrow_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-weather")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-weather")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        url = getattr(request, "full_url", "")
        if "open-meteo.com" in url:
            return _FakeResponse(
                {
                    "daily": {
                        "time": ["2026-05-26", "2026-05-27"],
                        "weather_code": [2, 61],
                        "temperature_2m_max": [22, 19],
                        "temperature_2m_min": [12, 11],
                        "precipitation_probability_max": [10, 70],
                    }
                }
            )
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse({"ok": True, "result": {"message_id": 81}})

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)
    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 61,
            "message": {
                "chat": {"id": 9495},
                "text": "What's the weather tomorrow?",
                "message_id": 20,
                "date": 123,
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["reply_sent"] is True
    assert "Tomorrow in Vienna looks" in resp.json()["reply_text"]
    assert "19" in resp.json()["reply_text"]
    assert sent and "Tomorrow in Vienna looks" in sent[0]["text"]


def test_telegram_ingest_repeats_previous_useful_reply_for_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-again")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-again")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __init__(self, payload: dict[str, object]):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        url = getattr(request, "full_url", "")
        if "open-meteo.com" in url:
            return _FakeResponse(
                {
                    "daily": {
                        "time": ["2026-05-26", "2026-05-27"],
                        "weather_code": [2, 61],
                        "temperature_2m_max": [22, 19],
                        "temperature_2m_min": [12, 11],
                        "precipitation_probability_max": [10, 70],
                    }
                }
            )
        return _FakeResponse({"ok": True, "result": {"message_id": 82}})

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)
    first = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 62,
            "message": {
                "chat": {"id": 9496},
                "text": "What's the weather tomorrow?",
                "message_id": 21,
                "date": 123,
            },
        },
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 63,
            "message": {
                "chat": {"id": 9496},
                "text": "Again",
                "message_id": 22,
                "date": 124,
            },
        },
    )
    assert second.status_code == 200
    assert second.json()["reply_text"] == first.json()["reply_text"]


def test_telegram_ingest_prefers_real_ea_for_ambiguous_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-real-followup")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-real-followup")
    from app.api.routes import channels as channels_route

    seen: list[str] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 83}}).encode("utf-8")

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", lambda request, timeout=30: _FakeResponse())
    monkeypatch.setattr(
        channels_route,
        "_telegram_real_ea_reply_text",
        lambda **kwargs: seen.append(str(kwargs.get("text") or "")) or "I would score tomorrow around 7/10.",
    )
    monkeypatch.setattr(
        channels_route,
        "_telegram_local_assistant_reply_text",
        lambda *args, **kwargs: "LOCAL_FALLBACK_SHOULD_NOT_WIN",
    )

    client = _client(principal_id="", operator=False)
    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 64,
            "message": {
                "chat": {"id": 9497},
                "text": "well? score?",
                "message_id": 23,
                "date": 125,
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["reply_sent"] is True
    assert resp.json()["reply_text"] == "I would score tomorrow around 7/10."
    assert seen == ["well? score?"]


def test_telegram_ingest_answers_capability_question_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-real-ea")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-real-ea")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 9}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        if not request.full_url.startswith("https://api.telegram.org/"):
            raise channels_route.urllib.error.URLError(
                "non-Telegram transport disabled by this test"
            )
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)
    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 70,
            "message": {
                "chat": {"id": 9595},
                "text": "Can u answer everything now?",
                "message_id": 19,
                "date": 123,
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["reply_sent"] is True
    assert "grounded workspace state" in resp.json()["reply_text"]
    assert sent and "grounded workspace state" in sent[0]["text"]


def test_telegram_ingest_answers_next_appointment_from_calendar_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-calendar")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-calendar")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 10}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)
    client.app.state.container.channel_runtime.ingest_observation(
        principal_id="exec-telegram-calendar",
        channel="calendar",
        event_type="office_signal_calendar_note",
        payload={
            "title": "Design Review",
            "summary": "Design Review",
            "start_at": "2099-01-01T15:00:00+01:00",
            "location": "Studio",
            "attendees": ["Alex Example"],
        },
        source_id="calendar-event:test-1",
        external_id="calendar-event:test-1",
        dedupe_key="calendar-event:test-1",
    )
    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 71,
            "message": {
                "chat": {"id": 9696},
                "text": "Can u answer everything now? what is my next appointment?",
                "message_id": 20,
                "date": 123,
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_sent"] is True
    assert "Your next appointment is Design Review" in body["reply_text"]
    assert "Location: Studio." in body["reply_text"]
    assert "Alex Example" in body["reply_text"]


def test_telegram_ingest_answers_focus_on_tomorrow_from_calendar_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-focus")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-focus")
    from app.api.routes import channels as channels_route
    tomorrow_vienna = (datetime.now(ZoneInfo("Europe/Vienna")) + timedelta(days=1)).replace(
        hour=9,
        minute=30,
        second=0,
        microsecond=0,
    )

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 11}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)
    client.app.state.container.channel_runtime.ingest_observation(
        principal_id="exec-telegram-focus",
        channel="calendar",
        event_type="office_signal_calendar_note",
        payload={
            "title": "Strategy Review",
            "summary": "Strategy Review",
            "start_at": tomorrow_vienna.isoformat(),
            "location": "HQ",
        },
        source_id="calendar-event:test-2",
        external_id="calendar-event:test-2",
        dedupe_key="calendar-event:test-2",
    )
    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 73,
            "message": {
                "chat": {"id": 9799},
                "text": "What should I focus on tomorrow?",
                "message_id": 31,
                "date": 123,
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_sent"] is True
    assert "Tomorrow, focus first on Strategy Review at 09:30." in body["reply_text"]
    assert "Location: HQ." in body["reply_text"]


def test_telegram_local_assistant_focus_ignores_sync_noise(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace
    from app.product.models import EvidenceRef

    class _FakeProductService:
        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {
                "preference_nodes": [
                    {
                        "domain": "life_admin",
                        "category": "insurance_admin",
                        "key": "rehab_authorization_management",
                        "status": "active",
                        "confidence": 0.9,
                    },
                    {
                        "domain": "family_admin",
                        "category": "school_admin",
                        "key": "school_and_kindergarten_coordination",
                        "status": "active",
                        "confidence": 0.88,
                    },
                ]
            }

        def list_office_events(self, *, principal_id: str, limit: int = 20, **kwargs):
            return [
                {"channel": "gmail", "summary": "Signal from Amazon.de"},
                {"channel": "gmail", "summary": "google workspace signal sync completed"},
                {"channel": "product", "summary": "workspace signal sync completed"},
                {"channel": "gmail", "summary": "Please review the revised board packet before send."},
            ]

        def list_queue(self, *, principal_id: str, limit: int = 3, **kwargs):
            return [
                SimpleNamespace(
                    title="Approve reply to Arc'teryx",
                    summary="Reply to Arc'teryx | Re: Arc'teryx Rücksendung gestartet | email thread",
                ),
                SimpleNamespace(title='Review apartment alert: "Mietwohnungen 2,20, 09" hat 2 neue Anzeigen für dich gefunden', summary=""),
            ]

    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(channels_route, "_telegram_upcoming_calendar_events", lambda *args, **kwargs: [])
    client = _client(principal_id="exec-telegram-focus-noise", operator=False)
    reply = channels_route._telegram_local_assistant_reply_text(
        client.app.state.container,
        principal_id="exec-telegram-focus-noise",
        text="What should I focus on tomorrow?",
    )
    assert "sync completed" not in reply.lower()
    assert "signal from amazon" not in reply.lower()
    assert "Top priority looks like Approve reply to Arc'teryx." in reply
    assert "Reply to Arc'teryx |" not in reply
    assert "Arc'teryx Rücksendung gestartet | email thread" in reply
    assert "Apartment alert: Mietwohnungen 2,20, 09 (2 new listings)" in reply
    assert "Profile-based focus:" in reply
    assert "Insurance admin is a real theme" in reply


def test_telegram_async_worker_sends_real_ea_followup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 21}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route, "_telegram_real_ea_reply_text", lambda **kwargs: "Here is the real EA answer.")
    client = _client(principal_id="exec-telegram-fallback", operator=False)
    channels_route._telegram_async_assistant_reply_worker(
        container=client.app.state.container,
        principal_id="exec-telegram-fallback",
        bot_config={"token": "telegram-token-fallback"},
        chat_id="9797",
        text="Tell me something useful",
        current_message_id="21",
    )
    assert sent and sent[0]["text"] == "Here is the real EA answer."


def test_telegram_async_worker_routes_supported_property_link_to_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route

    client = _client(principal_id="exec-telegram-property-link-worker", operator=False)
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        channels_route,
        "build_product_service",
        lambda container: SimpleNamespace(
            deliver_telegram_property_link_bundle=lambda **kwargs: observed.update(kwargs) or {"status": "sent"}
        ),
    )
    monkeypatch.setattr(
        channels_route,
        "_record_telegram_async_sent",
        lambda *args, **kwargs: observed.update({"async_sent_reply_text": kwargs.get("reply_text")}),
        raising=False,
    )
    monkeypatch.setattr(channels_route, "_record_telegram_async_failed", lambda *args, **kwargs: observed.update({"async_failed_stage": kwargs.get("stage")}))
    monkeypatch.setattr(channels_route, "_telegram_real_ea_reply_text", lambda **kwargs: (_ for _ in ()).throw(AssertionError("real EA fallback should not run for property links")))

    property_url = "https://www.immobilienscout24.at/expose/telegram-property-link-worker"
    channels_route._telegram_async_assistant_reply_worker(
        container=client.app.state.container,
        principal_id="exec-telegram-property-link-worker",
        bot_config={"token": "telegram-token-property-link"},
        chat_id="9799",
        text=property_url,
        current_message_id="31",
    )

    assert observed["principal_id"] == "exec-telegram-property-link-worker"
    assert observed["property_url"] == property_url
    assert observed["actor"] == "telegram_property_link"
    assert observed["preference_person_id"] == "self"
    assert observed["async_sent_reply_text"] == f"property_link_bundle_sent:{property_url}"
    assert "async_failed_stage" not in observed


def test_telegram_link_turn_decision_reports_login_walled_broker_portal() -> None:
    from app.api.routes import channels as channels_route

    client = _client(principal_id="exec-telegram-broker-portal", operator=False)
    portal_url = "https://trend-immobilien.service.immo/objekt/16915684/details/angebot/34065127"
    ctx = SimpleNamespace(
        normalized=portal_url,
        container=client.app.state.container,
        principal_id="exec-telegram-broker-portal",
        current_message_id="1713",
    )

    decision = channels_route._telegram_link_turn_decision(ctx)

    assert decision.schedule_async is False
    assert "authenticated service-portal session" in decision.reply_text
    assert "3D tour, flythrough, or dossier" in decision.reply_text
    assert "PropertyQuarry cannot truthfully build" in decision.reply_text
    assert "EA cannot" not in decision.reply_text
    assert "EA can continue" not in decision.reply_text


def test_telegram_property_processing_ack_uses_propertyquarry_brand() -> None:
    from app.api.routes import channels as channels_route

    text = channels_route._telegram_processing_ack_text(
        "https://www.immobilienscout24.at/expose/telegram-property-link-worker",
        render_priority="paid",
    )

    assert "PropertyQuarry is processing this listing now" in text
    assert "EA is processing" not in text


def test_telegram_property_alert_policy_reply_uses_propertyquarry_brand(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route

    client = _client(principal_id="exec-telegram-property-policy", operator=False)
    monkeypatch.setattr(
        channels_route,
        "build_product_service",
        lambda container: SimpleNamespace(
            update_property_alert_policy=lambda **kwargs: {"good_fit_min_score": kwargs["good_fit_min_score"]}
        ),
    )

    reply = channels_route._telegram_property_alert_policy_reply(
        client.app.state.container,
        principal_id="exec-telegram-property-policy",
        lower="do all of that by itself",
    )

    assert "PropertyQuarry will now score and rank property alerts automatically" in reply
    assert "EA will now score" not in reply


def test_telegram_ingest_schedules_async_without_placeholder_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-async")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-async")
    from app.api.routes import channels as channels_route

    seen: list[dict[str, object]] = []

    def _fake_schedule_async_assistant_reply(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(channels_route, "_telegram_schedule_async_assistant_reply", _fake_schedule_async_assistant_reply)
    client = _client(principal_id="", operator=False)
    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 72,
            "message": {
                "chat": {"id": 9798},
                "text": "Tell me something useful",
                "message_id": 30,
                "date": 123,
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_sent"] is False
    assert body["reply_text"] == ""
    assert seen and seen[0]["chat_id"] == "9798"


def test_telegram_ingest_schedules_supported_property_link_for_async_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-property-link")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-property-link")
    from app.api.routes import channels as channels_route

    seen: list[dict[str, object]] = []

    def _fake_schedule_async_assistant_reply(**kwargs):
        seen.append(kwargs)

    monkeypatch.setattr(channels_route, "_telegram_schedule_async_assistant_reply", _fake_schedule_async_assistant_reply)
    client = _client(principal_id="", operator=False)
    property_url = "https://www.immobilienscout24.at/expose/telegram-property-link-ingest"
    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update_id": 73,
            "message": {
                "chat": {"id": 9799},
                "text": property_url,
                "message_id": 31,
                "date": 123,
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_sent"] is False
    assert body["reply_text"] == ""
    assert seen and seen[0]["chat_id"] == "9799"
    assert seen[0]["text"] == property_url
    assert seen[0]["current_message_id"] == "31"


def test_telegram_real_ea_reply_text_calls_upstream_with_required_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from app.product.models import EvidenceRef
    from types import SimpleNamespace

    seen: dict[str, object] = {}

    class _Result:
        def __init__(self, text: str) -> None:
            self.text = text

    def _fake_generate_upstream_text(**kwargs):
        seen.update(kwargs)
        return _Result("EA says hello.")

    class _FakeProductService:
        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {
                "preference_nodes": [
                    {
                        "domain": "willhaben",
                        "status": "active",
                        "key": "preferred_areas",
                        "value_json": ["Waehring", "Doebling"],
                        "confidence": 0.95,
                    },
                    {
                        "domain": "willhaben",
                        "status": "active",
                        "key": "avoid_heating_types",
                        "value_json": ["gasheizung"],
                        "confidence": 1.0,
                    },
                ]
            }

        def list_office_events(self, *, principal_id: str, limit: int = 12, **kwargs):
            return [
                {"channel": "product", "event_type": "property_alert_review_created", "summary": "New property alert analyzed."},
                {"channel": "gmail", "event_type": "office_signal_email", "summary": "Reply from Arc'teryx needs approval."},
            ]

        def list_brief_items(self, *, principal_id: str, limit: int = 5, **kwargs):
            return [
                SimpleNamespace(
                    id="brief-strong-waehring",
                    score=97.0,
                    title="Strong Waehring listing",
                    why_now="High-fit property alert with 360 media and preferred district match.",
                    recommended_action="review property alert",
                    object_ref="willhaben:1411708198",
                    profile_followup_refs=("profile_followup:insurance_admin:rehab_authorization_management",),
                    evidence_refs=(
                        EvidenceRef(
                            ref_id="listing:1411708198",
                            href="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1180-waehring/1411708198/",
                            label="Willhaben listing",
                        ),
                    ),
                ),
                SimpleNamespace(
                    id="brief-arcteryx-approval",
                    score=82.0,
                    title="Arc'teryx approval",
                    why_now="Approval is waiting and blocks the next outbound reply.",
                    recommended_action="approve draft",
                    object_ref="gmail-thread:arc-1",
                    evidence_refs=(),
                ),
                SimpleNamespace(
                    id="brief-doebling-listing",
                    score=91.0,
                    title="Strong Doebling listing",
                    why_now="Another high-fit property alert with lift and bike access.",
                    recommended_action="compare against shortlist",
                    object_ref="willhaben:1071155412",
                    evidence_refs=(
                        EvidenceRef(
                            ref_id="listing:1071155412",
                            href="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1190-doebling/1071155412/",
                            label="Willhaben listing",
                        ),
                    ),
                ),
            ]

        def list_queue(self, *, principal_id: str, limit: int = 5, **kwargs):
            return [
                SimpleNamespace(
                    id="queue-property-1411708198",
                    priority="high",
                    rank_score=96.0,
                    title="Review apartment alert: Strong Waehring listing",
                    summary="Personal fit 96/100 · shortlist · The listing is in Waehring, which matches established district preferences.",
                    profile_followup_refs=("profile_followup:insurance_admin:rehab_authorization_management",),
                    evidence_refs=(
                        EvidenceRef(
                            ref_id="listing:1411708198",
                            href="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1180-waehring/1411708198/",
                            label="Willhaben listing",
                        ),
                    ),
                ),
                SimpleNamespace(
                    id="queue-approval-arcteryx",
                    priority="high",
                    rank_score=0.0,
                    title="Approve reply to Arc'teryx",
                    summary="Arc'teryx Rücksendung gestartet | email thread",
                    evidence_refs=(
                        EvidenceRef(
                            ref_id="gmail-thread:arc-1",
                            href="",
                            label="Email thread",
                        ),
                    ),
                ),
            ]

    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(channels_route, "_telegram_upcoming_calendar_events", lambda *args, **kwargs: [])
    monkeypatch.setattr(channels_route.responses_route, "_generate_upstream_text", _fake_generate_upstream_text)
    client = _client(principal_id="exec-telegram-upstream", operator=False)
    container = client.app.state.container
    container.channel_runtime.ingest_observation(
        principal_id="exec-telegram-upstream",
        channel="telegram",
        event_type="telegram.message",
        payload={"text": "What should I focus on tomorrow?"},
        source_id="telegram:1354554303",
        external_id="29",
        dedupe_key="telegram-history-29",
    )
    container.channel_runtime.ingest_observation(
        principal_id="exec-telegram-upstream",
        channel="telegram",
        event_type="telegram.reply_async_sent",
        payload={"reply_text": "Top priority is the Waehring property review."},
        source_id="telegram:1354554303",
        external_id="30",
        dedupe_key="telegram-history-30",
    )
    reply = channels_route._telegram_real_ea_reply_text(
        container=container,
        principal_id="exec-telegram-upstream",
        text="test",
        current_message_id="31",
        preferred_onemin_labels=("fallback_1",),
    )
    assert reply == "EA says hello."
    assert seen["chatplayground_audit_callback"] is None
    assert seen["chatplayground_audit_callback_only"] is False
    assert seen["chatplayground_audit_principal_id"] == "exec-telegram-upstream"
    assert seen["preferred_onemin_labels"] == ("fallback_1",)
    system_messages = [item["content"] for item in seen["messages"] if item["role"] == "system"]
    assert len(system_messages) >= 2
    prompt_text = str(system_messages[0])
    grounding_text = str(system_messages[1])
    assert "Recent conversation focus:" in grounding_text
    assert "- user: What should I focus on tomorrow?" in grounding_text
    assert "- assistant: Top priority is the Waehring property review." in grounding_text
    assert "Likely active subjects for short follow-ups:" in grounding_text
    assert "- the Waehring property review" in grounding_text
    assert "Last active object map:" in grounding_text
    assert "active_property_candidate: Strong Waehring listing | willhaben:1411708198" in grounding_text
    assert "active_queue_item: Review apartment alert: Strong Waehring listing | queue-property-1411708198" in grounding_text
    assert "active_property_profile_refs: profile_followup:insurance_admin:rehab_authorization_management" in grounding_text
    assert "active_queue_profile_refs: profile_followup:insurance_admin:rehab_authorization_management" in grounding_text
    assert "active_email_thread: gmail-thread:arc-1" in grounding_text
    assert "Active housing preferences:" in grounding_text
    assert "preferred_areas: Waehring, Doebling" in grounding_text
    assert "avoid_heating_types: gasheizung" in grounding_text
    assert "Active admin focus:" in grounding_text
    assert "Insurance admin is a real theme" in grounding_text
    assert "Top brief items:" in grounding_text
    assert "Strong Waehring listing (score 97)" in grounding_text
    assert "next: review property alert" in grounding_text
    assert "refs: brief-strong-waehring, willhaben:1411708198, listing:1411708198" in grounding_text
    assert "profile refs: profile_followup:insurance_admin:rehab_authorization_management" in grounding_text
    assert "Top property comparisons:" in grounding_text
    assert "option 1: Strong Waehring listing (score 97)" in grounding_text
    assert "option 2: Strong Doebling listing (score 91)" in grounding_text
    assert "Top queue items:" in grounding_text
    assert "Review apartment alert: Strong Waehring listing" in grounding_text
    assert "rank 96" in grounding_text
    assert "refs: queue-property-1411708198, listing:1411708198" in grounding_text
    assert "profile refs: profile_followup:insurance_admin:rehab_authorization_management" in grounding_text
    assert "Approve reply to Arc'teryx" in grounding_text
    assert "Treat short follow-ups like 'well?', 'and?', 'why?', or 'again?'" in prompt_text
    non_system_messages = [item for item in seen["messages"] if item["role"] != "system"]
    serialized = json.dumps(non_system_messages)
    assert "What should I focus on tomorrow?" in serialized
    assert "Top priority is the Waehring property review." in serialized


def test_telegram_office_grounding_uses_persisted_active_object_map(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route

    class _FakeProductService:
        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {
                "preference_nodes": [
                    {
                        "domain": "life_admin",
                        "category": "utilities_admin",
                        "key": "utility_and_provider_account_management",
                        "status": "active",
                        "confidence": 0.86,
                    }
                ]
            }

        def list_office_events(self, *, principal_id: str, limit: int = 12, **kwargs):
            return []

        def list_brief_items(self, *, principal_id: str, limit: int = 5, **kwargs):
            return []

        def list_queue(self, *, principal_id: str, limit: int = 5, **kwargs):
            return []

    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(channels_route, "_telegram_upcoming_calendar_events", lambda *args, **kwargs: [])
    client = _client(principal_id="exec-telegram-persisted-map", operator=False)
    container = client.app.state.container
    container.channel_runtime.ingest_observation(
        principal_id="exec-telegram-persisted-map",
        channel="telegram",
        event_type="telegram.reply_sent",
        payload={
            "chat_id": "1354554303",
            "reply_text": "Strong Waehring listing still looks best.",
            "active_object_map": {
                "active_property_candidate": "Strong Waehring listing | willhaben:1411708198",
                "active_property_refs": "brief-strong-waehring, willhaben:1411708198, listing:1411708198",
                "active_property_profile_refs": "profile_followup:insurance_admin:rehab_authorization_management",
                "active_queue_item": "Review apartment alert: Strong Waehring listing | queue-property-1411708198",
                "active_queue_profile_refs": "profile_followup:insurance_admin:rehab_authorization_management",
                "active_email_thread": "gmail-thread:arc-1",
            },
            "intent_state": {
                "active_intent": "property_compare",
                "active_profile_themes": "profile_followup:insurance_admin:rehab_authorization_management",
            },
            "comparison_state": {
                "comparison_primary": "Strong Waehring listing | willhaben:1411708198",
                "comparison_primary_reason": "High-fit property alert with 360 media and preferred district match.",
                "comparison_primary_action": "review property alert",
                "comparison_primary_score": "97",
                "comparison_secondary": "Strong Doebling listing | willhaben:1071155412",
                "comparison_secondary_reason": "Another high-fit property alert with lift and bike access.",
                "comparison_secondary_action": "compare against shortlist",
                "comparison_secondary_score": "91",
                "comparison_pair": "Strong Waehring listing | willhaben:1411708198 || Strong Doebling listing | willhaben:1071155412",
                "comparison_pair_refs": "brief-strong-waehring, willhaben:1411708198, listing:1411708198 ; brief-doebling-listing, willhaben:1071155412, listing:1071155412",
            },
        },
        source_id="telegram:1354554303",
        external_id="701",
        dedupe_key="telegram-persisted-map-701",
    )
    grounding_text = channels_route._telegram_office_grounding_text(
        container,
        principal_id="exec-telegram-persisted-map",
    )
    assert "Last active object map:" in grounding_text
    assert "active_property_candidate: Strong Waehring listing | willhaben:1411708198" in grounding_text
    assert "active_property_refs: brief-strong-waehring, willhaben:1411708198, listing:1411708198" in grounding_text
    assert "active_property_profile_refs: profile_followup:insurance_admin:rehab_authorization_management" in grounding_text
    assert "active_queue_item: Review apartment alert: Strong Waehring listing | queue-property-1411708198" in grounding_text
    assert "active_queue_profile_refs: profile_followup:insurance_admin:rehab_authorization_management" in grounding_text
    assert "active_email_thread: gmail-thread:arc-1" in grounding_text
    assert "Last active intent:" in grounding_text
    assert "active_intent: property_compare" in grounding_text
    assert "active_profile_themes: profile_followup:insurance_admin:rehab_authorization_management" in grounding_text
    assert "Active admin focus:" in grounding_text
    assert "Utility admin is active" in grounding_text
    assert "Last comparison pair:" in grounding_text
    assert "comparison_primary: Strong Waehring listing | willhaben:1411708198" in grounding_text
    assert "comparison_primary_reason: High-fit property alert with 360 media and preferred district match." in grounding_text
    assert "comparison_primary_action: review property alert" in grounding_text
    assert "comparison_primary_score: 97" in grounding_text
    assert "comparison_secondary: Strong Doebling listing | willhaben:1071155412" in grounding_text
    assert "comparison_secondary_reason: Another high-fit property alert with lift and bike access." in grounding_text
    assert "comparison_secondary_action: compare against shortlist" in grounding_text
    assert "comparison_secondary_score: 91" in grounding_text
    assert "comparison_pair: Strong Waehring listing | willhaben:1411708198 || Strong Doebling listing | willhaben:1071155412" in grounding_text


def test_telegram_reinforces_active_object_map_from_reply_text() -> None:
    from app.api.routes import channels as channels_route
    from app.product.models import EvidenceRef
    from types import SimpleNamespace

    brief_items = [
        SimpleNamespace(
            id="brief-strong-waehring",
            score=97.0,
            title="Strong Waehring listing",
            object_ref="willhaben:1411708198",
            evidence_refs=(
                EvidenceRef(ref_id="listing:1411708198", href="", label="Willhaben listing"),
            ),
        ),
        SimpleNamespace(
            id="brief-doebling-listing",
            score=91.0,
            title="Strong Doebling listing",
            object_ref="willhaben:1071155412",
            evidence_refs=(
                EvidenceRef(ref_id="listing:1071155412", href="", label="Willhaben listing"),
            ),
        ),
    ]
    queue_items = [
        SimpleNamespace(
            id="queue-property-1411708198",
            priority="high",
            rank_score=96.0,
            title="Review apartment alert: Strong Waehring listing",
            evidence_refs=(),
        ),
        SimpleNamespace(
            id="queue-property-1071155412",
            priority="high",
            rank_score=91.0,
            title="Review apartment alert: Strong Doebling listing",
            evidence_refs=(),
        ),
    ]
    base_map = channels_route._telegram_build_active_object_map(brief_items, queue_items)
    reinforced = channels_route._telegram_reinforce_active_object_map_from_reply(
        base_map,
        brief_items=brief_items,
        queue_items=queue_items,
        reply_text="The Strong Doebling listing looks like the better alternative right now.",
    )
    assert reinforced["active_property_candidate"] == "Strong Doebling listing | willhaben:1071155412"
    assert "listing:1071155412" in reinforced["active_property_refs"]


def test_telegram_reinforces_comparison_state_from_reply_text() -> None:
    from app.api.routes import channels as channels_route
    from app.product.models import EvidenceRef
    from types import SimpleNamespace

    brief_items = [
        SimpleNamespace(
            id="brief-strong-waehring",
            score=97.0,
            title="Strong Waehring listing",
            object_ref="willhaben:1411708198",
            why_now="High-fit property alert",
            recommended_action="review property alert",
            evidence_refs=(
                EvidenceRef(ref_id="listing:1411708198", href="", label="Willhaben listing"),
            ),
        ),
        SimpleNamespace(
            id="brief-doebling-listing",
            score=91.0,
            title="Strong Doebling listing",
            object_ref="willhaben:1071155412",
            why_now="Another strong alternative",
            recommended_action="compare against shortlist",
            evidence_refs=(
                EvidenceRef(ref_id="listing:1071155412", href="", label="Willhaben listing"),
            ),
        ),
    ]
    base_state = channels_route._telegram_build_comparison_state(brief_items)
    reinforced = channels_route._telegram_reinforce_comparison_state_from_reply(
        base_state,
        brief_items=brief_items,
        reply_text="The Strong Doebling listing is the better comparison target now.",
    )
    assert reinforced["comparison_primary"] == "Strong Doebling listing | willhaben:1071155412"
    assert reinforced["comparison_primary_reason"] == "Another strong alternative"
    assert reinforced["comparison_primary_action"] == "compare against shortlist"
    assert reinforced["comparison_primary_score"] == "91"
    assert reinforced["comparison_secondary"] == "Strong Waehring listing | willhaben:1411708198"
    assert reinforced["comparison_secondary_reason"] == "High-fit property alert"
    assert reinforced["comparison_secondary_action"] == "review property alert"
    assert reinforced["comparison_secondary_score"] == "97"
    assert reinforced["comparison_pair"].startswith(
        "Strong Doebling listing | willhaben:1071155412 || Strong Waehring listing | willhaben:1411708198"
    )
    assert "listing:1071155412" in reinforced["comparison_pair_refs"]


def test_telegram_reinforces_active_profile_themes_from_reply_text() -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    brief_items = [
        SimpleNamespace(
            id="brief-profile-rehab",
            title="Review rehab approvals and KfA authorization status",
            summary="Recurring KfA, reha, and physio/ergo authorization paperwork suggests a likely pending follow-up.",
            object_ref="profile_followup:insurance_admin:rehab_authorization_management",
            profile_followup_refs=("profile_followup:insurance_admin:rehab_authorization_management",),
        ),
    ]
    queue_items = []
    themes = channels_route._telegram_reinforced_profile_themes_from_reply(
        brief_items=brief_items,
        queue_items=queue_items,
        reply_text="Focus on the rehab approvals and KfA authorization paperwork first.",
        active_object_map={},
    )
    assert themes == "profile_followup:insurance_admin:rehab_authorization_management"


def test_telegram_build_intent_state_prefers_admin_followup_for_profile_themes() -> None:
    from app.api.routes import channels as channels_route

    intent_state = channels_route._telegram_build_intent_state(
        text="What about that paperwork?",
        reply_text="Focus on the rehab approvals first.",
        active_object_map={
            "active_queue_profile_refs": "profile_followup:insurance_admin:rehab_authorization_management",
        },
    )
    assert intent_state["active_intent"] == "admin_followup"
    assert (
        intent_state["active_profile_themes"]
        == "profile_followup:insurance_admin:rehab_authorization_management"
    )


def test_telegram_local_assistant_uses_admin_followup_theme_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    class _FakeProductService:
        def list_brief_items(self, *, principal_id: str, limit: int = 8, **kwargs):
            return [
                SimpleNamespace(
                    id="brief-profile-rehab",
                    score=88.0,
                    title="Review rehab approvals and KfA authorization status",
                    why_now="Recurring KfA, reha, and physio authorization paperwork suggests a likely pending follow-up.",
                    recommended_action="check rehab approvals",
                    object_ref="profile_followup:insurance_admin:rehab_authorization_management",
                    profile_followup_refs=("profile_followup:insurance_admin:rehab_authorization_management",),
                ),
            ]

        def list_queue(self, *, principal_id: str, limit: int = 8, **kwargs):
            return []

        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {"preference_nodes": []}

    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(
        channels_route,
        "_telegram_recent_persisted_intent_state",
        lambda container, *, principal_id: {
            "active_intent": "admin_followup",
            "active_profile_themes": "profile_followup:insurance_admin:rehab_authorization_management",
        },
    )
    monkeypatch.setattr(channels_route, "_telegram_recent_persisted_object_map", lambda container, *, principal_id: {})
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-admin-followup", operator=False).app.state.container,
        principal_id="exec-telegram-admin-followup",
        text="What about that paperwork?",
    )
    assert "Review rehab approvals and KfA authorization status" in reply
    assert "Next: check rehab approvals." in reply


def test_telegram_local_assistant_uses_second_admin_followup_for_after_that(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    class _FakeProductService:
        def list_brief_items(self, *, principal_id: str, limit: int = 8, **kwargs):
            return []

        def list_queue(self, *, principal_id: str, limit: int = 8, **kwargs):
            return [
                SimpleNamespace(
                    id="queue-rehab",
                    priority="high",
                    rank_score=96.0,
                    title="Check KfA rehab authorization",
                    summary="Rehab approval and KfA paperwork still need review.",
                    profile_followup_refs=("profile_followup:insurance_admin:rehab_authorization_management",),
                ),
                SimpleNamespace(
                    id="queue-school",
                    priority="high",
                    rank_score=85.0,
                    title="Review Noah school paperwork",
                    summary="School enrollment and coordination paperwork need a pass.",
                    profile_followup_refs=("profile_followup:school_admin:school_and_kindergarten_coordination",),
                ),
            ]

        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {"preference_nodes": []}

    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(
        channels_route,
        "_telegram_recent_persisted_intent_state",
        lambda container, *, principal_id: {
            "active_intent": "admin_followup",
            "active_profile_themes": (
                "profile_followup:insurance_admin:rehab_authorization_management, "
                "profile_followup:school_admin:school_and_kindergarten_coordination"
            ),
        },
    )
    monkeypatch.setattr(channels_route, "_telegram_recent_persisted_object_map", lambda container, *, principal_id: {})
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-admin-followup-2", operator=False).app.state.container,
        principal_id="exec-telegram-admin-followup-2",
        text="And after that?",
    )
    assert "After that, focus on Review Noah school paperwork." in reply


def test_telegram_enriches_intent_state_with_admin_followup_primary_and_secondary() -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    queue_items = [
        SimpleNamespace(
            id="queue-rehab",
            priority="high",
            rank_score=96.0,
            title="Check KfA rehab authorization",
            profile_followup_refs=("profile_followup:insurance_admin:rehab_authorization_management",),
        ),
        SimpleNamespace(
            id="queue-school",
            priority="high",
            rank_score=85.0,
            title="Review Noah school paperwork",
            profile_followup_refs=("profile_followup:school_admin:school_and_kindergarten_coordination",),
        ),
    ]
    enriched = channels_route._telegram_with_admin_followup_state(
        {
            "active_intent": "admin_followup",
            "active_profile_themes": (
                "profile_followup:insurance_admin:rehab_authorization_management, "
                "profile_followup:school_admin:school_and_kindergarten_coordination"
            ),
        },
        brief_items=[],
        queue_items=queue_items,
        active_object_map={},
    )
    assert enriched["active_admin_primary"] == "queue-rehab"
    assert enriched["active_admin_primary_title"] == "Check KfA rehab authorization"
    assert enriched["active_admin_secondary"] == "queue-school"
    assert enriched["active_admin_secondary_title"] == "Review Noah school paperwork"


def test_telegram_local_assistant_explains_admin_primary_from_persisted_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    class _FakeProductService:
        def list_brief_items(self, *, principal_id: str, limit: int = 8, **kwargs):
            return []

        def list_queue(self, *, principal_id: str, limit: int = 8, **kwargs):
            return [
                SimpleNamespace(
                    id="queue-rehab",
                    priority="high",
                    rank_score=96.0,
                    title="Check KfA rehab authorization",
                    summary="Rehab approval and KfA paperwork still need review.",
                    recommended_action="check rehab approvals",
                    profile_followup_refs=("profile_followup:insurance_admin:rehab_authorization_management",),
                ),
                SimpleNamespace(
                    id="queue-school",
                    priority="high",
                    rank_score=85.0,
                    title="Review Noah school paperwork",
                    summary="School enrollment and coordination paperwork need a pass.",
                    recommended_action="review school paperwork",
                    profile_followup_refs=("profile_followup:school_admin:school_and_kindergarten_coordination",),
                ),
            ]

        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {"preference_nodes": []}

    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(
        channels_route,
        "_telegram_recent_persisted_intent_state",
        lambda container, *, principal_id: {
            "active_intent": "admin_followup",
            "active_profile_themes": (
                "profile_followup:insurance_admin:rehab_authorization_management, "
                "profile_followup:school_admin:school_and_kindergarten_coordination"
            ),
            "active_admin_primary": "queue-rehab",
            "active_admin_primary_title": "Check KfA rehab authorization",
            "active_admin_secondary": "queue-school",
            "active_admin_secondary_title": "Review Noah school paperwork",
        },
    )
    monkeypatch.setattr(channels_route, "_telegram_recent_persisted_object_map", lambda container, *, principal_id: {})
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-admin-why", operator=False).app.state.container,
        principal_id="exec-telegram-admin-why",
        text="Why that one?",
    )
    assert "That one leads because Rehab approval and KfA paperwork still need review." in reply
    assert "Next: check rehab approvals." in reply


def test_telegram_local_assistant_can_answer_named_ltd_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    monkeypatch.setattr(
        channels_route,
        "_telegram_ltd_runtime_profiles",
        lambda container: [
            SimpleNamespace(
                service_name="MarkupGo",
                runtime_state="browseract_ui_service",
                workspace_integration_tier="Tier 3",
                aliases=("markupgo",),
                actions=(
                    SimpleNamespace(action_key="inspect_workspace"),
                    SimpleNamespace(action_key="discover_account"),
                ),
            ),
        ],
    )
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-ltd-request", operator=False).app.state.container,
        principal_id="exec-telegram-ltd-request",
        text="Use MarkupGo for this.",
    )
    assert "MarkupGo is cataloged in PropertyQuarry" in reply
    assert "does not prove credentials, provider health, or a live call" in reply
    assert "inspect_workspace" in reply


def test_telegram_local_assistant_uses_answerly_for_document_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route

    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_API_KEY", "answerly-key")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_AGENT_ID", "agent-123")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_LABEL", "Scanned OneDrive documents")
    monkeypatch.setattr(
        channels_route,
        "_answerly_chat",
        lambda **kwargs: {
            "status": True,
            "data": {
                "messages": [
                    "The latest KfA rehab approval confirms Rosenhügel NRZ and references the authorization status."
                ],
                "actionResponse": {"name": "conversational"},
                "meta": {
                    "source": [
                        {"dataItemId": "scan-akh-1"},
                        {"dataItemId": "scan-kfa-2"},
                    ]
                },
            },
        },
    )
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-answerly-doc", operator=False).app.state.container,
        principal_id="exec-telegram-answerly-doc",
        text="What does the latest OneDrive KfA rehab approval say?",
    )
    assert "The latest KfA rehab approval confirms Rosenhügel NRZ" in reply
    assert "Matched Scanned OneDrive documents Answerly items: scan-akh-1, scan-kfa-2." in reply


def test_telegram_local_assistant_routes_birth_certificate_request_to_onedrive_answerly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import channels as channels_route

    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_API_KEY", "onedrive-key")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_AGENT_ID", "agent-123")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_LABEL", "Scanned OneDrive documents")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_API_KEY", "shareone-key")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_AGENT_ID", "shareone-agent")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_LABEL", "ShareOne documents")
    answerly_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        channels_route,
        "_answerly_chat",
        lambda **kwargs: answerly_calls.append(kwargs)
        or {
            "status": True,
            "data": {
                "messages": ["Noah Girschele's birth certificate is in the scanned OneDrive documents."],
                "actionResponse": {"name": "conversational"},
                "meta": {"source": [{"dataItemId": "onedrive-birth-cert-1"}]},
            },
        },
    )
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-answerly-birth-cert", operator=False).app.state.container,
        principal_id="exec-telegram-answerly-birth-cert",
        text="Send me the birth certificate of Noah Girschele.",
    )
    assert "Noah Girschele's birth certificate is in the scanned OneDrive documents." in reply
    assert "Matched Scanned OneDrive documents Answerly items: onedrive-birth-cert-1." in reply
    assert answerly_calls[-1]["config"]["scope"] == "onedrive"


def test_telegram_local_assistant_routes_medication_whereabouts_request_to_onedrive_answerly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import channels as channels_route

    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_API_KEY", "onedrive-key")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_AGENT_ID", "agent-123")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_LABEL", "Scanned OneDrive documents")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_API_KEY", "shareone-key")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_AGENT_ID", "shareone-agent")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_LABEL", "ShareOne documents")
    answerly_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        channels_route,
        "_answerly_chat",
        lambda **kwargs: answerly_calls.append(kwargs)
        or {
            "status": True,
            "data": {
                "messages": ["Your medication is currently listed in the bedside drawer medication organizer."],
                "actionResponse": {"name": "conversational"},
                "meta": {"source": [{"dataItemId": "onedrive-medication-1"}]},
            },
        },
    )
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-answerly-medication", operator=False).app.state.container,
        principal_id="exec-telegram-answerly-medication",
        text="Where is my medication right now?",
    )
    assert "Your medication is currently listed in the bedside drawer medication organizer." in reply
    assert "Matched Scanned OneDrive documents Answerly items: onedrive-medication-1." in reply
    assert answerly_calls[-1]["config"]["scope"] == "onedrive"


def test_telegram_local_assistant_sends_onedrive_pdf_match_via_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import channels as channels_route

    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_API_KEY", "onedrive-key")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_AGENT_ID", "agent-123")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_LABEL", "Scanned OneDrive documents")
    monkeypatch.setattr(
        channels_route,
        "_answerly_chat",
        lambda **kwargs: {
            "status": True,
            "data": {
                "messages": [
                    "I found the scanned birthday and Christmas measurements PDF in the OneDrive scans."
                ],
                "actionResponse": {"name": "conversational"},
                "meta": {"source": [{"dataItemId": "onedrive-birthday-scan-1"}]},
            },
        },
    )
    captured_delivery: dict[str, object] = {}

    class _FakeProductService:
        def deliver_onedrive_document_search_to_telegram(self, **kwargs):  # type: ignore[no-untyped-def]
            captured_delivery.update(kwargs)
            return {
                "query": kwargs["query"],
                "matched_total": 1,
                "filename": "Geburtstags-und-Weihnachtsgroessen.pdf",
                "document_path": "/mnt/onedrive/Documents/Scanned Documents/Geburtstags-und-Weihnachtsgroessen.pdf",
                "document_download_url": "",
                "answerly_data_item_id": "onedrive-birthday-scan-1",
                "telegram_delivery_status": "sent",
                "telegram_delivery_error": "",
                "telegram_message_ids": ["42"],
                "telegram_chat_ref": "1354554303",
            }

    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-answerly-send-pdf", operator=False).app.state.container,
        principal_id="exec-telegram-answerly-send-pdf",
        text="Schick mir das PDF mit unseren handschriftlichen Geburtstags- und Weihnachtsgrößen aus OneDrive.",
    )
    assert "I found the scanned birthday and Christmas measurements PDF" in reply
    assert "Sent Geburtstags-und-Weihnachtsgroessen.pdf on Telegram." in reply
    assert "Matched Scanned OneDrive documents Answerly items: onedrive-birthday-scan-1." in reply
    assert captured_delivery["answerly_source_ids"] == ("onedrive-birthday-scan-1",)


def test_telegram_local_assistant_reports_unconfigured_answerly_when_named(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route

    monkeypatch.delenv("EA_ANSWERLY_ONEDRIVE_API_KEY", raising=False)
    monkeypatch.delenv("EA_ANSWERLY_ONEDRIVE_AGENT_ID", raising=False)
    monkeypatch.delenv("EA_ANSWERLY_SHAREONE_API_KEY", raising=False)
    monkeypatch.delenv("EA_ANSWERLY_SHAREONE_AGENT_ID", raising=False)
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-answerly-missing", operator=False).app.state.container,
        principal_id="exec-telegram-answerly-missing",
        text="Use Answerly to search the scanned documents.",
    )
    assert reply == "Answerly document Q&A is not configured yet in PropertyQuarry."


def test_telegram_local_assistant_requires_explicit_source_when_answerly_corpora_are_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import channels as channels_route

    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_API_KEY", "onedrive-key")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_AGENT_ID", "onedrive-agent")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_LABEL", "OneDrive documents")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_API_KEY", "shareone-key")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_AGENT_ID", "shareone-agent")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_LABEL", "ShareOne documents")
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-answerly-split", operator=False).app.state.container,
        principal_id="exec-telegram-answerly-split",
        text="Search the documents for the rehab approval.",
    )
    assert "Your document backends stay separated." in reply
    assert "OneDrive documents or ShareOne documents" in reply


def test_telegram_local_assistant_resolves_named_ltd_request_to_best_action(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    monkeypatch.setattr(
        channels_route,
        "_telegram_ltd_runtime_profiles",
        lambda container: [
            SimpleNamespace(
                service_name="MarkupGo",
                runtime_state="browseract_ui_service",
                workspace_integration_tier="Tier 3",
                aliases=("markupgo",),
                actions=(
                    SimpleNamespace(
                        action_key="inspect_workspace",
                        route_path="/v1/ltds/runtime-catalog/MarkupGo/inspect-workspace",
                        executable=True,
                        description="Inspect the MarkupGo workspace.",
                    ),
                ),
            ),
        ],
    )

    class _FakeCatalog:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(channels_route, "LtdRuntimeCatalogService", _FakeCatalog)
    monkeypatch.setattr(
        channels_route,
        "projected_task_key_for_request",
        lambda **kwargs: channels_route.projected_task_key("MarkupGo", "inspect_workspace"),
    )
    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-ltd-action", operator=False).app.state.container,
        principal_id="exec-telegram-ltd-action",
        text="Use MarkupGo for this PDF.",
    )
    assert "For MarkupGo, the catalog route is inspect_workspace." in reply
    assert "/v1/ltds/runtime-catalog/MarkupGo/inspect-workspace" in reply
    assert "Live execution is unverified" in reply
    assert "Executable now." not in reply


def test_telegram_local_assistant_executes_safe_onemin_ltd_action(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from app.domain.models import ToolInvocationResult
    from types import SimpleNamespace

    monkeypatch.setattr(
        channels_route,
        "_telegram_ltd_runtime_profiles",
        lambda container: [
            SimpleNamespace(
                service_name="1min.AI",
                runtime_state="provider_executable",
                workspace_integration_tier="Tier 1",
                aliases=("1min ai",),
                actions=(
                    SimpleNamespace(
                        action_key="background_remove",
                        route_path="/v1/ltds/runtime-catalog/1min.AI/actions/background_remove",
                        executable=True,
                        description="Remove the background from an image.",
                        tool_name="provider.onemin.media_transform",
                        action_kind="media_transform",
                    ),
                ),
            ),
        ],
    )

    class _FakeCatalog:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(channels_route, "LtdRuntimeCatalogService", _FakeCatalog)
    monkeypatch.setattr(
        channels_route,
        "projected_task_key_for_request",
        lambda **kwargs: channels_route.projected_task_key("1min.AI", "background_remove"),
    )
    captured = []

    def _fake_execute(request):  # noqa: ANN001
        captured.append(request)
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind,
            target_ref="onemin:background-remove-receipt",
            output_json={
                "provider_backend": "1min",
                "asset_urls": ["https://media.example.invalid/result.png"],
            },
            receipt_json={
                "principal_id": request.context_json["principal_id"],
                "handler_key": request.tool_name,
                "invocation_contract": "tool.v1",
                "provider_key": "onemin",
                "provider_backend": "1min",
                "feature_type": "BACKGROUND_REMOVER",
                "model": "image-model",
            },
        )

    client = _client(principal_id="exec-telegram-ltd-exec", operator=False)
    monkeypatch.setattr(client.app.state.container.tool_execution, "execute_invocation", _fake_execute)
    reply = channels_route._telegram_local_assistant_reply_text(
        client.app.state.container,
        principal_id="exec-telegram-ltd-exec",
        text="Use 1min.AI to remove the background from https://example.invalid/cat.png",
    )
    assert "Executed 1min.AI background_remove with a principal-bound provider receipt." in reply
    assert "onemin:background-remove-receipt" in reply
    assert captured[0].payload_json["feature_type"] == "BACKGROUND_REMOVER"
    assert captured[0].payload_json["image_url"] == "https://example.invalid/cat.png"
    assert captured[0].context_json["principal_id"] == "exec-telegram-ltd-exec"


def test_telegram_ltd_execution_rejects_unbound_provider_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from app.domain.models import ToolInvocationResult
    from types import SimpleNamespace

    captured = []

    def _fake_execute(request):  # noqa: ANN001
        captured.append(request)
        return ToolInvocationResult(
            tool_name=request.tool_name,
            action_kind=request.action_kind,
            target_ref="onemin:unbound-background-remove",
            output_json={
                "provider_backend": "1min",
                "asset_urls": ["https://media.example.invalid/result.png"],
            },
            receipt_json={
                "principal_id": "another-principal",
                "handler_key": request.tool_name,
                "invocation_contract": "tool.v1",
                "provider_key": "onemin",
                "provider_backend": "1min",
                "feature_type": "BACKGROUND_REMOVER",
                "model": "image-model",
            },
        )

    container = SimpleNamespace(
        tool_execution=SimpleNamespace(execute_invocation=_fake_execute),
    )
    action = SimpleNamespace(
        action_key="background_remove",
        route_path="/v1/ltds/runtime-catalog/1min.AI/actions/background_remove",
        tool_name="provider.onemin.media_transform",
        action_kind="media_transform",
    )

    reply = channels_route._telegram_try_execute_ltd_action(
        container,
        principal_id="exec-telegram-ltd-unbound",
        service_name="1min.AI",
        action=action,
        text="Remove the background from https://example.invalid/cat.png",
    )

    assert "execution is not proven" in reply
    assert "Executed" not in reply
    assert captured[0].context_json["principal_id"] == "exec-telegram-ltd-unbound"


def test_telegram_office_grounding_includes_ltd_runtime_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    class _FakeProductService:
        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {"preference_nodes": []}

        def list_office_events(self, *, principal_id: str, limit: int = 12, **kwargs):
            return []

        def list_brief_items(self, *, principal_id: str, limit: int = 5, **kwargs):
            return []

        def list_queue(self, *, principal_id: str, limit: int = 5, **kwargs):
            return []

    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(channels_route, "_telegram_upcoming_calendar_events", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        channels_route,
        "_telegram_ltd_runtime_profiles",
        lambda container: [
            SimpleNamespace(
                service_name="1min.AI",
                runtime_state="provider_executable",
                workspace_integration_tier="Tier 1",
                actions=(
                    SimpleNamespace(action_key="background_remove"),
                    SimpleNamespace(action_key="image_generate"),
                ),
            ),
            SimpleNamespace(
                service_name="MarkupGo",
                runtime_state="browseract_ui_service",
                workspace_integration_tier="Tier 3",
                actions=(SimpleNamespace(action_key="inspect_workspace"),),
            ),
        ],
    )
    grounding_text = channels_route._telegram_office_grounding_text(
        _client(principal_id="exec-telegram-ltd-grounding", operator=False).app.state.container,
        principal_id="exec-telegram-ltd-grounding",
    )
    assert "Available LTD runtime lanes:" in grounding_text
    assert "1min.AI [provider_executable] Tier 1 | actions: background_remove, image_generate" in grounding_text
    assert "MarkupGo [browseract_ui_service] Tier 3 | actions: inspect_workspace" in grounding_text


def test_telegram_office_grounding_includes_answerly_document_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import channels as channels_route

    class _FakeProductService:
        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {"preference_nodes": []}

        def list_office_events(self, *, principal_id: str, limit: int = 12, **kwargs):
            return []

        def list_brief_items(self, *, principal_id: str, limit: int = 5, **kwargs):
            return []

        def list_queue(self, *, principal_id: str, limit: int = 5, **kwargs):
            return []

    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_API_KEY", "answerly-key")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_AGENT_ID", "agent-123")
    monkeypatch.setenv("EA_ANSWERLY_ONEDRIVE_LABEL", "Scanned OneDrive documents")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_API_KEY", "answerly-key-2")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_AGENT_ID", "agent-456")
    monkeypatch.setenv("EA_ANSWERLY_SHAREONE_LABEL", "ShareOne documents")
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: _FakeProductService())
    monkeypatch.setattr(channels_route, "_telegram_upcoming_calendar_events", lambda *args, **kwargs: [])
    monkeypatch.setattr(channels_route, "_telegram_ltd_runtime_profiles", lambda container: [])
    grounding_text = channels_route._telegram_office_grounding_text(
        _client(principal_id="exec-telegram-answerly-grounding", operator=False).app.state.container,
        principal_id="exec-telegram-answerly-grounding",
    )
    assert "Document Q&A backend:" in grounding_text
    assert "Answerly connected for Scanned OneDrive documents [onedrive]." in grounding_text
    assert "Answerly connected for ShareOne documents [shareone]." in grounding_text


def test_telegram_reinforces_active_intent_to_admin_followup_from_reply_text() -> None:
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    brief_items = [
        SimpleNamespace(
            id="brief-profile-rehab",
            title="Review rehab approvals and KfA authorization status",
            why_now="Recurring KfA and rehab authorization paperwork suggests a likely pending follow-up.",
            recommended_action="check rehab approvals",
            object_ref="profile_followup:insurance_admin:rehab_authorization_management",
            profile_followup_refs=("profile_followup:insurance_admin:rehab_authorization_management",),
        ),
    ]
    reinforced = channels_route._telegram_reinforced_intent_state_from_reply(
        {
            "active_intent": "property_compare",
            "active_profile_themes": "profile_followup:insurance_admin:rehab_authorization_management",
        },
        brief_items=brief_items,
        queue_items=[],
        reply_text="Focus on the rehab approvals and KfA authorization paperwork first.",
        active_object_map={},
    )
    assert reinforced["active_intent"] == "admin_followup"
    assert (
        reinforced["active_profile_themes"]
        == "profile_followup:insurance_admin:rehab_authorization_management"
    )


def test_telegram_async_worker_sends_last_resort_reply_when_real_ea_reply_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-fallback")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-fallback")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 21}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route, "_telegram_real_ea_reply_text", lambda **kwargs: "")
    client = _client(principal_id="", operator=False)
    channels_route._telegram_async_assistant_reply_worker(
        container=client.app.state.container,
        principal_id="exec-telegram-fallback",
        bot_config={"token": "telegram-token-fallback"},
        chat_id="9797",
        text="tell me more",
        current_message_id="21",
    )
    assert sent
    assert sent[-1]["text"] == "I'm here. Give me a concrete task."


def test_telegram_async_worker_keeps_fallback_reply_free_of_stale_intent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-fallback-intent")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-fallback-intent")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 31}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route, "_telegram_real_ea_reply_text", lambda **kwargs: "")
    client = _client(principal_id="", operator=False)
    channels_route._telegram_async_assistant_reply_worker(
        container=client.app.state.container,
        principal_id="exec-telegram-fallback-intent",
        bot_config={"token": "telegram-token-fallback-intent"},
        chat_id="9796",
        text="Receiver check. Reply with one short line.",
        current_message_id="31",
    )
    observations = list(client.app.state.container.channel_runtime.list_recent_observations(limit=12, principal_id="exec-telegram-fallback-intent"))
    payload = next(dict(row.payload or {}) for row in observations if str(row.event_type) == "telegram.reply_async_sent")
    assert payload.get("reply_text") == "I'm here. Ask directly."
    assert dict(payload.get("intent_state") or {}) == {}
    assert dict(payload.get("comparison_state") or {}) == {}


def test_telegram_async_worker_sends_probe_fallback_when_real_ea_reply_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-probe-fallback")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-probe-fallback")
    from app.api.routes import channels as channels_route
    from types import SimpleNamespace

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 771}}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        sent.append({"url": getattr(request, "full_url", ""), "body": request.data.decode("utf-8") if request.data else ""})
        return _FakeResponse()

    monkeypatch.setattr(channels_route.responses_route, "_generate_upstream_text", lambda **kwargs: SimpleNamespace(text=""))
    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="exec-telegram-probe-fallback", operator=False)
    channels_route._telegram_async_assistant_reply_worker(
        container=client.app.state.container,
        principal_id="exec-telegram-probe-fallback",
        bot_config={"token": "telegram-token-probe-fallback", "preferred_onemin_labels": ()},
        chat_id="1354554303",
        text="Test",
        current_message_id="991001",
    )
    observations = list(client.app.state.container.channel_runtime.list_recent_observations(limit=12, principal_id="exec-telegram-probe-fallback"))
    assert any(str(row.event_type) == "telegram.reply_async_sent" for row in observations)
    assert sent
    assert "I'm here. Ask directly." in sent[-1]["body"]


def test_telegram_ingest_answers_question_mark_probe_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-question-probe")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-question-probe")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 991}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append(payload)
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route, "_telegram_real_ea_reply_text", lambda **kwargs: "")
    client = _client(principal_id="", operator=False)
    response = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991002,
                "date": 123,
                "text": "?",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply_sent"] is True
    assert body["reply_text"] == "Ask directly."
    assert sent[-1]["text"] == "Ask directly."
    observations = list(client.app.state.container.channel_runtime.list_recent_observations(limit=12, principal_id="exec-telegram-question-probe"))
    assert any(str(row.event_type) == "telegram.reply_sent" for row in observations)
    assert not any(str(row.event_type) == "telegram.reply_async_started" for row in observations)


def test_telegram_ingest_answers_google_photos_capability_request_from_grounded_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-google-photos")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-google-photos")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 992}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append(payload)
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [])
    monkeypatch.setattr(
        channels_route.google_oauth_service,
        "build_google_oauth_start",
        lambda **kwargs: type("Packet", (), {"auth_url": "https://accounts.google.com/o/oauth2/v2/auth?scope_bundle=full_workspace_photos"})(),
    )
    client = _client(principal_id="", operator=False)
    response = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991003,
                "date": 123,
                "text": "You should have access to my Google photos. Can you find me the picture where Noah is sleeping on a mattress?",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply_sent"] is True
    assert "I do not see a connected Google account" in body["reply_text"]
    assert "Google Photos Picker" in body["reply_text"]
    assert "https://accounts.google.com/o/oauth2/v2/auth?scope_bundle=full_workspace_photos" in body["reply_text"]
    assert '<a href="https://accounts.google.com/o/oauth2/v2/auth?scope_bundle=full_workspace_photos">Open Google navigation</a>' in sent[-1]["text"]


def test_telegram_resolve_message_payload_transcribes_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import telegram_session_service

    class _FakeResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"file_path": "voice/file-123.ogg"}}).encode("utf-8")

    monkeypatch.setattr(telegram_session_service.product_service, "_pocket_audio_fallback_available", lambda: True)
    monkeypatch.setattr(
        telegram_session_service.product_service,
        "_pocket_retranscribe_from_audio_url",
        lambda **kwargs: {
            "transcript_text": "Can you start the photo picker now?",
            "transcript_metadata": {"transcriber": "test-transcriber"},
        },
    )
    monkeypatch.setattr(telegram_session_service.urllib.request, "urlopen", lambda request, timeout=30: _FakeResponse())
    resolved = telegram_session_service.resolve_telegram_message_payload(
        payload={
            "text": "Voice Message",
            "kind": "voice",
            "message_metadata": {"file_id": "voice-file-123", "duration": 8},
            "message_id": 42,
        },
        bot_token="tg-token",
    )
    assert resolved["text"] == "Can you start the photo picker now?"
    assert resolved["transcription_status"] == "ok"
    assert dict(resolved["transcript_metadata"] or {})["telegram_file_id"] == "voice-file-123"


def test_telegram_photo_adapter_preserves_media_kind_with_caption() -> None:
    from app.channels.telegram.adapter import TelegramObservationAdapter

    text, kind, metadata = TelegramObservationAdapter._message_text_and_kind(
        {
            "caption": "What do you see here?",
            "photo": [
                {"file_id": "photo-small"},
                {"file_id": "photo-large"},
            ],
        }
    )

    assert text == "What do you see here?"
    assert kind == "photo"
    assert metadata["file_id"] == "photo-large"
    assert metadata["caption"] == "What do you see here?"


def test_telegram_resolve_message_payload_analyzes_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import telegram_session_service

    monkeypatch.setattr(
        telegram_session_service,
        "_telegram_file_download_url",
        lambda **kwargs: "https://api.telegram.org/file/bot-token/photos/file-123.jpg",
    )
    monkeypatch.setattr(
        telegram_session_service.photo_signal_analysis,
        "analyze_photo_url",
        lambda **kwargs: {
            "summary": "A living room with large windows and a couch.",
            "notable_details": ["large windows", "couch", "wood floor"],
            "suggestions": ["This is useful for interior-layout review."],
            "status": "analyzed",
        },
    )

    resolved = telegram_session_service.resolve_telegram_message_payload(
        payload={
            "text": "Please inspect this.",
            "kind": "photo",
            "message_metadata": {"file_id": "photo-file-123", "caption": "Please inspect this."},
            "message_id": 46,
        },
        bot_token="tg-token",
    )

    assert resolved["photo_analysis_status"] == "analyzed"
    assert dict(resolved["photo_analysis"] or {})["summary"] == "A living room with large windows and a couch."
    assert dict(resolved["message_metadata"] or {})["download_url"].endswith("/photos/file-123.jpg")
    assert "A living room with large windows and a couch." in str(resolved["text"])


def test_telegram_ingest_replies_from_photo_analysis_instead_of_generic_blind_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-photo")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-photo")
    from app.api.routes import channels as channels_route
    from app.services import telegram_session_service

    sent: list[dict[str, object]] = []

    class _FakeTelegramSendResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 9902}}).encode("utf-8")

    def _fake_send_urlopen(request, timeout=30):
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeTelegramSendResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_send_urlopen)
    monkeypatch.setattr(
        telegram_session_service,
        "_telegram_file_download_url",
        lambda **kwargs: "https://api.telegram.org/file/bot-photo/photos/file-777.jpg",
    )
    monkeypatch.setattr(
        telegram_session_service.photo_signal_analysis,
        "analyze_photo_url",
        lambda **kwargs: {
            "summary": "A family photo in a hospital room.",
            "notable_details": ["two people", "hospital bed"],
            "suggestions": ["This likely belongs in the family memorial thread."],
            "status": "analyzed",
        },
    )

    client = _client(principal_id="exec-telegram-photo")
    response = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "message": {
                "message_id": 9001,
                "date": 123456,
                "chat": {"id": 1354554303, "type": "private"},
                "caption": "Can you identify this?",
                "photo": [{"file_id": "small-1"}, {"file_id": "large-1"}],
            }
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply_sent"] is True
    assert "I got the photo." in body["reply_text"]
    assert "A family photo in a hospital room." in body["reply_text"]
    assert "can't see it" not in body["reply_text"].lower()
    assert sent
    assert "A family photo in a hospital room." in str(sent[-1]["text"])


def test_telegram_resolve_message_payload_sanitizes_transcription_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import telegram_session_service

    monkeypatch.setattr(telegram_session_service.product_service, "_pocket_audio_fallback_available", lambda: True)
    monkeypatch.setattr(
        telegram_session_service,
        "_telegram_file_download_url",
        lambda **kwargs: "https://api.telegram.org/file/bot-secret-token/voice/file-123.ogg",
    )

    def _raise_failure(**kwargs):
        raise RuntimeError("telegram_getfile_http_401:https://api.telegram.org/file/bot-secret-token/voice/file-123.ogg")

    monkeypatch.setattr(
        telegram_session_service.product_service,
        "_pocket_retranscribe_from_audio_url",
        _raise_failure,
    )
    resolved = telegram_session_service.resolve_telegram_message_payload(
        payload={
            "text": "Voice Message",
            "kind": "voice",
            "message_metadata": {"file_id": "voice-file-123", "duration": 8},
            "message_id": 43,
        },
        bot_token="tg-token",
    )
    assert resolved["transcription_status"] == "failed"
    assert resolved["transcription_error_code"] == "telegram_getfile_http_401"
    assert "bot-secret-token" not in json.dumps(resolved)


def test_telegram_resolve_message_payload_skips_long_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import telegram_session_service

    monkeypatch.setenv("EA_TELEGRAM_MAX_AUDIO_TRANSCRIBE_SECONDS", "10")
    monkeypatch.setattr(telegram_session_service.product_service, "_pocket_audio_fallback_available", lambda: True)
    called: list[dict[str, object]] = []
    monkeypatch.setattr(
        telegram_session_service,
        "_telegram_file_download_url",
        lambda **kwargs: called.append(dict(kwargs)) or "https://example.invalid/audio.ogg",
    )
    resolved = telegram_session_service.resolve_telegram_message_payload(
        payload={
            "text": "Voice Message",
            "kind": "voice",
            "message_metadata": {"file_id": "voice-file-999", "duration": 11},
            "message_id": 44,
        },
        bot_token="tg-token",
    )
    assert resolved["transcription_status"] == "skipped"
    assert resolved["transcription_error_code"] == "duration_limit"
    assert called == []


def test_telegram_resolve_message_payload_truncates_long_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import telegram_session_service

    monkeypatch.setenv("EA_TELEGRAM_MAX_TRANSCRIPT_CHARS", "32")
    monkeypatch.setattr(telegram_session_service.product_service, "_pocket_audio_fallback_available", lambda: True)
    monkeypatch.setattr(
        telegram_session_service,
        "_telegram_file_download_url",
        lambda **kwargs: "https://example.invalid/audio.ogg",
    )
    monkeypatch.setattr(
        telegram_session_service.product_service,
        "_pocket_retranscribe_from_audio_url",
        lambda **kwargs: {
            "transcript_text": "This is a very long transcript that should be truncated before it enters the Telegram session payload.",
            "transcript_metadata": {"transcriber": "test-transcriber"},
        },
    )
    resolved = telegram_session_service.resolve_telegram_message_payload(
        payload={
            "text": "Voice Message",
            "kind": "voice",
            "message_metadata": {"file_id": "voice-file-555", "duration": 8},
            "message_id": 45,
        },
        bot_token="tg-token",
    )
    assert resolved["transcription_status"] == "ok"
    assert len(resolved["text"]) <= 35
    assert resolved["text"].endswith("...")


def test_telegram_ingest_deduped_voice_message_skips_retranscription(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-deduped-voice")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-deduped-voice")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []
    resolve_calls: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 995}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append(payload)
        return _FakeResponse()

    def _fake_resolve_message_payload(*, payload, bot_token):
        resolve_calls.append(dict(payload or {}))
        return {
            **dict(payload or {}),
            "text": "Can you start the photo picker now?",
            "transcription_status": "ok",
        }

    class _Account:
        def __init__(self):
            self.token_status = "active"
            self.binding = type("Binding", (), {"status": "enabled"})()
            self.granted_scopes = [channels_route.google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER]
            self.google_email = "person@example.test"

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route, "resolve_telegram_message_payload", _fake_resolve_message_payload)
    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [_Account()])
    client = _client(principal_id="", operator=False)
    first = client.post(
        "/v1/channels/telegram/ingest",
        json={"message": {"message_id": 991500, "date": 123, "voice": {"file_id": "voice-file-1", "duration": 8}, "chat": {"id": 1354554303, "type": "private"}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert first.status_code == 200
    assert resolve_calls
    first_count = len(resolve_calls)
    second = client.post(
        "/v1/channels/telegram/ingest",
        json={"message": {"message_id": 991500, "date": 123, "voice": {"file_id": "voice-file-1", "duration": 8}, "chat": {"id": 1354554303, "type": "private"}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert second.status_code == 200
    assert len(resolve_calls) == first_count


def test_telegram_ingest_answers_done_from_recent_google_photos_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-google-photos-done")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-google-photos-done")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 994}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append(payload)
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    class _Account:
        def __init__(self):
            self.token_status = "active"
            self.binding = type("Binding", (), {"status": "enabled"})()
            self.granted_scopes = [channels_route.google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER]
            self.google_email = "person@example.test"

    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [_Account()])
    monkeypatch.setattr(
        channels_route.google_oauth_service,
        "build_google_oauth_start",
        lambda **kwargs: type("Packet", (), {"auth_url": "https://accounts.google.com/o/oauth2/v2/auth?scope_bundle=full_workspace_photos"})(),
    )
    client = _client(principal_id="", operator=False)
    product_service = channels_route.build_product_service(client.app.state.container)
    monkeypatch.setattr(
        product_service,
        "create_google_photos_picker_session",
        lambda **kwargs: {"picker_uri": "https://photos.app/picker/session-123/autoclose"},
    )
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: product_service)

    first = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991005,
                "date": 123,
                "text": "You should have access to my Google photos. Can you find me the picture where Noah is sleeping on a mattress?",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert first.status_code == 200

    second = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991006,
                "date": 124,
                "text": "Done",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["reply_sent"] is True
    assert "Google Photos Picker is ready" in body["reply_text"]
    assert "https://photos.app/picker/session-123/autoclose" in body["reply_text"]
    observations = list(client.app.state.container.channel_runtime.list_recent_observations(limit=20, principal_id="exec-telegram-google-photos-done"))
    assert any(
        str(row.event_type) == "telegram.reply_sent" and "Google Photos Picker is ready" in str(dict(row.payload or {}).get("reply_text") or "")
        for row in observations
    )
    assert not any(
        str(row.event_type) == "telegram.reply_async_started" and "Done" in str(dict(row.payload or {}).get("prompt_text") or "")
        for row in observations
    )


def test_telegram_ingest_suppresses_repeated_done_when_google_photos_state_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-google-photos-done-repeat")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-google-photos-done-repeat")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 997}}).encode("utf-8")

    class _Account:
        def __init__(self):
            self.token_status = "active"
            self.binding = type("Binding", (), {"status": "enabled"})()
            self.granted_scopes = [channels_route.google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER]
            self.google_email = "person@example.test"

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", lambda request, timeout=30: _FakeResponse())
    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [_Account()])
    counter = {"n": 0}

    def _build_start(**kwargs):
        counter["n"] += 1
        return type(
            "Packet",
            (),
            {"auth_url": f"https://accounts.google.com/o/oauth2/v2/auth?scope_bundle=full_workspace_photos&nonce={counter['n']}"},
        )()

    monkeypatch.setattr(
        channels_route.google_oauth_service,
        "build_google_oauth_start",
        _build_start,
    )
    client = _client(principal_id="", operator=False)
    product_service = channels_route.build_product_service(client.app.state.container)

    def _boom(**kwargs):
        raise RuntimeError("google_photos_forbidden")

    monkeypatch.setattr(product_service, "create_google_photos_picker_session", _boom)
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: product_service)

    for message_id in (991009, 991010, 991011):
        response = client.post(
            "/v1/channels/telegram/ingest",
            json={
                "message": {
                    "message_id": message_id,
                    "date": message_id,
                    "text": "Done",
                    "chat": {"id": 1354554303, "type": "private"},
                }
            },
            headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        )
        assert response.status_code == 200

    first_body = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991012,
                "date": 991012,
                "text": "You should have access to my Google photos. Can you find me the picture where Noah is sleeping on a mattress?",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    ).json()
    assert first_body["reply_sent"] is True

    repeat_one = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991013,
                "date": 991013,
                "text": "Done",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    ).json()
    assert repeat_one["reply_sent"] is True

    repeat_two = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991014,
                "date": 991014,
                "text": "Done",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    ).json()
    assert repeat_two["reply_sent"] is False
    assert repeat_two["reply_text"] == ""


def test_telegram_ingest_suppresses_repeated_again_when_google_photos_state_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-google-photos-again-repeat")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-google-photos-again-repeat")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 998}}).encode("utf-8")

    class _Account:
        def __init__(self):
            self.token_status = "active"
            self.binding = type("Binding", (), {"status": "enabled"})()
            self.granted_scopes = [channels_route.google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER]
            self.google_email = "person@example.test"

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", lambda request, timeout=30: _FakeResponse())
    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [_Account()])
    counter = {"n": 0}

    def _build_start(**kwargs):
        counter["n"] += 1
        return type(
            "Packet",
            (),
            {"auth_url": f"https://accounts.google.com/o/oauth2/v2/auth?scope_bundle=full_workspace_photos&nonce={counter['n']}"},
        )()

    monkeypatch.setattr(channels_route.google_oauth_service, "build_google_oauth_start", _build_start)
    client = _client(principal_id="", operator=False)
    product_service = channels_route.build_product_service(client.app.state.container)

    def _boom(**kwargs):
        raise RuntimeError("google_photos_forbidden")

    monkeypatch.setattr(product_service, "create_google_photos_picker_session", _boom)
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: product_service)

    first_body = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991015,
                "date": 991015,
                "text": "You should have access to my Google photos. Can you find me the picture where Noah is sleeping on a mattress?",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    ).json()
    assert first_body["reply_sent"] is True

    done_body = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991016,
                "date": 991016,
                "text": "Done",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    ).json()
    assert done_body["reply_sent"] is True

    again_one = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991017,
                "date": 991016,
                "text": "Again?",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    ).json()
    assert again_one["reply_sent"] is False
    assert again_one["reply_text"] == ""


def test_telegram_ingest_reuses_google_photos_context_for_voice_message_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-google-photos-voice")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-google-photos-voice")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 999}}).encode("utf-8")

    class _Account:
        def __init__(self):
            self.token_status = "active"
            self.binding = type("Binding", (), {"status": "enabled"})()
            self.granted_scopes = [channels_route.google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER]
            self.google_email = "person@example.test"

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", lambda request, timeout=30: _FakeResponse())
    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [_Account()])
    client = _client(principal_id="", operator=False)
    product_service = channels_route.build_product_service(client.app.state.container)
    monkeypatch.setattr(
        product_service,
        "create_google_photos_picker_session",
        lambda **kwargs: {"picker_uri": "https://photos.app/picker/session-voice/autoclose"},
    )
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: product_service)

    first = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991018,
                "date": 991018,
                "text": "You should have access to my Google photos. Can you find me the picture where Noah is sleeping on a mattress?",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert first.status_code == 200

    second = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991019,
                "date": 991019,
                "text": "Voice Message",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert second.status_code == 200
    body = second.json()
    assert body["reply_sent"] is True
    assert "Google Photos Picker is ready" in body["reply_text"]
    assert "https://photos.app/picker/session-voice/autoclose" in body["reply_text"]


def test_telegram_ingest_answers_start_picker_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-google-photos-picker")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-google-photos-picker")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 995}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append(payload)
        return _FakeResponse()

    class _Account:
        def __init__(self):
            self.token_status = "active"
            self.binding = type("Binding", (), {"status": "enabled"})()
            self.granted_scopes = [channels_route.google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER]
            self.google_email = "person@example.test"

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [_Account()])
    client = _client(principal_id="", operator=False)
    product_service = channels_route.build_product_service(client.app.state.container)
    monkeypatch.setattr(
        product_service,
        "create_google_photos_picker_session",
        lambda **kwargs: {"picker_uri": "https://photos.app/picker/session-456/autoclose"},
    )
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: product_service)

    response = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991007,
                "date": 125,
                "text": "start photo picker",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply_sent"] is True
    assert "Google Photos Picker is ready" in body["reply_text"]
    assert "https://photos.app/picker/session-456/autoclose" in body["reply_text"]
    assert '<a href="https://photos.app/picker/session-456/autoclose">Autoclose</a>' in sent[-1]["text"]


def test_telegram_ingest_surfaces_google_photos_picker_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-google-photos-forbidden")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-google-photos-forbidden")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 996}}).encode("utf-8")

    class _Account:
        def __init__(self):
            self.token_status = "active"
            self.binding = type("Binding", (), {"status": "enabled"})()
            self.granted_scopes = [channels_route.google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER]
            self.google_email = "person@example.test"

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", lambda request, timeout=30: _FakeResponse())
    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [_Account()])
    monkeypatch.setattr(
        channels_route.google_oauth_service,
        "build_google_oauth_start",
        lambda **kwargs: type("Packet", (), {"auth_url": "https://accounts.google.com/o/oauth2/v2/auth?scope_bundle=full_workspace_photos"})(),
    )
    client = _client(principal_id="", operator=False)
    product_service = channels_route.build_product_service(client.app.state.container)

    def _boom(**kwargs):
        raise RuntimeError("google_photos_forbidden")

    monkeypatch.setattr(product_service, "create_google_photos_picker_session", _boom)
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: product_service)

    response = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991008,
                "date": 126,
                "text": "start photo picker",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply_sent"] is True
    assert "Google is still refusing picker sessions for this app with a 403" in body["reply_text"]


def test_telegram_ingest_surfaces_google_photos_picker_service_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES", "1")
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-google-photos-service-disabled")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-google-photos-service-disabled")
    from app.api.routes import channels as channels_route

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 999}}).encode("utf-8")

    class _Account:
        def __init__(self):
            self.token_status = "active"
            self.binding = type("Binding", (), {"status": "enabled"})()
            self.granted_scopes = [channels_route.google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER]
            self.google_email = "person@example.test"

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", lambda request, timeout=30: _FakeResponse())
    monkeypatch.setattr(channels_route.google_oauth_service, "list_google_accounts", lambda **kwargs: [_Account()])
    client = _client(principal_id="", operator=False)
    product_service = channels_route.build_product_service(client.app.state.container)

    def _boom(**kwargs):
        raise RuntimeError(
            "google_photos_service_disabled:https://console.developers.google.com/apis/api/photospicker.googleapis.com/overview?project=357214671780"
        )

    monkeypatch.setattr(product_service, "create_google_photos_picker_session", _boom)
    monkeypatch.setattr(channels_route, "build_product_service", lambda container: product_service)

    response = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991018,
                "date": 127,
                "text": "start photo picker",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply_sent"] is True
    assert "Google Photos Picker API is disabled" in body["reply_text"]
    assert "https://console.developers.google.com/apis/api/photospicker.googleapis.com/overview?project=357214671780" in body["reply_text"]


def test_telegram_ingest_answers_meta_assistant_prompt_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-meta")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-meta")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 993}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append(payload)
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)
    response = client.post(
        "/v1/channels/telegram/ingest",
        json={
            "message": {
                "message_id": 991004,
                "date": 123,
                "text": "I want you to finally work.",
                "chat": {"id": 1354554303, "type": "private"},
            }
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply_sent"] is True
    assert body["reply_text"] == "I'm here. Give me a concrete task."
    assert sent[-1]["text"] == "I'm here. Give me a concrete task."
    observations = list(client.app.state.container.channel_runtime.list_recent_observations(limit=12, principal_id="exec-telegram-meta"))
    assert any(str(row.event_type) == "telegram.reply_sent" for row in observations)
    assert not any(str(row.event_type) == "telegram.reply_async_started" for row in observations)
    payload = next(dict(row.payload or {}) for row in observations if str(row.event_type) == "telegram.reply_sent")
    assert dict(payload.get("active_object_map") or {}) == {}
    assert dict(payload.get("intent_state") or {}) == {}
    assert dict(payload.get("comparison_state") or {}) == {}


def test_telegram_ingest_schedules_async_codex_reply_for_generic_plain_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-real-chat")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-real-chat")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 22}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append(payload)
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(channels_route, "_telegram_real_ea_reply_text", lambda **kwargs: "Here is the real EA answer.")
    client = _client(principal_id="", operator=False)

    resp = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json={
            "update": {
                "message": {
                    "chat": {"id": 9798},
                    "text": "Tell me something useful.",
                    "message_id": 22,
                    "date": 123,
                }
            }
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["reply_sent"] is False
    assert body["reply_text"] == ""
    observations = list(client.app.state.container.channel_runtime.list_recent_observations(limit=12, principal_id="exec-telegram-real-chat"))
    assert any(str(row.event_type) == "telegram.reply_async_started" for row in observations)


def test_telegram_ingest_updates_property_alert_policy_from_plain_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "telegram-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-policy")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-policy")
    from app.api.routes import channels as channels_route

    client = _client(principal_id="", operator=False)
    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 401}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        payload = json.loads(request.data.decode("utf-8"))
        sent.append({"url": request.full_url, "payload": payload, "timeout": timeout})
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)

    response = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
        json={
            "update": {
                "message": {
                    "message_id": 401,
                    "chat": {"id": "telegram-policy-chat"},
                    "text": "I want EA to do all of that by itself. If it's good, I want a notification here.",
                    "date": 123,
                }
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reply_sent"] is True
    assert "score and rank property alerts automatically" in body["reply_text"]
    assert sent and "only notify you here when the fit looks genuinely good" in str(sent[0]["payload"]["text"])


def test_telegram_local_assistant_can_answer_capability_question() -> None:
    from app.api.routes import channels as channels_route

    reply = channels_route._telegram_local_assistant_reply_text(
        _client(principal_id="exec-telegram-capabilities", operator=False).app.state.container,
        principal_id="exec-telegram-capabilities",
        text="What can you do?",
    )
    assert "schedule" in reply.lower()
    assert "property" in reply.lower()


def test_telegram_ingest_falls_back_when_real_ea_reply_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-timeout")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-timeout")
    monkeypatch.setenv("EA_TELEGRAM_RESPONSES_TIMEOUT_SECONDS", "1")
    from app.api.routes import channels as channels_route
    import time

    def _slow_run_response(*args, **kwargs):
        time.sleep(3.0)
        raise RuntimeError("should_have_timed_out_first")

    monkeypatch.setattr(channels_route.responses_route, "_generate_upstream_text", _slow_run_response)
    container = _client(principal_id="exec-telegram-timeout", operator=False).app.state.container
    started = time.monotonic()
    reply = channels_route._telegram_real_ea_reply_text(
        container=container,
        principal_id="exec-telegram-timeout",
        text="Tell me something slow",
        current_message_id="22",
    )
    elapsed = time.monotonic() - started
    assert reply == ""
    assert elapsed < 2.5


def test_telegram_ingest_duplicate_update_does_not_send_duplicate_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-dup")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-dup")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 5}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)
    payload = {
        "update_id": 50,
        "message": {
            "chat": {"id": 9292},
            "text": "2+2=?",
            "message_id": 16,
            "date": 123,
        },
    }
    first = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json=payload,
    )
    second = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json=payload,
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["reply_sent"] is True
    assert second.json()["reply_sent"] is False
    assert len(sent) == 1


def test_telegram_ingest_duplicate_update_retries_reply_after_transient_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_TELEGRAM_INGEST_SECRET", "tg-secret")
    monkeypatch.setenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT", "1")
    monkeypatch.setenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID", "exec-telegram-retry")
    monkeypatch.setenv("EA_TELEGRAM_BOT_HANDLE", "tibor_concierge_bot")
    monkeypatch.setenv("EA_TELEGRAM_BOT_TOKEN", "telegram-token-retry")
    from app.api.routes import channels as channels_route

    sent: list[dict[str, object]] = []
    call_count = {"value": 0}

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self) -> bytes:
            return json.dumps({"ok": True, "result": {"message_id": 6}}).encode("utf-8")

    def _fake_urlopen(request, timeout=30):
        if not request.full_url.startswith("https://api.telegram.org/"):
            raise channels_route.urllib.error.URLError(
                "non-Telegram transport disabled by this test"
            )
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError("transient_telegram_send_failure")
        sent.append(json.loads(request.data.decode("utf-8")))
        return _FakeResponse()

    monkeypatch.setattr(channels_route.urllib.request, "urlopen", _fake_urlopen)
    client = _client(principal_id="", operator=False)
    payload = {
        "update_id": 51,
        "message": {
            "chat": {"id": 9393},
            "text": "2+2=?",
            "message_id": 17,
            "date": 123,
        },
    }
    first = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json=payload,
    )
    second = client.post(
        "/v1/channels/telegram/ingest",
        headers={"X-Telegram-Bot-Api-Secret-Token": "tg-secret"},
        json=payload,
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["reply_sent"] is False
    assert second.json()["reply_sent"] is True
    assert len(sent) == 1


def test_browser_landing_exposes_google_onboarding_and_html_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/v1/providers/google/oauth/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")

    owner = _client(principal_id="exec-browser")

    landing = owner.get("/")
    assert landing.status_code == 200
    _assert_no_product_drift(landing.text)
    assert "Search once. See the right homes. Decide faster." in landing.text
    assert "Signing you in" in landing.text
    assert "Create account" not in landing.text
    assert "PropertyQuarry" in landing.text
    for href in _internal_links(landing.text):
        resolved = owner.get(href, follow_redirects=False)
        assert resolved.status_code in {200, 303, 307}, href

    # The link crawl above deliberately visits localized public URLs and may
    # update the client's locale cookie. Pin English for this English-copy
    # identity contract instead of depending on traversal order.
    setup = owner.get("/register?lang=en")
    assert setup.status_code == 200
    _assert_no_product_drift(setup.text)
    assert "Set up your property search." in setup.text
    assert "Account details" in setup.text
    assert "Google sign-in" in setup.text

    sign_in = owner.get("/sign-in")
    assert sign_in.status_code == 200
    _assert_no_product_drift(sign_in.text)
    assert "Use a secure email link if your address already has access." in sign_in.text
    assert "You can also continue with an available provider." in sign_in.text
    assert "Identity only" not in sign_in.text
    assert 'href="/app/search"' in sign_in.text
    assert 'action="/app/actions/sign-out"' in sign_in.text
    assert "Email me a fresh access link" not in sign_in.text
    assert "Open search" in sign_in.text

    legacy_setup = owner.get("/setup", follow_redirects=False)
    assert legacy_setup.status_code == 307
    assert legacy_setup.headers["location"] == "/register"

    privacy = owner.get("/security")
    assert privacy.status_code == 200
    _assert_no_product_drift(privacy.text)
    assert "Fit guide" in privacy.text
    assert "Open fit guide" in privacy.text
    assert "Must-haves decide what belongs. Preferences shape the order." in privacy.text

    for path in ("/product", "/integrations", "/pricing", "/docs"):
        page = owner.get(path)
        assert page.status_code == 200
        _assert_no_product_drift(page.text)

    privacy = owner.get("/privacy", follow_redirects=False)
    assert privacy.status_code == 200
    _assert_no_product_drift(privacy.text)
    assert "Data protection" in privacy.text
    assert "Public tours should use a narrow public manifest" in privacy.text

    started = owner.post(
        "/google/connect",
        data={"scope_bundle": "identity", "api_token": ""},
        follow_redirects=False,
    )
    assert started.status_code == 303
    location = started.headers["location"]
    assert "https://accounts.google.com/o/oauth2/v2/auth" in location
    parsed = urllib.parse.urlparse(location)
    query = urllib.parse.parse_qs(parsed.query)
    state = query["state"][0]
    assert query["redirect_uri"][0] == "https://ea.example/google/callback"

    from app.services import google_oauth as google_service

    monkeypatch.setattr(
        google_service,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_service,
        "_refresh_google_access_token",
        lambda **kwargs: {
            "access_token": "fresh-access-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(google_service, "_gmail_messages_payload", lambda **kwargs: {})
    monkeypatch.setattr(google_service, "_list_recent_calendar_signals", lambda **kwargs: [])
    monkeypatch.setattr(
        google_service,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-browser",
            "email": "person@example.test",
            "hd": "gmail.example",
        },
    )

    callback = owner.get("/google/callback", params={"code": "code-123", "state": state})
    assert callback.status_code == 200
    assert "Google account linked." in callback.text
    assert "person@example.test" in callback.text
    assert "openid" in callback.text
    assert 'href="/sign-in?signing_in=1"' in callback.text
    assert "No Gmail or Calendar sync was requested." in callback.text


def test_browser_landing_uses_cloudflare_access_identity_for_gmail_onboarding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/v1/providers/google/oauth/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")
    monkeypatch.setenv("EA_CF_ACCESS_TEAM_DOMAIN", "girschele.cloudflareaccess.com")
    monkeypatch.setenv("EA_CF_ACCESS_AUD", "aud-123")

    from app.api import dependencies as deps
    from app.services.cloudflare_access import CloudflareAccessIdentity

    monkeypatch.setattr(
        deps,
        "resolve_access_identity",
        lambda **kwargs: CloudflareAccessIdentity(
            principal_id="cf-email:person@example.test",
            email="person@example.test",
            subject="subject-browser",
            display_name="Browser Gmail",
            issuer="https://girschele.cloudflareaccess.com",
            idp_name="google",
            audiences=("aud-123",),
            claims={"email": "person@example.test", "sub": "subject-browser"},
        ),
    )

    owner = _client(principal_id="ignored-browser")

    landing = owner.get("/", follow_redirects=False)
    assert landing.status_code == 307
    assert landing.headers["location"] == "/app/search"

    started = owner.post(
        "/google/connect",
        data={"scope_bundle": "identity"},
        follow_redirects=False,
    )
    assert started.status_code == 303
    parsed = urllib.parse.urlparse(started.headers["location"])
    query = urllib.parse.parse_qs(parsed.query)
    assert query["redirect_uri"][0] == "https://ea.example/google/callback"
    state = query["state"][0]

    from app.services import google_oauth as google_service

    monkeypatch.setattr(
        google_service,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "scope": "openid email profile https://www.googleapis.com/auth/gmail.send",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_service,
        "_refresh_google_access_token",
        lambda **kwargs: {
            "access_token": "fresh-access-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(google_service, "_gmail_messages_payload", lambda **kwargs: {})
    monkeypatch.setattr(google_service, "_list_recent_calendar_signals", lambda **kwargs: [])
    monkeypatch.setattr(
        google_service,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-browser",
            "email": "person@example.test",
            "hd": "gmail.com",
        },
    )

    callback = owner.get("/google/callback", params={"code": "code-123", "state": state})
    assert callback.status_code == 200
    assert "Google account linked." in callback.text
    assert "person@example.test" in callback.text
    assert "cf-email:person@example.test" not in callback.text
    assert 'href="/sign-in?signing_in=1"' in callback.text
    assert "No Gmail or Calendar sync was requested." in callback.text


def test_browser_google_callback_redirects_back_to_setup_when_state_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/google/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")

    owner = _client(principal_id="exec-browser-expired")
    started = owner.post(
        "/google/connect",
        data={"scope_bundle": "identity"},
        follow_redirects=False,
    )
    assert started.status_code == 303
    parsed = urllib.parse.urlparse(started.headers["location"])
    query = urllib.parse.parse_qs(parsed.query)
    state = query["state"][0]

    from app.services import google_oauth as google_service

    payload = google_service.read_google_oauth_state_unchecked(state)
    issued_at = int(payload["issued_at"])
    monkeypatch.setattr(google_service.time, "time", lambda: issued_at + 21601)

    callback = owner.get(
        "/google/callback",
        params={"code": "expired-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/sign-in?signing_in=1&")
    assert "google_error=google_oauth_state_expired" in callback.headers["location"]


def test_browser_google_callback_restarts_sign_in_for_stale_redirect_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://propertyquarry.com/google/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")

    from app.services import google_oauth as google_service

    owner = _client(principal_id="exec-browser-stale-redirect")
    state = google_service._encode_signed_state(
        {
            "browser_source": "sign_in",
            "issued_at": int(google_service.time.time()),
            "nonce": "stale-redirect",
            "principal_id": "",
            "redirect_uri": "https://old.propertyquarry.example/google/callback",
            "return_to": "/sign-in?google_connected=1",
            "scope_bundle": "identity",
        },
        secret="google-state-secret",
    )

    callback = owner.get(
        "/google/callback",
        params={"code": "code-from-stale-flow", "state": state},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert (
        callback.headers["location"]
        == "/sign-in/google?restart=stale_redirect&return_to=%2Fsign-in%3Fgoogle_connected%3D1"
    )


def test_browser_google_callback_renders_error_page_for_google_error_params(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/v1/providers/google/oauth/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")

    owner = _client(principal_id="exec-browser-google-error")
    callback = owner.get(
        "/google/callback",
        params={"error": "access_denied", "error_description": "User denied the request"},
    )
    assert callback.status_code == 400
    assert "Google connection needs attention" in callback.text
    assert "User denied the request" in callback.text
    assert "instead of a blank gateway error" in callback.text


def test_browser_google_callback_renders_failure_page_for_unexpected_callback_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "google-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "google-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://ea.example/v1/providers/google/oauth/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "provider-secret-key")

    owner = _client(principal_id="exec-browser-google-failure")

    started = owner.post(
        "/google/connect",
        data={"scope_bundle": "core"},
        follow_redirects=False,
    )
    assert started.status_code == 303
    parsed = urllib.parse.urlparse(started.headers["location"])
    query = urllib.parse.parse_qs(parsed.query)
    state = query["state"][0]

    from app.api.routes import landing_setup as landing_setup_route

    def _boom(*args, **kwargs):
        raise ValueError("google_token_exchange_boom")

    monkeypatch.setattr(landing_setup_route, "complete_google_oauth_callback", _boom)

    callback = owner.get("/google/callback", params={"code": "code-123", "state": state})
    assert callback.status_code == 502
    assert "Google connection needs attention" in callback.text
    assert "google_token_exchange_boom" in callback.text


def test_browser_shell_routes_and_nav_links_resolve() -> None:
    user = _client(principal_id="exec-browser-shell")
    operator = _client(principal_id="operator-browser-shell", operator=True)

    for path in (
        "/app/today",
        "/app/queue",
        "/app/commitments",
        "/app/people",
        "/app/evidence",
        "/app/settings",
    ):
        page = user.get(path)
        assert page.status_code == 200
        _assert_no_product_drift(page.text)
        for href in _internal_links(page.text):
            resolved = user.get(href, follow_redirects=False)
            assert resolved.status_code in {200, 303, 307}, (path, href)

    for path, target in (
        ("/app/briefing", "/app/queue"),
        ("/app/inbox", "/app/queue"),
        ("/app/follow-ups", "/app/commitments"),
        ("/app/memory", "/app/people"),
        ("/app/contacts", "/app/evidence"),
        ("/app/activity", "/app/account"),
        ("/app/channels", "/app/account?billing=1#delivery"),
        ("/app/automations", "/app/agents"),
    ):
        page = user.get(path, follow_redirects=False)
        assert page.status_code == 307
        assert page.headers["location"] == target

    for path in (
        "/admin/office",
        "/admin/policies",
        "/admin/providers",
        "/admin/audit-trail",
        "/admin/operators",
        "/admin/community",
        "/admin/api",
    ):
        page = operator.get(path)
        assert page.status_code == 200
        _assert_no_product_drift(page.text)
        for href in _internal_links(page.text):
            resolved = operator.get(href, follow_redirects=False)
            assert resolved.status_code in {200, 303, 307}, (path, href)


def test_provider_bindings_reject_cross_principal_query_scope() -> None:
    owner = _client(principal_id="exec-1", operator=True)
    response = owner.get("/v1/providers/bindings?principal_id=exec-2")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "principal_scope_mismatch"


def test_onemin_probe_all_endpoint_returns_slot_results(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("ONEMIN_AI_API_KEY_FALLBACK_"):
            monkeypatch.delenv(key, raising=False)
    for key in (
        "EA_RESPONSES_ONEMIN_ACTIVE_SLOTS",
        "EA_RESPONSES_ONEMIN_RESERVE_SLOTS",
        "ONEMIN_DIRECT_API_KEYS_JSON",
        "ONEMIN_DIRECT_API_KEYS_JSON_FILE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ONEMIN_AI_API_KEY", "probe-primary")
    monkeypatch.setenv("ONEMIN_AI_API_KEY_FALLBACK_1", "probe-deleted")
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {
                        "secret_sha256": hashlib.sha256(b"probe-primary").hexdigest(),
                        "owner_email": "probe@example.com",
                    }
                ]
            }
        ),
    )
    owner = _client(principal_id="exec-1", operator=True)
    from app.services import responses_upstream as upstream

    upstream._test_reset_onemin_states()

    def fake_post_json(*, url: str, headers: dict[str, str], payload: dict[str, object], timeout_seconds: int) -> tuple[int, dict[str, object]]:
        if headers["API-KEY"] == "probe-primary":
            return (
                200,
                {
                    "aiRecord": {
                        "model": "gpt-4.1",
                        "aiRecordDetail": {"resultObject": "OK"},
                    }
                },
            )
        return (401, {"errorCode": "HTTP_EXCEPTION", "message": "API Key has been deleted"})

    monkeypatch.setattr(upstream, "_post_json", fake_post_json)

    response = owner.post(
        "/v1/providers/onemin/probe-all",
        json={
            "include_reserve": True,
            "account_labels": ["ONEMIN_AI_API_KEY", "ONEMIN_AI_API_KEY_FALLBACK_1"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider_key"] == "onemin"
    assert body["result_counts"] == {"ok": 1, "revoked": 1}
    primary = next(slot for slot in body["slots"] if slot["account_name"] == "ONEMIN_AI_API_KEY")
    assert primary["owner_email"] == "probe@example.com"
    assert primary["result"] == "ok"


def test_onemin_billing_refresh_executes_browseract_tools_and_maps_owner_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "owner_email": "owner@example.com",
                    }
                ]
            }
        ),
    )

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "owner@example.com",
            "scope_json": {"scopes": ["billing", "inventory"]},
            "auth_metadata_json": {
                "onemin_billing_usage_run_url": "https://browseract.example/run/billing",
                "onemin_members_run_url": "https://browseract.example/run/members",
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200
    binding_id = created.json()["binding_id"]

    container = owner.app.state.container
    container.tool_execution._browseract_onemin_billing_usage = lambda **_: {
        "remaining_credits": "12345",
        "max_credits": "20000",
        "next_topup_at": "2026-03-31T00:00:00Z",
        "topup_amount": "20000",
        "used_percent": "38.3",
    }
    container.tool_execution._browseract_onemin_member_reconciliation = lambda **_: {
        "members": [
            {
                "email": "owner@example.com",
                "status": "active",
                "credit_limit": "5000",
            }
        ]
    }

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": True, "include_provider_api": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider_key"] == "onemin"
    assert body["connector_binding_count"] == 1
    assert body["billing_refresh_count"] == 1
    assert body["member_reconciliation_count"] == 1
    assert body["errors"] == []
    assert body["billing_results"][0]["binding_id"] == binding_id
    assert body["billing_results"][0]["account_label"] == "ONEMIN_AI_API_KEY"
    assert body["billing_results"][0]["next_topup_at"] == "2026-03-31T00:00:00Z"
    assert body["member_results"][0]["account_label"] == "ONEMIN_AI_API_KEY"
    assert body["member_results"][0]["matched_owner_slots"] == 1


def test_onemin_billing_refresh_forwards_default_browser_proxy_settings_to_browseract_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_SERVER", "http://ea-fastestvpn-proxy:3128")
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_USERNAME", "vpn-user")
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_PASSWORD", "vpn-pass")
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_BYPASS", "localhost,127.0.0.1,ea-api")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": ["ONEMIN_AI_API_KEY"],
                "onemin_billing_usage_run_url": "https://browseract.example/run/billing",
                "onemin_members_run_url": "https://browseract.example/run/members",
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))

    observed: list[tuple[str, dict[str, object]]] = []

    def fake_invoke_browseract_tool(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "")
        payload_json = dict(kwargs.get("payload_json") or {})
        observed.append((tool_name, payload_json))
        if tool_name == "browseract.onemin_billing_usage":
            return {"refresh_backend": "browseract", "remaining_credits": "12345"}
        return {"refresh_backend": "browseract", "matched_owner_slots": 1}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": True, "include_provider_api": False},
    )
    assert response.status_code == 200
    assert [tool_name for tool_name, _payload in observed] == [
        "browseract.onemin_billing_usage",
        "browseract.onemin_member_reconciliation",
    ]
    for _tool_name, payload_json in observed:
        assert payload_json["browser_proxy_server"] == "http://ea-fastestvpn-proxy:3128"
        assert payload_json["browser_proxy_username"] == "vpn-user"
        assert payload_json["browser_proxy_password"] == "vpn-pass"
        assert payload_json["browser_proxy_bypass"] == "localhost,127.0.0.1,ea-api"


def test_onemin_billing_refresh_chooses_browser_proxy_from_pool_per_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    proxy_pool = (
        "http://ea-fastestvpn-proxy:3128",
        "http://ea-fastestvpn-proxy-ie:3128",
        "http://ea-fastestvpn-proxy-nl:3128",
    )
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_SERVER", proxy_pool[0])
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_POOL", ",".join(proxy_pool))

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": ["ONEMIN_AI_API_KEY", "ONEMIN_AI_API_KEY_FALLBACK_1"],
                "onemin_billing_usage_run_url": "https://browseract.example/run/billing",
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))

    observed: dict[str, str] = {}

    def fake_invoke_browseract_tool(**kwargs):
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        observed[account_label] = str(payload_json.get("browser_proxy_server") or "")
        return {"refresh_backend": "browseract", "remaining_credits": "12345"}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": False, "include_provider_api": False},
    )
    assert response.status_code == 200
    assert observed == {
        account_label: providers_route._proxy_url_for_subject(proxy_urls=proxy_pool, subject=account_label)
        for account_label in ("ONEMIN_AI_API_KEY", "ONEMIN_AI_API_KEY_FALLBACK_1")
    }


def test_onemin_billing_refresh_provisions_fastestvpn_services_on_demand(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    isolated_ltd_path = tmp_path / "LTDs.md"
    monkeypatch.setenv("EA_LTD_MARKDOWN_PATH", str(isolated_ltd_path))
    monkeypatch.setenv(
        "EA_RESPONSES_PROVIDER_LEDGER_DIR",
        str(tmp_path / "provider-ledger"),
    )
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_SERVER", "http://ea-fastestvpn-proxy:3128")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": ["ONEMIN_AI_API_KEY"],
                "onemin_billing_usage_run_url": "https://browseract.example/run/billing",
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))
    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", lambda **_: {"refresh_backend": "browseract", "remaining_credits": "12345"})

    ensured: list[tuple[tuple[str, ...], str]] = []
    stopped: list[tuple[tuple[str, ...], str]] = []

    monkeypatch.setattr(
        providers_route,
        "_ensure_fastestvpn_services",
        lambda *, service_names, reason: ensured.append((tuple(service_names), reason)) or {
            "reason": reason,
            "service_names": list(service_names),
            "started_services": list(service_names),
            "already_running_services": [],
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        providers_route,
        "_stop_fastestvpn_services",
        lambda *, service_names, reason: stopped.append((tuple(service_names), reason)) or {
            "reason": reason,
            "service_names": list(service_names),
            "returncode": 0,
            "stdout": "",
            "stderr": "",
        },
    )

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": False, "include_provider_api": False},
    )
    assert response.status_code == 200
    assert ensured == [(("ea-fastestvpn-proxy",), "onemin.browseract.refresh")]
    assert stopped == [(("ea-fastestvpn-proxy",), "onemin.browseract.refresh:cleanup")]
    assert not isolated_ltd_path.exists()


def test_fastestvpn_service_provision_uses_no_build_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route

    root = Path("/tmp/ea-fastestvpn-compose")
    root.mkdir(parents=True, exist_ok=True)
    (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (root / "docker-compose.fastestvpn.yml").write_text("services: {}\n", encoding="utf-8")

    monkeypatch.setenv("EA_FASTESTVPN_ON_DEMAND_ENABLED", "1")
    monkeypatch.setenv("EA_FASTESTVPN_COMPOSE_ROOT", str(root))
    monkeypatch.setattr(providers_route, "_fastestvpn_service_state", lambda _service_name: "unhealthy")
    monkeypatch.setattr(providers_route, "_wait_for_fastestvpn_services", lambda service_names, timeout_seconds: None)
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = list(command)
        observed["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(providers_route.subprocess, "run", fake_run)

    result = providers_route._ensure_fastestvpn_services(
        service_names=("ea-fastestvpn-proxy",),
        reason="provider_scrape.unit_test",
    )

    assert result["returncode"] == 0
    assert observed["cwd"] == str(root)
    assert observed["command"][-5:] == [
        "up",
        "-d",
        "--no-build",
        "--no-deps",
        "ea-fastestvpn-proxy",
    ]
    assert observed["command"][:5] == [
        "docker-compose",
        "-f",
        str(root / "docker-compose.yml"),
        "-f",
        str(root / "docker-compose.fastestvpn.yml"),
    ] or observed["command"][:6] == [
        "docker",
        "compose",
        "-f",
        str(root / "docker-compose.yml"),
        "-f",
        str(root / "docker-compose.fastestvpn.yml"),
    ]


def test_onemin_billing_refresh_prefers_binding_browser_proxy_settings_over_env_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_SERVER", "http://ea-fastestvpn-proxy:3128")
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_USERNAME", "vpn-user")
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_PASSWORD", "vpn-pass")
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_BYPASS", "localhost,127.0.0.1,ea-api")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": ["ONEMIN_AI_API_KEY"],
                "onemin_billing_usage_run_url": "https://browseract.example/run/billing",
                "proxy_server": "http://binding-proxy:8080",
                "browser_proxy_username": "binding-user",
                "browser_proxy_password": "binding-pass",
                "proxy_bypass": "localhost,internal.service",
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))

    observed: dict[str, object] = {}

    def fake_invoke_browseract_tool(**kwargs):
        payload_json = dict(kwargs.get("payload_json") or {})
        observed.update(payload_json)
        return {"refresh_backend": "browseract", "remaining_credits": "12345"}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": False, "include_provider_api": False},
    )
    assert response.status_code == 200
    assert observed["browser_proxy_server"] == "http://binding-proxy:8080"
    assert observed["browser_proxy_username"] == "binding-user"
    assert observed["browser_proxy_password"] == "binding-pass"
    assert observed["browser_proxy_bypass"] == "localhost,internal.service"


def test_onemin_billing_refresh_rotates_fastestvpn_proxy_and_retries_browseract_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv("EA_UI_BROWSER_PROXY_SERVER", "http://ea-fastestvpn-proxy:3128")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": ["ONEMIN_AI_API_KEY"],
                "onemin_billing_usage_run_url": "https://browseract.example/run/billing",
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))

    calls = 0
    rotations: list[str] = []

    def fake_invoke_browseract_tool(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise providers_route.ToolExecutionError("ui_service_worker_failed:onemin_billing_usage:auth_request_failed")
        return {"refresh_backend": "browseract", "remaining_credits": "12345"}

    def fake_rotate_fastestvpn_proxy(*, reason: str):
        rotations.append(reason)
        return {
            "reason": reason,
            "returncode": 0,
            "duration_seconds": 0.25,
            "stdout": "rotated",
            "stderr": "",
        }

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_rotate_fastestvpn_proxy", fake_rotate_fastestvpn_proxy)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": False, "include_provider_api": False},
    )
    assert response.status_code == 200
    body = response.json()
    assert calls == 2
    assert len(rotations) == 1
    assert body["billing_refresh_count"] == 1
    assert body["errors"] == []
    assert body["browseract_proxy_rotation_count"] == 1
    assert body["browseract_proxy_recovered_labels"] == ["ONEMIN_AI_API_KEY"]
    assert "FastestVPN proxy rotated 1 time" in body["note"]


def test_onemin_billing_refresh_uses_direct_api_when_no_browseract_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "owner_email": "owner@example.com",
                    }
                ]
            }
        ),
    )

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(
        providers_route,
        "_refresh_onemin_via_provider_api",
        lambda **_: (
            [
                {
                    "refresh_backend": "onemin_api",
                    "account_label": "ONEMIN_AI_API_KEY",
                    "owner_email": "owner@example.com",
                    "next_topup_at": "2026-03-19T22:00:00Z",
                    "topup_amount": 15000.0,
                    "basis": "actual_provider_api",
                }
            ],
            [
                {
                    "refresh_backend": "onemin_api",
                    "account_label": "ONEMIN_AI_API_KEY",
                    "owner_email": "owner@example.com",
                    "matched_owner_slots": 1,
                    "basis": "actual_provider_api",
                }
            ],
            [],
            1,
            0,
            False,
        ),
    )

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": True, "provider_api_all_accounts": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider_key"] == "onemin"
    assert body["connector_binding_count"] == 0
    assert body["api_account_count"] == 1
    assert body["billing_refresh_count"] == 1
    assert body["member_reconciliation_count"] == 1
    assert body["api_billing_refresh_count"] == 1
    assert body["api_member_reconciliation_count"] == 1
    assert body["billing_results"][0]["refresh_backend"] == "onemin_api"
    assert body["member_results"][0]["refresh_backend"] == "onemin_api"
    assert "direct 1min API" in body["note"]


def test_onemin_billing_refresh_forwards_full_provider_api_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "owner_email": "owner@example.com",
                    }
                ]
            }
        ),
    )

    from app.api.routes import providers as providers_route

    observed: dict[str, object] = {}

    def fake_refresh(**kwargs):
        observed.update(kwargs)
        return ([], [], [], 1, 0, False)

    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={
            "provider_api_all_accounts": True,
            "provider_api_continue_on_rate_limit": True,
        },
    )
    assert response.status_code == 200
    assert observed["include_members"] is True
    assert observed["all_accounts"] is False
    assert observed["account_labels"] == {"ONEMIN_AI_API_KEY"}
    assert observed["continue_on_rate_limit"] is True


def test_onemin_billing_refresh_targeted_global_provider_api_bypasses_browseract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY_FALLBACK_68",
                        "owner_email": "owner@example.com",
                    }
                ]
            }
        ),
    )
    monkeypatch.setenv("BROWSERACT_PASSWORD", "slotpass")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": [
                    "ONEMIN_AI_API_KEY_FALLBACK_68",
                ]
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    invoked: list[str] = []
    observed: dict[str, object] = {}

    def fake_invoke_browseract_tool(**kwargs):
        invoked.append(str(kwargs.get("tool_name") or ""))
        return {"refresh_backend": "browseract", "remaining_credits": "12345"}

    def fake_refresh(**kwargs):
        observed.update(kwargs)
        return (
            [{"account_label": "ONEMIN_AI_API_KEY_FALLBACK_68", "refresh_backend": "onemin_api"}],
            [],
            [],
            1,
            0,
            False,
        )

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={
            "include_members": False,
            "provider_api_all_accounts": True,
            "account_labels": ["ONEMIN_AI_API_KEY_FALLBACK_68"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert invoked == []
    assert observed["all_accounts"] is False
    assert observed["account_labels"] == {"ONEMIN_AI_API_KEY_FALLBACK_68"}
    assert body["provider_api_target_labels"] == ["ONEMIN_AI_API_KEY_FALLBACK_68"]
    assert body["api_account_attempted"] == 1
    assert body["billing_results"][0]["refresh_backend"] == "onemin_api"


def test_onemin_billing_refresh_invalidates_provider_health_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "owner_email": "owner@example.com",
                    }
                ]
            }
        ),
    )

    from app.api.routes import providers as providers_route

    invalidations: list[object] = []
    remembered: list[tuple[bool, dict[str, object]]] = []

    monkeypatch.setattr(
        providers_route,
        "_refresh_onemin_via_provider_api",
        lambda **_kwargs: ([{"account_label": "ONEMIN_AI_API_KEY", "refresh_backend": "onemin_api"}], [], [], 1, 0, False),
    )
    monkeypatch.setattr(
        providers_route,
        "invalidate_provider_health_snapshot_cache",
        lambda lightweight=None: invalidations.append(lightweight),
    )
    monkeypatch.setattr(
        providers_route,
        "remember_provider_health_snapshot_cache",
        lambda *, lightweight, payload: remembered.append((lightweight, dict(payload))),
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "_provider_health_report",
        lambda lightweight=False: {
            "providers": {"onemin": {"state": "ready", "configured_slots": 1}},
            "provider_health_snapshot": {"lightweight": bool(lightweight)},
        },
    )

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": False, "provider_api_all_accounts": True},
    )
    assert response.status_code == 200
    assert invalidations == [None]
    assert remembered == [
        (False, {"providers": {"onemin": {"state": "ready", "configured_slots": 1}}, "provider_health_snapshot": {"lightweight": False}}),
        (True, {"providers": {"onemin": {"state": "ready", "configured_slots": 1}}, "provider_health_snapshot": {"lightweight": True}}),
    ]


def test_onemin_probe_all_invalidates_provider_health_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    from app.api.routes import providers as providers_route

    invalidations: list[object] = []
    remembered: list[tuple[bool, dict[str, object]]] = []

    monkeypatch.setattr(
        providers_route,
        "probe_all_onemin_slots",
        lambda **_kwargs: {"provider_key": "onemin", "slot_count": 1},
    )
    monkeypatch.setattr(
        providers_route,
        "invalidate_provider_health_snapshot_cache",
        lambda lightweight=None: invalidations.append(lightweight),
    )
    monkeypatch.setattr(
        providers_route,
        "remember_provider_health_snapshot_cache",
        lambda *, lightweight, payload: remembered.append((lightweight, dict(payload))),
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "_provider_health_report",
        lambda lightweight=False: {
            "providers": {"onemin": {"state": "ready", "configured_slots": 1}},
            "provider_health_snapshot": {"lightweight": bool(lightweight)},
        },
    )

    response = owner.post("/v1/providers/onemin/probe-all", json={"include_reserve": True})
    assert response.status_code == 200
    assert response.json()["provider_key"] == "onemin"
    assert invalidations == [None]
    assert remembered == [
        (True, {"providers": {"onemin": {"state": "ready", "configured_slots": 1}}, "provider_health_snapshot": {"lightweight": True}}),
    ]


def test_onemin_probe_all_forwards_requested_account_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    from app.api.routes import providers as providers_route

    observed: dict[str, object] = {}

    def fake_probe_all_onemin_slots(*, include_reserve: bool, account_labels: list[str] | None = None):
        observed["include_reserve"] = include_reserve
        observed["account_labels"] = list(account_labels or [])
        return {"provider_key": "onemin", "slot_count": len(account_labels or [])}

    monkeypatch.setattr(providers_route, "probe_all_onemin_slots", fake_probe_all_onemin_slots)
    monkeypatch.setattr(
        providers_route,
        "invalidate_provider_health_snapshot_cache",
        lambda lightweight=None: None,
    )
    monkeypatch.setattr(
        providers_route,
        "remember_provider_health_snapshot_cache",
        lambda *, lightweight, payload: None,
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "_provider_health_report",
        lambda lightweight=False: {"providers": {"onemin": {"state": "ready"}}},
    )

    response = owner.post(
        "/v1/providers/onemin/probe-all",
        json={"include_reserve": False, "account_labels": ["ACC_A", "ACC_B"]},
    )

    assert response.status_code == 200
    assert observed == {"include_reserve": False, "account_labels": ["ACC_A", "ACC_B"]}
    assert response.json()["slot_count"] == 2


def test_onemin_billing_refresh_is_throttled_to_one_run_per_minute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    from app.api.routes import providers as providers_route
    monkeypatch.setattr(
        providers_route.upstream,
        "onemin_owner_rows",
        lambda: (
            {
                "account_name": "ONEMIN_AI_API_KEY",
                "owner_email": "owner@example.com",
            },
        ),
    )

    begin_states = iter([(True, 0.0, ""), (False, 40.0, "cadence")])
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: next(begin_states))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)

    call_count = 0

    def fake_refresh(**_kwargs):
        nonlocal call_count
        call_count += 1
        return (
            [{"refresh_backend": "onemin_api", "account_label": "ONEMIN_AI_API_KEY"}],
            [{"refresh_backend": "onemin_api", "account_label": "ONEMIN_AI_API_KEY"}],
            [],
            1,
            0,
            False,
        )

    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    first = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": True, "provider_api_all_accounts": True},
    )
    assert first.status_code == 200
    assert first.json()["billing_refresh_count"] == 1
    assert first.json()["refresh_throttled"] is False

    second = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": True, "provider_api_all_accounts": True},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert call_count == 1
    assert second_body["refresh_throttled"] is True
    assert second_body["refresh_throttle_seconds_remaining"] == 40
    assert second_body["billing_refresh_count"] == 0
    assert second_body["member_reconciliation_count"] == 0
    assert second_body["api_account_attempted"] == 0
    assert second_body["api_account_skipped"] == 1
    assert "throttled to one run per minute" in second_body["note"]


def test_onemin_billing_refresh_forwards_bound_account_login_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "owner_email": "owner@example.com",
                    }
                ]
            }
        ),
    )

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_name": "ONEMIN_AI_API_KEY",
                "onemin_account_credentials_json": {
                    "ONEMIN_AI_API_KEY": {
                        "login_email": "slot@example.com",
                        "login_password": "slotpass",
                        "team_id": "team-123",
                        "team_name": "Finland Office",
                    }
                },
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    observed: dict[str, object] = {}

    def fake_refresh(**kwargs):
        observed.update(kwargs)
        return ([], [], [], 1, 0, False)

    def fake_invoke_browseract_tool(**_kwargs):
        raise providers_route.ToolExecutionError("browseract_unavailable")

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": True},
    )
    assert response.status_code == 200
    assert observed["account_labels"] == {"ONEMIN_AI_API_KEY"}
    assert observed["account_login_credentials"] == {
        "ONEMIN_AI_API_KEY": {
            "login_email": "slot@example.com",
            "login_password": "slotpass",
            "team_id": "team-123",
            "team_name": "Finland Office",
        }
    }


def test_onemin_billing_refresh_uses_browseract_login_fallback_and_skips_direct_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "owner_email": "owner@example.com",
                    }
                ]
            }
        ),
    )
    monkeypatch.setenv("BROWSERACT_PASSWORD", "slotpass")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_name": "ONEMIN_AI_API_KEY",
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    invoked: list[str] = []
    observed: dict[str, object] = {}

    def fake_invoke_browseract_tool(**kwargs):
        invoked.append(str(kwargs.get("tool_name") or ""))
        tool_name = str(kwargs.get("tool_name") or "")
        if tool_name == "browseract.onemin_billing_usage":
            return {
                "refresh_backend": "browseract",
                "remaining_credits": "12345",
                "max_credits": "20000",
                "next_topup_at": "2026-03-31T00:00:00Z",
                "topup_amount": "20000",
            }
        return {
            "refresh_backend": "browseract",
            "matched_owner_slots": 1,
        }

    def fake_refresh(**kwargs):
        observed.update(kwargs)
        return (
            [{"account_label": "ONEMIN_AI_API_KEY", "refresh_backend": "onemin_api"}],
            [{"account_label": "ONEMIN_AI_API_KEY", "refresh_backend": "onemin_api"}],
            [],
            1,
            0,
            False,
        )

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert invoked == [
        "browseract.onemin_billing_usage",
        "browseract.onemin_member_reconciliation",
    ]
    assert observed == {}
    assert body["billing_refresh_count"] == 1
    assert body["member_reconciliation_count"] == 1
    assert body["api_account_attempted"] == 0
    assert body["api_account_skipped"] == 1
    assert "BrowserAct login-backed billing pages" in body["note"]


def test_onemin_billing_refresh_uses_owner_ledger_browseract_scope_for_global_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {"account_name": "ONEMIN_AI_API_KEY", "owner_email": "owner-1@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_1", "owner_email": "owner-2@example.com"},
                ]
            }
        ),
    )
    monkeypatch.setenv("BROWSERACT_PASSWORD", "slotpass")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "service_accounts_json": {"BrowserAct": {"tier": "Tier 3"}},
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    invoked: list[tuple[str, str]] = []
    observed: dict[str, object] = {}

    def fake_invoke_browseract_tool(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "")
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        invoked.append((tool_name, account_label))
        if tool_name == "browseract.onemin_billing_usage":
            return {"refresh_backend": "browseract", "remaining_credits": "12345"}
        return {"refresh_backend": "browseract", "matched_owner_slots": 1}

    def fake_refresh(**kwargs):
        observed.update(kwargs)
        return ([], [], [], 0, 0, False)

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={"include_members": True, "provider_api_all_accounts": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert observed == {}
    assert body["browseract_scope"] == "all_owner_accounts"
    assert body["browseract_target_labels"] == ["ONEMIN_AI_API_KEY", "ONEMIN_AI_API_KEY_FALLBACK_1"]
    assert body["provider_api_scope"] == "global"
    assert body["provider_api_target_labels"] == []
    assert body["global_aggregate_snapshot"]["provider_key"] == "onemin"
    assert invoked == [
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY"),
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_1"),
        ("browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY"),
        ("browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY_FALLBACK_1"),
    ]


def test_onemin_billing_refresh_uses_fleet_browseract_binding_for_operator_targeted_owner_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_1", "owner_email": "owner-2@example.com"},
                ]
            }
        ),
    )
    monkeypatch.setenv("BROWSERACT_PASSWORD", "slotpass")

    from app.api.routes import providers as providers_route

    owner_binding = SimpleNamespace(
        binding_id="owner-binding",
        principal_id="exec-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        auth_metadata_json={
            "trusted_onemin_mapping": True,
            "service_accounts_json": {"BrowserAct": {"tier": "Tier 3"}},
        },
        status="enabled",
    )
    fleet_binding = SimpleNamespace(
        binding_id="fleet-binding",
        principal_id="codex-fleet",
        connector_name="browseract",
        external_account_ref="browseract-main",
        auth_metadata_json={
            "trusted_onemin_mapping": True,
            "onemin_account_names": ["ONEMIN_AI_API_KEY_FALLBACK_1"],
        },
        status="enabled",
    )

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_: True)
    monkeypatch.setattr(
        providers_route,
        "_normalized_onemin_owner_rows",
        lambda account_labels=None: [
            {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_1", "owner_email": "owner-2@example.com"}
        ],
    )
    monkeypatch.setattr(providers_route, "_enabled_browseract_bindings", lambda _container, _principal_id: [owner_binding])
    monkeypatch.setattr(providers_route, "_all_enabled_browseract_bindings", lambda _container: [owner_binding, fleet_binding])
    observed: dict[str, object] = {}

    def fake_invoke_browseract_tool(**kwargs):
        observed["principal_id"] = kwargs.get("principal_id")
        payload_json = dict(kwargs.get("payload_json") or {})
        observed["binding_id"] = payload_json.get("binding_id")
        observed["account_label"] = payload_json.get("account_label")
        return {"refresh_backend": "browseract", "remaining_credits": "12345"}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={
            "include_members": False,
            "include_provider_api": False,
            "account_labels": ["ONEMIN_AI_API_KEY_FALLBACK_1"],
        },
        )
    assert response.status_code == 200
    body = response.json()
    assert observed["principal_id"] == "codex-fleet"
    assert observed["binding_id"] == "fleet-binding"
    assert observed["account_label"] == "ONEMIN_AI_API_KEY_FALLBACK_1"
    assert body["selected_binding_ids"] == ["fleet-binding"]
    assert body["billing_refresh_count"] == 1
    assert body["browseract_target_labels"] == ["ONEMIN_AI_API_KEY_FALLBACK_1"]


def test_onemin_billing_refresh_caps_browseract_login_pass_per_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {"account_name": "ONEMIN_AI_API_KEY", "owner_email": "owner-1@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_1", "owner_email": "owner-2@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_2", "owner_email": "owner-3@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_3", "owner_email": "owner-4@example.com"},
                ]
            }
        ),
    )
    monkeypatch.setenv("BROWSERACT_PASSWORD", "slotpass")
    monkeypatch.setenv("ONEMIN_BROWSERACT_MAX_ACCOUNTS_PER_REFRESH", "2")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": [
                    "ONEMIN_AI_API_KEY",
                    "ONEMIN_AI_API_KEY_FALLBACK_1",
                    "ONEMIN_AI_API_KEY_FALLBACK_2",
                    "ONEMIN_AI_API_KEY_FALLBACK_3",
                ]
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    invoked: list[tuple[str, str]] = []
    observed: dict[str, object] = {}

    def fake_invoke_browseract_tool(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "")
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        invoked.append((tool_name, account_label))
        if tool_name == "browseract.onemin_billing_usage":
            return {"refresh_backend": "browseract", "remaining_credits": "12345"}
        return {"refresh_backend": "browseract", "matched_owner_slots": 1}

    def fake_refresh(**kwargs):
        observed.update(kwargs)
        return ([], [], [], 0, 0, False)

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    response = owner.post("/v1/providers/onemin/billing-refresh", json={"include_members": True})
    assert response.status_code == 200
    body = response.json()
    assert observed == {
        "include_members": True,
        "timeout_seconds": 75,
        "all_accounts": False,
        "continue_on_rate_limit": False,
        "account_labels": {"ONEMIN_AI_API_KEY_FALLBACK_2", "ONEMIN_AI_API_KEY_FALLBACK_3"},
        "account_login_credentials": {},
    }
    assert invoked == [
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY"),
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_1"),
        ("browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY"),
        ("browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY_FALLBACK_1"),
    ]
    assert body["billing_refresh_count"] == 2
    assert body["member_reconciliation_count"] == 2
    assert body["api_account_attempted"] == 0
    assert body["api_account_skipped"] == 0
    assert body["provider_api_target_labels"] == ["ONEMIN_AI_API_KEY_FALLBACK_2", "ONEMIN_AI_API_KEY_FALLBACK_3"]
    assert "skipped 2 bound 1min account(s)" in body["note"]


def test_onemin_billing_refresh_can_target_specific_account_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {"account_name": "ONEMIN_AI_API_KEY", "owner_email": "owner-1@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_1", "owner_email": "owner-2@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_2", "owner_email": "owner-3@example.com"},
                ]
            }
        ),
    )
    monkeypatch.setenv("BROWSERACT_PASSWORD", "slotpass")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": [
                    "ONEMIN_AI_API_KEY",
                    "ONEMIN_AI_API_KEY_FALLBACK_1",
                    "ONEMIN_AI_API_KEY_FALLBACK_2",
                ]
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    invoked: list[tuple[str, str]] = []

    def fake_invoke_browseract_tool(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "")
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        invoked.append((tool_name, account_label))
        if tool_name == "browseract.onemin_billing_usage":
            return {"refresh_backend": "browseract", "remaining_credits": "12345"}
        return {"refresh_backend": "browseract", "matched_owner_slots": 1}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={
            "include_members": False,
            "account_labels": ["ONEMIN_AI_API_KEY_FALLBACK_2", "UNKNOWN_SLOT"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert invoked == [("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_2")]
    assert body["billing_refresh_count"] == 1
    assert any(
        row.get("account_label") == "UNKNOWN_SLOT" and row.get("reason") == "account_label_not_bound"
        for row in body["skipped"]
    )


def test_onemin_billing_refresh_skips_targeted_fresh_actual_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_2", "owner_email": "owner-2@example.com"},
                ]
            }
        ),
    )
    monkeypatch.setenv("BROWSERACT_PASSWORD", "slotpass")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": [
                    "ONEMIN_AI_API_KEY_FALLBACK_2",
                ]
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(
        owner.app.state.container.onemin_manager,
        "accounts_snapshot",
        lambda **_: [
            {
                "account_label": "ONEMIN_AI_API_KEY_FALLBACK_2",
                "has_actual_billing": True,
                "last_billing_snapshot_at": now.isoformat(),
                "details_json": {
                    "billing_next_topup_at": (now + timedelta(hours=12)).isoformat(),
                },
            },
        ],
    )

    invoked: list[tuple[str, str]] = []
    provider_api_called = False

    def fake_invoke_browseract_tool(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "")
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        invoked.append((tool_name, account_label))
        return {"refresh_backend": "browseract", "remaining_credits": "12345"}

    def fake_refresh(**kwargs):
        nonlocal provider_api_called
        provider_api_called = True
        return ([], [], [], 0, 0, False)

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    response = owner.post(
        "/v1/providers/onemin/billing-refresh",
        json={
            "include_members": False,
            "account_labels": ["ONEMIN_AI_API_KEY_FALLBACK_2"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert invoked == []
    assert provider_api_called is False
    assert body["provider_api_target_labels"] == []
    assert body["billing_refresh_count"] == 0
    assert "fresh actual billing snapshots" in body["note"]


def test_onemin_billing_refresh_rotates_browseract_login_pass_across_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {"account_name": "ONEMIN_AI_API_KEY", "owner_email": "owner-1@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_1", "owner_email": "owner-2@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_2", "owner_email": "owner-3@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_3", "owner_email": "owner-4@example.com"},
                ]
            }
        ),
    )
    monkeypatch.setenv("BROWSERACT_PASSWORD", "slotpass")
    monkeypatch.setenv("ONEMIN_BROWSERACT_MAX_ACCOUNTS_PER_REFRESH", "2")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": [
                    "ONEMIN_AI_API_KEY",
                    "ONEMIN_AI_API_KEY_FALLBACK_1",
                    "ONEMIN_AI_API_KEY_FALLBACK_2",
                    "ONEMIN_AI_API_KEY_FALLBACK_3",
                ]
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    begin_states = iter([(True, 0.0, ""), (True, 0.0, "")])
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: next(begin_states))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)

    invoked: list[tuple[str, str]] = []

    def fake_invoke_browseract_tool(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "")
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        invoked.append((tool_name, account_label))
        if tool_name == "browseract.onemin_billing_usage":
            return {"refresh_backend": "browseract", "remaining_credits": "12345"}
        return {"refresh_backend": "browseract", "matched_owner_slots": 1}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))

    first = owner.post("/v1/providers/onemin/billing-refresh", json={"include_members": False})
    second = owner.post("/v1/providers/onemin/billing-refresh", json={"include_members": False})

    assert first.status_code == 200
    assert second.status_code == 200
    assert invoked == [
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY"),
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_1"),
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_2"),
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_3"),
    ]


def test_onemin_billing_refresh_fans_out_browseract_jobs_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv("ONEMIN_BROWSERACT_PARALLELISM", "3")
    for index, account_label in enumerate(
        [
            "ONEMIN_AI_API_KEY",
            "ONEMIN_AI_API_KEY_FALLBACK_1",
            "ONEMIN_AI_API_KEY_FALLBACK_2",
        ],
        start=1,
    ):
        created = owner.post(
            "/v1/connectors/bindings",
            json={
                "connector_name": "browseract",
                "external_account_ref": f"browseract-main-{index}",
                "scope_json": {"services": ["BrowserAct"]},
                "auth_metadata_json": {
                    "onemin_account_name": account_label,
                },
                "status": "enabled",
            },
        )
        assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_: True)

    barrier = threading.Barrier(3)
    invoked: list[tuple[str, str]] = []

    def fake_invoke_browseract_tool(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "")
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        if tool_name == "browseract.onemin_billing_usage":
            barrier.wait(timeout=1.0)
            invoked.append((tool_name, account_label))
            return {"refresh_backend": "browseract", "remaining_credits": "12345"}
        return {"refresh_backend": "browseract", "matched_owner_slots": 1}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)

    response = owner.post("/v1/providers/onemin/billing-refresh", json={"include_members": False})

    assert response.status_code == 200
    assert response.json()["billing_refresh_count"] == 3
    assert sorted(invoked) == [
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY"),
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_1"),
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_2"),
    ]


def test_onemin_billing_refresh_only_reconciles_members_after_successful_billing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": [
                    "ONEMIN_AI_API_KEY",
                    "ONEMIN_AI_API_KEY_FALLBACK_1",
                ]
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_: True)

    invoked: list[tuple[str, str]] = []

    def fake_invoke_browseract_tool(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "")
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        invoked.append((tool_name, account_label))
        if tool_name == "browseract.onemin_billing_usage" and account_label == "ONEMIN_AI_API_KEY_FALLBACK_1":
            raise providers_route.ToolExecutionError("login_failed")
        if tool_name == "browseract.onemin_billing_usage":
            return {"refresh_backend": "browseract", "remaining_credits": "12345"}
        return {"refresh_backend": "browseract", "matched_owner_slots": 1}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)

    response = owner.post("/v1/providers/onemin/billing-refresh", json={"include_members": True})

    assert response.status_code == 200
    assert ("browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY") in invoked
    assert ("browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY_FALLBACK_1") not in invoked
    assert response.json()["member_reconciliation_count"] == 1


def test_onemin_billing_refresh_stops_after_systemic_browseract_failures_and_targets_remaining_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    monkeypatch.setenv("ONEMIN_BROWSERACT_SYSTEMIC_FAILURE_THRESHOLD", "2")

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": [
                    "ONEMIN_AI_API_KEY",
                    "ONEMIN_AI_API_KEY_FALLBACK_1",
                    "ONEMIN_AI_API_KEY_FALLBACK_2",
                    "ONEMIN_AI_API_KEY_FALLBACK_3",
                ]
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    observed: dict[str, object] = {}
    invoked: list[str] = []

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_: True)

    def fake_invoke_browseract_tool(**kwargs):
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        invoked.append(account_label)
        raise providers_route.ToolExecutionError("ui_service_worker_failed:onemin_billing_usage:auth_request_failed")

    def fake_refresh(**kwargs):
        observed.update(kwargs)
        return ([], [], [], 0, 0, False)

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    response = owner.post("/v1/providers/onemin/billing-refresh", json={"include_members": False})

    assert response.status_code == 200
    body = response.json()
    assert invoked == [
        "ONEMIN_AI_API_KEY",
        "ONEMIN_AI_API_KEY_FALLBACK_1",
    ]
    assert body["browseract_failed_labels"] == [
        "ONEMIN_AI_API_KEY",
        "ONEMIN_AI_API_KEY_FALLBACK_1",
    ]
    assert body["provider_api_target_labels"] == [
        "ONEMIN_AI_API_KEY",
        "ONEMIN_AI_API_KEY_FALLBACK_1",
        "ONEMIN_AI_API_KEY_FALLBACK_2",
        "ONEMIN_AI_API_KEY_FALLBACK_3",
    ]
    assert observed["account_labels"] == {
        "ONEMIN_AI_API_KEY",
        "ONEMIN_AI_API_KEY_FALLBACK_1",
        "ONEMIN_AI_API_KEY_FALLBACK_2",
        "ONEMIN_AI_API_KEY_FALLBACK_3",
    }


def test_onemin_billing_refresh_recovers_browseract_failures_via_targeted_provider_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": [
                    "ONEMIN_AI_API_KEY",
                    "ONEMIN_AI_API_KEY_FALLBACK_1",
                ]
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(
        providers_route,
        "_resolve_onemin_account_labels",
        lambda _binding: ("ONEMIN_AI_API_KEY", "ONEMIN_AI_API_KEY_FALLBACK_1"),
    )
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_: True)
    monkeypatch.setattr(
        providers_route.upstream,
        "_provider_health_report",
        lambda: {
            "providers": {
                "onemin": {
                    "configured_slots": 2,
                    "slots": [
                        {
                            "account_name": "ONEMIN_AI_API_KEY",
                            "slot_env_name": "ONEMIN_AI_API_KEY",
                            "slot": "slot-1",
                            "state": "ready",
                            "estimated_remaining_credits": 12345.0,
                        },
                        {
                            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
                            "slot": "slot-2",
                            "state": "ready",
                            "estimated_remaining_credits": 12000.0,
                        },
                    ],
                }
            }
        },
    )

    invoked: list[tuple[str, str]] = []
    observed: dict[str, object] = {}

    def fake_invoke_browseract_tool(**kwargs):
        tool_name = str(kwargs.get("tool_name") or "")
        payload_json = dict(kwargs.get("payload_json") or {})
        account_label = str(payload_json.get("account_label") or "")
        invoked.append((tool_name, account_label))
        if tool_name == "browseract.onemin_billing_usage" and account_label == "ONEMIN_AI_API_KEY_FALLBACK_1":
            raise providers_route.ToolExecutionError(
                "ui_service_worker_failed:onemin_billing_usage:auth_request_failed"
            )
        if tool_name == "browseract.onemin_billing_usage":
            return {"refresh_backend": "browseract", "account_label": account_label, "remaining_credits": "12345"}
        return {"refresh_backend": "browseract", "account_label": account_label, "matched_owner_slots": 1}

    def fake_refresh(**kwargs):
        observed.update(kwargs)
        return (
            [{"account_label": "ONEMIN_AI_API_KEY_FALLBACK_1", "refresh_backend": "onemin_api", "remaining_credits": 12000}],
            [{"account_label": "ONEMIN_AI_API_KEY_FALLBACK_1", "refresh_backend": "onemin_api", "matched_owner_slots": 1}],
            [],
            1,
            0,
            False,
        )

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)

    response = owner.post("/v1/providers/onemin/billing-refresh", json={"include_members": True})

    assert response.status_code == 200
    body = response.json()
    assert observed["all_accounts"] is False
    assert observed["account_labels"] == {"ONEMIN_AI_API_KEY_FALLBACK_1"}
    assert body["billing_refresh_count"] == 2
    assert body["member_reconciliation_count"] == 2
    assert body["errors"] == []
    assert body["provider_api_recovery_mode"] == "browseract_failure_recovery"
    assert body["provider_api_target_labels"] == ["ONEMIN_AI_API_KEY_FALLBACK_1"]
    assert body["browseract_failed_labels"] == ["ONEMIN_AI_API_KEY_FALLBACK_1"]
    assert body["browseract_recovered_labels"] == ["ONEMIN_AI_API_KEY_FALLBACK_1"]
    assert body["aggregate_snapshot"]["sum_free_credits"] == 24345.0
    assert body["actual_credits_snapshot"]["binding_account_count"] == 2
    assert "recovered through the direct 1min API" in body["note"]
    assert invoked == [
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY"),
        ("browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_1"),
        ("browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY"),
    ]


def test_onemin_billing_refresh_prioritizes_missing_and_stale_actual_billing_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "browseract-main",
            "scope_json": {"services": ["BrowserAct"]},
            "auth_metadata_json": {
                "onemin_account_names": [
                    "ONEMIN_AI_API_KEY",
                    "ONEMIN_AI_API_KEY_FALLBACK_1",
                    "ONEMIN_AI_API_KEY_FALLBACK_2",
                    "ONEMIN_AI_API_KEY_FALLBACK_3",
                ]
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    from app.api.routes import providers as providers_route

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "begin_billing_refresh", lambda: (True, 0.0, ""))
    monkeypatch.setattr(owner.app.state.container.onemin_manager, "finish_billing_refresh", lambda: None)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_: ([], [], [], 0, 0, False))
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_: True)
    monkeypatch.setattr(
        owner.app.state.container.onemin_manager,
        "accounts_snapshot",
        lambda **_: [
            {
                "account_label": "ONEMIN_AI_API_KEY",
                "has_actual_billing": True,
                "last_billing_snapshot_at": "2026-03-28T12:00:00+00:00",
            },
            {
                "account_label": "ONEMIN_AI_API_KEY_FALLBACK_1",
                "has_actual_billing": False,
                "last_billing_snapshot_at": None,
            },
            {
                "account_label": "ONEMIN_AI_API_KEY_FALLBACK_2",
                "has_actual_billing": True,
                "last_billing_snapshot_at": "2026-03-28T09:00:00+00:00",
            },
            {
                "account_label": "ONEMIN_AI_API_KEY_FALLBACK_3",
                "has_actual_billing": False,
                "last_billing_snapshot_at": "2026-03-27T09:00:00+00:00",
            },
        ],
    )

    selected_orders: list[list[str]] = []

    def fake_select(labels, *, limit: int):
        selected_orders.append(list(labels))
        return tuple(list(labels)[:limit])

    monkeypatch.setattr(owner.app.state.container.onemin_manager, "select_billing_refresh_account_labels", fake_select)
    monkeypatch.setattr(
        providers_route,
        "_invoke_browseract_tool",
        lambda **kwargs: {"refresh_backend": "browseract", "remaining_credits": "12345"}
        if str(kwargs.get("tool_name") or "") == "browseract.onemin_billing_usage"
        else {"refresh_backend": "browseract", "matched_owner_slots": 1},
    )

    response = owner.post("/v1/providers/onemin/billing-refresh", json={"include_members": False})

    assert response.status_code == 200
    assert selected_orders == [
        [
            "ONEMIN_AI_API_KEY_FALLBACK_1",
            "ONEMIN_AI_API_KEY_FALLBACK_3",
            "ONEMIN_AI_API_KEY_FALLBACK_2",
            "ONEMIN_AI_API_KEY",
        ],
    ]


def test_onemin_provider_api_full_refresh_continues_after_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route
    monkeypatch.setattr(
        providers_route.upstream,
        "onemin_owner_rows",
        lambda: (
            {"account_name": "ONEMIN_AI_API_KEY", "owner_email": "owner-1@example.com"},
            {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_1", "owner_email": "owner-2@example.com"},
            {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_2", "owner_email": "owner-3@example.com"},
        ),
    )

    calls: list[str] = []

    def fake_refresh_account(
        *,
        account_name: str,
        owner_email: str,
        include_members: bool,
        timeout_seconds: int,
        login_email: str = "",
        login_password: str = "",
    ):
        calls.append(account_name)
        if account_name == "ONEMIN_AI_API_KEY":
            raise RuntimeError("onemin_login_http_429")
        billing_result = {
            "refresh_backend": "onemin_api",
            "account_label": account_name,
            "owner_email": owner_email,
            "basis": "actual_provider_api",
        }
        member_result = {
            "refresh_backend": "onemin_api",
            "account_label": account_name,
            "owner_email": owner_email,
            "basis": "actual_provider_api",
        }
        return billing_result, member_result if include_members else None

    monkeypatch.setattr(providers_route, "_refresh_onemin_api_account", fake_refresh_account)
    monkeypatch.setattr(providers_route.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(providers_route, "_ONEMIN_DIRECT_API_QUARANTINED_UNTIL", 0.0, raising=False)
    monkeypatch.setattr(providers_route, "_ONEMIN_DIRECT_API_QUARANTINE_REASON", "", raising=False)
    monkeypatch.setattr(providers_route, "_onemin_direct_api_uses_fastestvpn_proxy", lambda **_: False)
    monkeypatch.setenv("ONEMIN_DIRECT_API_MAX_RATE_LIMIT_SLEEP_SECONDS", "0")

    billing_results, member_results, errors, attempted_count, skipped_count, rate_limited = providers_route._refresh_onemin_via_provider_api(
        include_members=True,
        timeout_seconds=180,
        all_accounts=True,
        continue_on_rate_limit=True,
    )

    assert calls == ["ONEMIN_AI_API_KEY"]
    assert attempted_count == 1
    assert skipped_count == 2
    assert rate_limited is True
    assert len(errors) == 1
    assert errors[0]["tool_name"] == "onemin.api.billing_refresh"
    assert billing_results == []
    assert member_results == []


def test_onemin_provider_api_refresh_batches_after_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route

    calls: list[str] = []
    sleep_calls: list[float] = []

    monkeypatch.setenv(
        "EA_RESPONSES_ONEMIN_OWNER_LEDGER_JSON",
        json.dumps(
            {
                "slots": [
                    {"account_name": "ONEMIN_AI_API_KEY", "owner_email": "owner-1@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_1", "owner_email": "owner-2@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_2", "owner_email": "owner-3@example.com"},
                    {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_3", "owner_email": "owner-4@example.com"},
                ]
            }
        ),
    )

    quarantine_state = {"seconds": 0.0, "reason": ""}
    monkeypatch.setattr(
        providers_route,
        "_onemin_direct_api_quarantine_remaining",
        lambda: (quarantine_state["seconds"], quarantine_state["reason"]),
    )
    monkeypatch.setattr(
        providers_route,
        "_quarantine_onemin_direct_api",
        lambda reason: quarantine_state.update({"seconds": 300.0, "reason": reason}),
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "onemin_owner_rows",
        lambda: (
            {
                "account_name": "ONEMIN_AI_API_KEY",
                "owner_email": "owner-1@example.com",
            },
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
                "owner_email": "owner-2@example.com",
            },
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_2",
                "owner_email": "owner-3@example.com",
            },
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_3",
                "owner_email": "owner-4@example.com",
            },
        ),
    )

    def fake_refresh_account(
        *,
        account_name: str,
        owner_email: str,
        include_members: bool,
        timeout_seconds: int,
        login_email: str = "",
        login_password: str = "",
    ):
        calls.append(account_name)
        if account_name == "ONEMIN_AI_API_KEY":
            raise RuntimeError("onemin_login_http_429")
        billing_result = {
            "refresh_backend": "onemin_api",
            "account_label": account_name,
            "owner_email": owner_email,
            "basis": "actual_provider_api",
        }
        member_result = {
            "refresh_backend": "onemin_api",
            "account_label": account_name,
            "owner_email": owner_email,
            "basis": "actual_provider_api",
        }
        return billing_result, member_result if include_members else None

    monkeypatch.setattr(providers_route, "_refresh_onemin_api_account", fake_refresh_account)
    monkeypatch.setenv("ONEMIN_DIRECT_API_BATCH_SIZE", "2")
    monkeypatch.setenv("ONEMIN_DIRECT_API_BATCH_BACKOFF_SECONDS", "0.5")
    monkeypatch.setenv("ONEMIN_DIRECT_API_MIN_ACCOUNT_DELAY_SECONDS", "0")
    monkeypatch.setattr(providers_route, "_onemin_direct_api_uses_fastestvpn_proxy", lambda **_: False)
    monkeypatch.setenv("ONEMIN_DIRECT_API_MAX_RATE_LIMIT_SLEEP_SECONDS", "0")
    monkeypatch.setattr(providers_route.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    _, _, errors, attempted_count, skipped_count, rate_limited = providers_route._refresh_onemin_via_provider_api(
        include_members=True,
        timeout_seconds=180,
        all_accounts=True,
        continue_on_rate_limit=True,
    )

    assert calls == ["ONEMIN_AI_API_KEY"]
    assert attempted_count == 1
    assert skipped_count == 3
    assert rate_limited is True
    assert errors[-1]["error"] == "onemin_login_http_429"
    assert sleep_calls == []


def test_onemin_provider_api_refresh_rotates_fastestvpn_proxy_and_recovers_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route

    monkeypatch.setattr(
        providers_route.upstream,
        "onemin_owner_rows",
        lambda: (
            {"account_name": "ONEMIN_AI_API_KEY", "owner_email": "owner-1@example.com"},
        ),
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "_onemin_direct_api_proxy_url_for_subject",
        lambda _subject="": "http://ea-fastestvpn-proxy:3128",
        raising=False,
    )
    monkeypatch.setattr(
        providers_route,
        "_onemin_direct_api_quarantine_remaining",
        lambda: (0.0, ""),
    )
    monkeypatch.setattr(providers_route.time, "sleep", lambda *_args, **_kwargs: None)

    rotation_reasons: list[str] = []
    call_count = {"value": 0}

    def fake_refresh_account(
        *,
        account_name: str,
        owner_email: str,
        include_members: bool,
        timeout_seconds: int,
        login_email: str = "",
        login_password: str = "",
    ):
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError("onemin_login_http_429")
        billing_result = {
            "refresh_backend": "onemin_api",
            "account_label": account_name,
            "owner_email": owner_email,
            "basis": "actual_provider_api",
        }
        member_result = {
            "refresh_backend": "onemin_api",
            "account_label": account_name,
            "owner_email": owner_email,
            "basis": "actual_provider_api",
        }
        return billing_result, member_result if include_members else None

    def fake_rotate_fastestvpn_proxy(*, reason: str):
        rotation_reasons.append(reason)
        return {"returncode": 0, "stdout": "rotated", "stderr": "", "duration_seconds": 0.1}

    monkeypatch.setattr(providers_route, "_refresh_onemin_api_account", fake_refresh_account)
    monkeypatch.setattr(providers_route, "_rotate_fastestvpn_proxy", fake_rotate_fastestvpn_proxy)
    monkeypatch.setenv("EA_ONEMIN_DIRECT_API_PROXY_ROTATION_RETRY_LIMIT", "1")

    billing_results, member_results, errors, attempted_count, skipped_count, rate_limited = providers_route._refresh_onemin_via_provider_api(
        include_members=True,
        timeout_seconds=180,
        all_accounts=True,
        continue_on_rate_limit=True,
    )

    assert attempted_count == 1
    assert skipped_count == 0
    assert rate_limited is True
    assert errors == []
    assert [row["account_label"] for row in billing_results] == ["ONEMIN_AI_API_KEY"]
    assert [row["account_label"] for row in member_results] == ["ONEMIN_AI_API_KEY"]
    assert rotation_reasons == ["onemin.api.billing_refresh:ONEMIN_AI_API_KEY"]


def test_onemin_provider_api_refresh_tries_full_fastestvpn_pool_before_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route

    call_offsets: list[int] = []

    monkeypatch.setattr(
        providers_route.upstream,
        "onemin_owner_rows",
        lambda: (
            {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_27", "owner_email": "owner-27@example.com"},
        ),
    )

    def fake_proxy_url_for_subject(subject: str = "", retry_offset: int = 0):
        pool = (
            "http://ea-fastestvpn-proxy:3128",
            "http://ea-fastestvpn-proxy-ie:3128",
            "http://ea-fastestvpn-proxy-nl:3128",
        )
        return pool[retry_offset % len(pool)]

    monkeypatch.setattr(
        providers_route.upstream,
        "_onemin_direct_api_proxy_url_for_subject",
        fake_proxy_url_for_subject,
        raising=False,
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "_onemin_direct_api_proxy_pool_urls",
        lambda: (
            "http://ea-fastestvpn-proxy:3128",
            "http://ea-fastestvpn-proxy-ie:3128",
            "http://ea-fastestvpn-proxy-nl:3128",
        ),
        raising=False,
    )
    monkeypatch.setattr(providers_route.time, "sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("EA_ONEMIN_DIRECT_API_PROXY_ROTATION_RETRY_LIMIT", "1")
    monkeypatch.setenv("ONEMIN_DIRECT_API_MAX_RATE_LIMIT_SLEEP_SECONDS", "0")

    def fake_refresh_account(
        *,
        account_name: str,
        owner_email: str,
        include_members: bool,
        timeout_seconds: int,
        login_email: str = "",
        login_password: str = "",
        preferred_team_id: str = "",
        preferred_team_name: str = "",
        proxy_retry_offset: int = 0,
    ):
        call_offsets.append(proxy_retry_offset)
        if proxy_retry_offset < 2:
            raise RuntimeError("onemin_login_http_429")
        billing_result = {
            "refresh_backend": "onemin_api",
            "account_label": account_name,
            "owner_email": owner_email,
            "basis": "actual_provider_api",
        }
        return billing_result, None

    monkeypatch.setattr(providers_route, "_refresh_onemin_api_account", fake_refresh_account)

    billing_results, member_results, errors, attempted_count, skipped_count, rate_limited = providers_route._refresh_onemin_via_provider_api(
        include_members=False,
        timeout_seconds=180,
        all_accounts=True,
        continue_on_rate_limit=True,
    )

    assert attempted_count == 1
    assert skipped_count == 0
    assert rate_limited is True
    assert errors == []
    assert member_results == []
    assert [row["account_label"] for row in billing_results] == ["ONEMIN_AI_API_KEY_FALLBACK_27"]
    assert call_offsets == [0, 1, 2]


def test_onemin_provider_api_refresh_sleeps_for_retry_after_and_retries_same_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route

    sleep_calls: list[float] = []
    call_count = {"value": 0}

    monkeypatch.setattr(
        providers_route.upstream,
        "onemin_owner_rows",
        lambda: (
            {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_27", "owner_email": "owner-27@example.com"},
        ),
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "_onemin_direct_api_proxy_url_for_subject",
        lambda _subject="", retry_offset=0: "http://ea-fastestvpn-proxy-nl:3128",
        raising=False,
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "_onemin_direct_api_proxy_pool_urls",
        lambda: ("http://ea-fastestvpn-proxy-nl:3128",),
        raising=False,
    )
    monkeypatch.setattr(providers_route.time, "sleep", lambda seconds: sleep_calls.append(seconds))
    monkeypatch.setenv("ONEMIN_DIRECT_API_MAX_RATE_LIMIT_SLEEP_SECONDS", "200")
    monkeypatch.setenv("EA_ONEMIN_DIRECT_API_PROXY_ROTATION_RETRY_LIMIT", "0")

    def fake_refresh_account(
        *,
        account_name: str,
        owner_email: str,
        include_members: bool,
        timeout_seconds: int,
        login_email: str = "",
        login_password: str = "",
        preferred_team_id: str = "",
        preferred_team_name: str = "",
        proxy_retry_offset: int = 0,
    ):
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise RuntimeError(
                'onemin_login_http_429:{"message":"Too many requests. Please try again after 151 seconds","retryAfter":151}'
            )
        billing_result = {
            "refresh_backend": "onemin_api",
            "account_label": account_name,
            "owner_email": owner_email,
            "basis": "actual_provider_api",
        }
        return billing_result, None

    monkeypatch.setattr(providers_route, "_refresh_onemin_api_account", fake_refresh_account)

    billing_results, member_results, errors, attempted_count, skipped_count, rate_limited = providers_route._refresh_onemin_via_provider_api(
        include_members=False,
        timeout_seconds=180,
        all_accounts=True,
        continue_on_rate_limit=True,
    )

    assert attempted_count == 1
    assert skipped_count == 0
    assert rate_limited is True
    assert errors == []
    assert member_results == []
    assert [row["account_label"] for row in billing_results] == ["ONEMIN_AI_API_KEY_FALLBACK_27"]
    assert sleep_calls == pytest.approx([166.0])
    assert call_count["value"] == 2


def test_onemin_direct_api_quarantine_uses_retry_after_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route

    monkeypatch.setenv("ONEMIN_DIRECT_API_CLOUDFLARE_COOLDOWN_SECONDS", "7200")
    seconds = providers_route._onemin_direct_api_quarantine_seconds_for_reason(
        'onemin_login_http_429:{"message":"Too many requests. Please try again after 252 seconds","retryAfter":252}'
    )

    assert seconds == 267.0


def test_onemin_manager_exposes_hourly_burn_rate_on_accounts_aggregate_and_actual_credits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)
    from app.api.routes import providers as providers_route

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "owner@example.com",
            "auth_metadata_json": {
                "onemin_account_name": "ONEMIN_AI_API_KEY",
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    monkeypatch.setattr(
        providers_route.upstream,
        "_provider_health_report",
        lambda: {
            "providers": {
                "onemin": {
                    "configured_slots": 3,
                    "estimated_burn_credits_per_hour": 2400.0,
                    "estimated_hours_remaining_at_current_pace": 50.0,
                    "estimated_days_remaining_at_7d_average": 4.5,
                    "slots": [
                        {
                            "account_name": "ONEMIN_AI_API_KEY",
                            "slot": "primary",
                            "slot_env_name": "ONEMIN_AI_API_KEY",
                            "state": "ready",
                            "owner_email": "owner@example.com",
                            "billing_remaining_credits": 1000.0,
                            "billing_max_credits": 2000.0,
                            "billing_basis": "actual_billing_usage_page",
                            "billing_observed_usage_burn_credits_per_hour": 1200.0,
                        },
                        {
                            "account_name": "ONEMIN_AI_API_KEY",
                            "slot": "fallback_1",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
                            "state": "ready",
                            "owner_email": "owner@example.com",
                            "estimated_remaining_credits": 0.0,
                            "billing_observed_usage_burn_credits_per_hour": 300.0,
                        },
                        {
                            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_2",
                            "slot": "fallback_2",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_2",
                            "state": "ready",
                            "owner_email": "other@example.com",
                            "estimated_remaining_credits": 500.0,
                        },
                    ],
                }
            }
        },
    )

    accounts = owner.get("/v1/providers/onemin/accounts")
    assert accounts.status_code == 200
    account_row = next(row for row in accounts.json()["accounts"] if row["account_id"] == "ONEMIN_AI_API_KEY")
    assert account_row["observed_usage_burn_credits_per_hour"] == 1500.0
    assert account_row["current_burn_credits_per_hour"] == 1500.0
    assert account_row["burn_basis"] == "observed_usage"
    assert account_row["slot_count_with_observed_usage_burn"] == 2

    aggregate = owner.get("/v1/providers/onemin/aggregate")
    assert aggregate.status_code == 200
    aggregate_body = aggregate.json()
    assert aggregate_body["observed_usage_burn_credits_per_hour"] == 1500.0
    assert aggregate_body["estimated_pool_burn_credits_per_hour"] == 2400.0
    assert aggregate_body["current_burn_credits_per_hour"] == 1500.0
    assert aggregate_body["burn_basis"] == "observed_usage"
    assert aggregate_body["bound_observed_usage_burn_credits_per_hour"] == 1500.0

    actual = owner.get("/v1/providers/onemin/actual-credits")
    assert actual.status_code == 200
    actual_body = actual.json()
    assert actual_body["actual_free_credits_total"] == 1000.0
    assert actual_body["observed_usage_burn_credits_per_hour"] == 1500.0
    assert actual_body["current_burn_credits_per_hour"] == 1500.0
    assert actual_body["burn_basis"] == "observed_usage"
    assert actual_body["global_estimated_pool_burn_credits_per_hour"] == 2400.0

def test_onemin_aggregate_and_runway_expose_scope_and_operator_global_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route

    owner = _client(principal_id="exec-1", operator=True)

    created = owner.post(
        "/v1/connectors/bindings",
        json={
            "connector_name": "browseract",
            "external_account_ref": "owner@example.com",
            "auth_metadata_json": {
                "onemin_account_name": "ONEMIN_AI_API_KEY",
            },
            "status": "enabled",
        },
    )
    assert created.status_code == 200

    monkeypatch.setattr(
        providers_route.upstream,
        "_provider_health_report",
        lambda: {
            "providers": {
                "onemin": {
                    "configured_slots": 2,
                    "estimated_remaining_credits_total": 1500.0,
                    "remaining_percent_of_max": 75.0,
                    "estimated_hours_remaining_at_current_pace": 12.0,
                    "estimated_days_remaining_at_7d_average": 1.5,
                    "slots": [
                        {
                            "account_name": "ONEMIN_AI_API_KEY",
                            "slot": "primary",
                            "slot_env_name": "ONEMIN_AI_API_KEY",
                            "state": "ready",
                            "estimated_remaining_credits": 1000.0,
                        },
                        {
                            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
                            "slot": "fallback_1",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
                            "state": "ready",
                            "estimated_remaining_credits": 500.0,
                        },
                    ],
                }
            }
        },
    )

    principal_aggregate = owner.get("/v1/providers/onemin/aggregate")
    assert principal_aggregate.status_code == 200
    principal_aggregate_body = principal_aggregate.json()
    assert principal_aggregate_body["scope"] == "principal_bindings"
    assert principal_aggregate_body["sum_free_credits"] == 1000.0
    assert principal_aggregate_body["live_remaining_credits_total"] == 1000.0
    assert principal_aggregate_body["live_positive_balance_account_count"] == 1
    assert principal_aggregate_body["global_estimated_free_credits_total"] == 1500.0
    assert principal_aggregate_body["global_live_remaining_credits_total"] == 1500.0
    assert principal_aggregate_body["global_estimated_hours_remaining_at_current_pace"] == 12.0
    assert principal_aggregate_body["scope_note"].startswith("principal view only includes 1min accounts bound")

    principal_runway = owner.get("/v1/providers/onemin/runway")
    assert principal_runway.status_code == 200
    principal_runway_body = principal_runway.json()
    assert principal_runway_body["forecast"]["scope"] == "principal_bindings"
    assert principal_runway_body["forecast"]["remaining_credits"] == 1000.0
    assert principal_runway_body["forecast"]["global_estimated_free_credits_total"] == 1500.0
    assert principal_runway_body["forecast"]["global_live_remaining_credits_total"] == 1500.0

    viewer = _client(principal_id="exec-viewer")
    denied_global = viewer.get("/v1/providers/onemin/aggregate?scope=global")
    assert denied_global.status_code == 403
    denied_body = denied_global.json()
    if "detail" in denied_body:
        assert denied_body["detail"] == "operator_scope_required"
    else:
        assert denied_body["error"]["code"] == "operator_scope_required"

    operator = _client(principal_id="exec-ops", operator=True)
    global_aggregate = operator.get("/v1/providers/onemin/aggregate?scope=global")
    assert global_aggregate.status_code == 200
    global_aggregate_body = global_aggregate.json()
    assert global_aggregate_body["scope"] == "global_pool"
    assert global_aggregate_body["scope_principal_id"] is None
    assert global_aggregate_body["sum_free_credits"] == 1500.0
    assert global_aggregate_body["live_remaining_credits_total"] == 1500.0
    assert global_aggregate_body["live_positive_balance_slot_count"] == 2
    assert global_aggregate_body["account_count"] == 2
    assert global_aggregate_body["scope_note"] == ""

    global_runway = operator.get("/v1/providers/onemin/runway?scope=global")
    assert global_runway.status_code == 200
    global_runway_body = global_runway.json()
    assert global_runway_body["principal_id"] == "exec-ops"
    assert global_runway_body["forecast"]["scope"] == "global_pool"
    assert global_runway_body["forecast"]["remaining_credits"] == 1500.0
    assert global_runway_body["forecast"]["global_live_remaining_credits_total"] == 1500.0


def test_onemin_manager_runway_falls_back_to_observed_burn_when_provider_pace_is_missing() -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    provider_health = {
        "providers": {
            "onemin": {
                "configured_slots": 3,
                "estimated_burn_credits_per_hour": 2400.0,
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "slot": "primary",
                        "slot_env_name": "ONEMIN_AI_API_KEY",
                        "state": "ready",
                        "billing_remaining_credits": 1000.0,
                        "billing_max_credits": 2000.0,
                        "billing_basis": "actual_billing_usage_page",
                        "billing_observed_usage_burn_credits_per_hour": 1200.0,
                    },
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "slot": "fallback_1",
                        "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
                        "state": "ready",
                        "estimated_remaining_credits": 0.0,
                        "billing_observed_usage_burn_credits_per_hour": 300.0,
                    },
                    {
                        "account_name": "ONEMIN_AI_API_KEY_FALLBACK_2",
                        "slot": "fallback_2",
                        "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_2",
                        "state": "ready",
                        "estimated_remaining_credits": 500.0,
                    },
                ],
            }
        }
    }

    forecast = manager.runway_snapshot(provider_health=provider_health, binding_rows=[], principal_id="")

    assert forecast["remaining_credits"] == 1500.0
    assert forecast["current_burn_per_hour"] == 1500.0
    assert forecast["hours_remaining_current_pace"] == 1.0
    assert forecast["days_remaining_7d_avg"] == 0.04
    assert forecast["burn_basis"] == "observed_usage"


def test_provider_registry_endpoint_exposes_lane_backend_and_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _client(principal_id="exec-1", operator=True)

    monkeypatch.setenv("EA_GEMINI_VORTEX_COMMAND", "sh")
    monkeypatch.setenv("GOOGLE_API_KEY_FALLBACK_1", "vertex-fallback")
    monkeypatch.setenv("EA_GEMINI_VORTEX_SLOT_DEFAULT_OWNER", "fleet-primary")
    monkeypatch.setenv("EA_GEMINI_VORTEX_SLOT_FALLBACK_1_OWNER", "fleet-shadow")
    monkeypatch.setenv("BROWSERACT_API_KEY", "browseract-key")

    response = owner.get("/v1/providers/registry")
    assert response.status_code == 200
    body = response.json()

    assert body["contract_name"] == "ea.provider_registry"
    assert body["principal_id"] == "exec-1"

    groundwork = next(item for item in body["lanes"] if item["profile"] == "groundwork")
    assert groundwork["backend"] == "gemini_vortex"
    assert groundwork["health_provider_key"] == "gemini_vortex"
    assert groundwork["capacity_summary"]["configured_slots"] == 2
    assert groundwork["capacity_summary"]["slot_owners"] == ["fleet-primary", "fleet-shadow"]

    review_light = next(item for item in body["lanes"] if item["profile"] == "review_light")
    assert review_light["backend"] == "browseract"
    assert review_light["health_provider_key"] == "browseract"
    assert review_light["providers"][0]["provider_key"] == "browseract"


def test_media_stewardship_endpoint_exposes_scheduler_and_challenger_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    owner = _client(principal_id="exec-1", operator=True)

    from app.api.routes import providers as providers_route

    scheduler_path = tmp_path / "provider-scheduler.json"
    challenger_path = tmp_path / "challenger-ledger.json"
    scheduler_path.write_text(
        json.dumps(
            {
                "providers": {
                    "media_factory": {
                        "active_until_epoch": 4102444800.0,
                        "active_target": "assets/hero/chummer6-hero.png",
                        "updated_at": 4102441200.0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    challenger_path.write_text(
        json.dumps(
            {
                "assets": {
                    "assets/hero/chummer6-hero.png": {
                        "provider": "media_factory",
                        "status": "media_factory:rendered",
                        "score": 312.0,
                        "updated_at": 4102441200.0,
                        "last_challenger": {
                            "provider": "gemini_vortex",
                            "status": "gemini_vortex:rendered",
                            "beat_champion": False,
                            "updated_at": 4102441300.0,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(providers_route, "_MEDIA_PROVIDER_SCHEDULER_PATH", scheduler_path)
    monkeypatch.setattr(providers_route, "_MEDIA_CHALLENGER_LEDGER_PATH", challenger_path)

    response = owner.get("/v1/providers/media-stewardship")
    assert response.status_code == 200
    body = response.json()
    assert body["contract_name"] == "ea.media_stewardship"
    assert body["provider_scheduler"]["provider_count"] == 1
    assert body["provider_scheduler"]["active_provider_count"] == 1
    assert body["provider_scheduler"]["providers"][0]["provider_key"] == "media_factory"
    assert body["provider_scheduler"]["providers"][0]["active_target"] == "assets/hero/chummer6-hero.png"
    assert body["provider_scheduler"]["providers"][0]["wait_seconds_remaining"] > 0
    assert body["challenger_ledger"]["asset_count"] == 1
    assert body["challenger_ledger"]["challenger_count"] == 1
    assert body["challenger_ledger"]["assets"][0]["last_challenger_provider"] == "gemini_vortex"
    assert body["challenger_ledger"]["assets"][0]["last_challenger_beat_champion"] is False


def test_public_tour_routes_serve_bundle_html_json_and_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_MYEXTERNALBRAIN_SITE_ID", "33ff8f39-6213-4903-99d7-81048b5b3e1f")
    slug = "kahlenberg-layout-first"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    asset_path = bundle_dir / "scene-01.jpg"
    asset_path.write_bytes(b"fake-jpeg-data")
    (bundle_dir / "private-secret.jpg").write_bytes(b"private-jpeg-data")
    for generated_relpath in (
        "tour.mp4",
        "telegram-preview-private.jpg",
        "diorama-preview-private.jpg",
        "magicfit-still-private.jpg",
    ):
        (bundle_dir / generated_relpath).write_bytes(b"unmanifested-generated-data")
    (bundle_dir / "debug.log").write_text("debug-token", encoding="utf-8")
    (bundle_dir / "raw-payload.json").write_text('{"principal_id":"exec-public-tour"}', encoding="utf-8")
    (bundle_dir / "notes.txt").write_text("recipient@example.test", encoding="utf-8")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Kahlenberg Tour",
                "display_title": "Kahlenberg Tour",
                "variant_key": "layout_first",
                "variant_label": "layout first",
                "scene_count": 1,
                "principal_id": "exec-public-tour",
                "recipient_email": "recipient@example.test",
                "source_ref": "private-source-ref",
                "external_id": "external-private-id",
                "runtime_inputs_json": {"credential_hint": "do-not-serve"},
                "listing_url": "https://example.test/listing",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "facts": {
                    "rooms": 2,
                    "area_sqm": 58,
                    "total_rent_eur": 897,
                    "availability": "ab sofort",
                    "address_lines": ["1200 Wien"],
                    "exact_address": "Kahlenberger Strasse 1, 1190 Wien",
                    "street_address": "Kahlenberger Strasse 1",
                    "map_lat": 48.25,
                    "map_lng": 16.35,
                    "postal_name": "1190 Wien",
                    "teaser_attributes": ["Kahlenbergblick"],
                    "public_preference_snapshot": {
                        "profile": {"principal_id": "exec-public-tour"},
                        "preference_nodes": [{"key": "prefer_balcony", "value_json": True}],
                    },
                    "personal_fit_assessment": {
                        "fit_score": 81,
                        "good_fit_reasons": ["Strong layout signal"],
                        "preference_nodes": [{"key": "private-node"}],
                    },
                },
                "brief": {
                    "theme_name": "Calm daylight",
                    "tour_style": "layout first",
                    "audience": "flat hunters",
                    "creative_brief": "Lead with plan clarity at Kahlenberger Strasse 1.",
                    "call_to_action": "Book a viewing.",
                },
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "photo",
                        "image_url": "https://example.test/original.jpg",
                        "source_url": "https://example.test/original.jpg",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "principal_id": "exec-public-tour",
                "recipient_email": "recipient@example.test",
                "source_ref": "private-source-ref",
                "external_id": "external-private-id",
                "listing_url": "https://private.example.test/listing",
                "property_url": "https://private.example.test/property",
                "facts": {"exact_address": "Private Receipt Street 7, 1190 Wien"},
                "scenes": [{"name": "Private receipt asset", "role": "photo", "asset_relpath": "private-secret.jpg"}],
                "public_assets": [
                    {
                        "path": "private-secret.jpg",
                        "role": "photo",
                        "privacy_class": "public",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour")

    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})
    assert page.status_code == 200
    assert "Kahlenberg Tour" in page.text
    assert f"/tours/files/{slug}/scene-01.jpg" in page.text
    assert "ea.example" not in page.text
    assert "https://js.clickrank.ai/seo/33ff8f39-6213-4903-99d7-81048b5b3e1f/script?" not in page.text
    page_head = client.head(f"/tours/{slug}", follow_redirects=False)
    assert page_head.status_code == 200

    payload = client.get(f"/tours/{slug}.json")
    assert payload.status_code == 200
    payload_body = payload.json()
    assert payload_body["slug"] == slug
    assert payload_body["hosted_url"] == f"/tours/{slug}"
    assert payload_body["tour_privacy_mode"] == "anonymous_public"
    assert payload_body["scenes"][0]["image_url"] == f"/tours/files/{slug}/scene-01.jpg"
    assert "personal_fit_assessment" not in payload_body["facts"]
    assert payload_body["facts"]["postal_name"] == "1190 Wien"
    assert payload_body["public_assets"] == [
        {
            "mime_type": "image/jpeg",
            "path": "scene-01.jpg",
            "privacy_class": "public",
            "role": "photo",
            "sha256": hashlib.sha256(b"fake-jpeg-data").hexdigest(),
            "size_bytes": len(b"fake-jpeg-data"),
            "url": f"/tours/files/{slug}/scene-01.jpg",
        }
    ]
    serialized_payload = json.dumps(payload_body, sort_keys=True)
    assert "brief" not in payload_body
    for private_marker in (
        "principal_id",
        "recipient@example.test",
        "private-source-ref",
        "external-private-id",
        "runtime_inputs_json",
        "public_preference_snapshot",
        "personal_fit_assessment",
        "preference_nodes",
        "debug-token",
        "Kahlenberger Strasse",
        "map_lat",
        "map_lng",
        "address_lines",
        "exact_address",
        "street_address",
        "Kahlenberger Strasse 1",
        "Private Receipt Street",
        "private-secret",
        "private.example.test",
        "ea.example",
    ):
        assert private_marker not in serialized_payload

    asset = client.get(f"/tours/files/{slug}/scene-01.jpg")
    assert asset.status_code == 200
    assert asset.content == b"fake-jpeg-data"
    assert asset.headers["content-type"].startswith("image/jpeg")
    assert asset.headers["x-propertyquarry-asset-sha256"] == hashlib.sha256(b"fake-jpeg-data").hexdigest()
    assert asset.headers["x-propertyquarry-asset-privacy"] == "public"
    assert client.get(f"/tours/files/{slug}/tour.json").status_code == 404
    assert client.get(f"/tours/files/{slug}/raw-payload.json").status_code == 404
    assert client.get(f"/tours/files/{slug}/debug.log").status_code == 404
    assert client.get(f"/tours/files/{slug}/notes.txt").status_code == 404
    assert client.get(f"/tours/files/{slug}/private-secret.jpg").status_code == 404
    assert client.get(f"/tours/files/{slug}/tour.mp4").status_code == 404
    assert client.get(f"/tours/files/{slug}/telegram-preview-private.jpg").status_code == 404
    assert client.get(f"/tours/files/{slug}/diorama-preview-private.jpg").status_code == 404
    assert client.get(f"/tours/files/{slug}/magicfit-still-private.jpg").status_code == 404


def test_public_tour_routes_serve_provider_neutral_responsive_walkthrough_variants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "danube-flats-responsive-walkthrough"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "living-room.jpg").write_bytes(b"fake-jpeg-data")
    desktop_bytes = b"desktop-1080p60-video"
    mobile_bytes = b"mobile-720p60-video"
    (bundle_dir / "walkthrough-desktop.mp4").write_bytes(desktop_bytes)
    (bundle_dir / "walkthrough-mobile.mp4").write_bytes(mobile_bytes)
    (bundle_dir / "walkthrough-review.json").write_text(
        json.dumps(
            {
                "acceptance_status": "accepted",
                "launch_eligible": True,
            }
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Danube Flats",
                    "display_title": "Danube Flats",
                    "video_relpath": "walkthrough-desktop.mp4",
                    "video_mobile_relpath": "walkthrough-mobile.mp4",
                    "video_sidecar_relpath": "walkthrough-review.json",
                    "scenes": [
                    {
                        "name": "Living room",
                        "role": "photo",
                        "asset_relpath": "living-room.jpg",
                        "mime_type": "image/jpeg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    client = _client(principal_id="exec-public-tour-responsive-walkthrough")

    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})
    assert page.status_code == 200
    mobile_url = f"/tours/files/{slug}/walkthrough-mobile.mp4"
    desktop_url = f"/tours/{slug}/walkthrough"
    mobile_source = f'<source src="{mobile_url}"'
    desktop_source = f'<source src="{desktop_url}"'
    assert page.text.index(mobile_source) < page.text.index(desktop_source)
    assert 'media="(max-width: 760px)"' in page.text

    public_payload = client.get(f"/tours/{slug}.json").json()
    assert public_payload["video_url"] == f"/tours/files/{slug}/walkthrough-desktop.mp4"
    assert public_payload["video_mobile_url"] == mobile_url

    desktop = client.get(desktop_url)
    assert desktop.status_code == 200
    assert desktop.content == desktop_bytes
    mobile = client.get(mobile_url)
    assert mobile.status_code == 200
    assert mobile.content == mobile_bytes
    mobile_range = client.get(mobile_url, headers={"range": "bytes=0-5"})
    assert mobile_range.status_code == 206
    assert mobile_range.content == mobile_bytes[:6]


def test_public_tour_routes_serve_registered_preview_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "preview-asset-tour"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.jpg").write_bytes(b"scene-jpeg-data")
    (bundle_dir / "diorama-preview.png").write_bytes(b"diorama-preview-data")
    (bundle_dir / "telegram-preview.png").write_bytes(b"telegram-preview-data")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Preview Asset Tour",
                "display_title": "Preview Asset Tour",
                "scene_count": 1,
                "variant_key": "layout_first",
                "variant_label": "layout first",
                "tour_privacy_mode": "anonymous_public",
                "diorama_preview_relpath": "diorama-preview.png",
                "preview_relpath": "diorama-preview.png",
                "telegram_preview_relpath": "telegram-preview.png",
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "photo",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour")
    payload = client.get(f"/tours/{slug}.json")
    assert payload.status_code == 200
    body = payload.json()
    assert body["diorama_preview_url"] == f"/tours/files/{slug}/diorama-preview.png"
    assert body["preview_url"] == f"/tours/files/{slug}/diorama-preview.png"
    assert body["telegram_preview_url"] == f"/tours/files/{slug}/telegram-preview.png"
    public_assets = body["public_assets"]
    assert {row["path"] for row in public_assets} >= {
        "scene-01.jpg",
        "diorama-preview.png",
        "telegram-preview.png",
    }

    diorama_asset = client.get(f"/tours/files/{slug}/diorama-preview.png")
    assert diorama_asset.status_code == 200
    assert diorama_asset.content == b"diorama-preview-data"

    telegram_asset = client.get(f"/tours/files/{slug}/telegram-preview.png")
    assert telegram_asset.status_code == 200
    assert telegram_asset.content == b"telegram-preview-data"


def test_generated_reconstruction_bundle_does_not_publish_fake_tour_viewer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    slug = "generated-reconstruction-assets"
    bundle_dir = tmp_path / slug
    reconstruction_dir = bundle_dir / "generated-reconstruction"
    reconstruction_dir.mkdir(parents=True)
    (reconstruction_dir / "viewer.html").write_text(
        """<!doctype html>
<html>
  <body>
    <img src="source-floorplan.jpg" alt="Floorplan">
    <img src="photo-01.jpg" alt="Photo 1">
    <img src="photo-02.jpg" alt="Photo 2">
  </body>
</html>
""",
        encoding="utf-8",
    )
    (reconstruction_dir / "model.obj").write_text("o room\n", encoding="utf-8")
    (reconstruction_dir / "model.mtl").write_text("newmtl warm_plaster\n", encoding="utf-8")
    (reconstruction_dir / "generated-walkthrough.mp4").write_bytes(b"fake-mp4-data")
    (reconstruction_dir / "source-floorplan.jpg").write_bytes(b"floorplan-jpeg")
    (reconstruction_dir / "photo-01.jpg").write_bytes(b"photo-one-jpeg")
    (reconstruction_dir / "photo-02.jpg").write_bytes(b"photo-two-jpeg")
    route_labels = ["living room", "bedroom"]
    requested_style, style_scene, generated_style_fields, viewer_style_fields = (
        _generated_reconstruction_style_fixture(
            route_stop_count=len(route_labels),
        )
    )
    walkable_scene = {
        "kind": "generated_reconstruction_layout",
        "rooms": [
            {
                "label": "living room",
                "name": "living room",
                "position": {"x": 0.0, "y": 0.0, "z": 0.0},
                "focus": {"x": 0.5, "y": 1.4, "z": 0.5},
            },
            {
                "label": "bedroom",
                "name": "bedroom",
                "position": {"x": 1.0, "y": 0.0, "z": 0.0},
                "focus": {"x": 1.5, "y": 1.4, "z": 0.5},
            },
        ],
        "route": [
            {
                "label": "living room",
                "room": "living room",
                "focus": {"x": 0.5, "y": 1.4, "z": 0.5},
                "camera": {"x": -0.5, "y": 1.6, "z": 1.2},
            },
            {
                "label": "bedroom",
                "room": "bedroom",
                "focus": {"x": 1.5, "y": 1.4, "z": 0.5},
                "camera": {"x": 0.5, "y": 1.6, "z": 1.2},
            },
        ],
    }
    coverage_proof = {
        "status": "pass",
        "segments_expected": route_labels,
        "segments_visited": route_labels,
        "coverage_segments": [
            {"index": 1, "segment": "living room", "start": 0.0, "end": 5.0},
            {"index": 2, "segment": "bedroom", "start": 5.0, "end": 10.0},
        ],
    }
    (reconstruction_dir / "generated-walkthrough.quality.json").write_text(
        json.dumps(
            {
                "route_labels": route_labels,
                "walkthrough_coverage_proof": coverage_proof,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (reconstruction_dir / "reconstruction.json").write_text(
        json.dumps(
            {
                "provider": "propertyquarry_generated_reconstruction",
                "verified_provider_capture": False,
                "satisfies_verified_tour_gate": False,
                "requested_style": requested_style,
                "style_scene": style_scene,
                "geometry": {"wall_rect_count": 4},
                "room_dimensions_m": {"width": 5.0, "depth": 4.0, "height": 2.7},
                "walkable_scene": walkable_scene,
                "route_labels": route_labels,
                "viewer": {
                    "relpath": "viewer.html",
                    **viewer_style_fields,
                    "sha256": hashlib.sha256(
                        (reconstruction_dir / "viewer.html").read_bytes()
                    ).hexdigest(),
                },
                "walkthrough_route_labels": route_labels,
                "walkthrough": {"status": "generated", "coverage_proof": coverage_proof},
                "model": {
                    "obj_sha256": hashlib.sha256(
                        (reconstruction_dir / "model.obj").read_bytes()
                    ).hexdigest(),
                    "mtl_sha256": hashlib.sha256(
                        (reconstruction_dir / "model.mtl").read_bytes()
                    ).hexdigest(),
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "display_title": "Generated reconstruction assets",
                "principal_id": "exec-generated-reconstruction-private",
                "listing_url": "https://private.example.test/listing/generated",
                "property_url": "https://private.example.test/property/generated",
                "source_ref": "willhaben:private-generated-reconstruction",
                "external_id": "private-generated-external-id",
                "recipient_email": "owner-private@example.test",
                "facts": {
                    "rooms": 3,
                    "postal_name": "1190 Wien",
                    "exact_address": "Private Generated Street 9, 1190 Wien",
                    "address_lines": ["Private Generated Street 9", "1190 Wien"],
                    "map_lat": 48.2491,
                    "map_lng": 16.3567,
                },
                "video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
                "video_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
                "video_provider": "propertyquarry_generated_reconstruction",
                "video_provider_key": "propertyquarry_generated_reconstruction",
                "video_coverage_proof": "boundary_verified_frame_continuation",
                "generated_reconstruction": {
                    "provider": "propertyquarry_generated_reconstruction",
                    **generated_style_fields,
                    "viewer_relpath": "generated-reconstruction/viewer.html",
                    "model_relpath": "generated-reconstruction/model.obj",
                    "material_relpath": "generated-reconstruction/model.mtl",
                    "manifest_relpath": "generated-reconstruction/reconstruction.json",
                    "walkthrough_video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
                    "walkthrough_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
                    "floorplan_relpath": "generated-reconstruction/source-floorplan.jpg",
                    "photo_relpaths": [
                        "generated-reconstruction/photo-01.jpg",
                        "generated-reconstruction/photo-02.jpg",
                    ],
                    "route_labels": route_labels,
                    "room_stop_count": len(route_labels),
                    "walkthrough_route_labels": route_labels,
                    "walkthrough_stop_count": len(route_labels),
                    "walkable_scene": walkable_scene,
                    "walkthrough_coverage_proof": coverage_proof,
                    "verified_provider_capture": False,
                    "satisfies_verified_tour_gate": False,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "principal_id": "exec-generated-reconstruction-private",
                "recipient_email": "owner-private@example.test",
                "source_ref": "willhaben:private-generated-reconstruction",
                "external_id": "private-generated-external-id",
                "listing_url": "https://private.example.test/listing/generated",
                "property_url": "https://private.example.test/property/generated",
                "facts": {"exact_address": "Private Receipt Generated Street 11, 1190 Wien"},
                "public_assets": [
                    {
                        "path": "generated-reconstruction/private-source.jpg",
                        "role": "photo",
                        "privacy_class": "generated_reconstruction_public",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    client = _client(principal_id="exec-generated-reconstruction")

    payload = client.get(f"/tours/{slug}.json")
    assert payload.status_code == 200
    payload_body = payload.json()
    public_assets = payload_body["public_assets"]
    assert {row["path"] for row in public_assets} == {
        "generated-reconstruction/viewer.html",
        "generated-reconstruction/generated-walkthrough.mp4",
        "generated-reconstruction/source-floorplan.jpg",
        "generated-reconstruction/photo-01.jpg",
        "generated-reconstruction/photo-02.jpg",
    }
    serialized_payload = json.dumps(payload_body, sort_keys=True)
    assert payload_body["facts"]["rooms"] == 3
    assert payload_body["facts"]["postal_name"] == "1190 Wien"
    for private_marker in (
        "principal_id",
        "exec-generated-reconstruction-private",
        "owner-private@example.test",
        "willhaben:private-generated-reconstruction",
        "private-generated-external-id",
        "private.example.test",
        "listing_url",
        "property_url",
        "exact_address",
        "address_lines",
        "map_lat",
        "map_lng",
        "Private Generated Street",
        "Private Receipt Generated Street",
        "private-source",
    ):
        assert private_marker not in serialized_payload

    viewer = client.get(f"/tours/files/{slug}/generated-reconstruction/viewer.html", follow_redirects=False)
    assert viewer.status_code == 302
    assert viewer.headers["location"] == f"/tours/{slug}"

    viewer_shell = client.get(f"/tours/{slug}")
    assert viewer_shell.status_code == 200
    assert "PropertyQuarry styled 3D reconstruction" in viewer_shell.text
    assert "Generated reconstruction" in viewer_shell.text
    assert "Room route" in viewer_shell.text
    for private_marker in (
        "owner-private@example.test",
        "willhaben:private-generated-reconstruction",
        "private-generated-external-id",
        "private.example.test",
        "Private Generated Street",
        "Private Receipt Generated Street",
    ):
        assert private_marker not in viewer_shell.text

    assert client.get(f"/tours/files/{slug}/generated-reconstruction/model.obj").status_code == 410
    assert client.get(f"/tours/files/{slug}/generated-reconstruction/model.mtl").status_code == 410

    floorplan = client.get(f"/tours/files/{slug}/generated-reconstruction/source-floorplan.jpg")
    assert floorplan.status_code == 200
    assert floorplan.content == b"floorplan-jpeg"
    assert floorplan.headers["x-propertyquarry-asset-privacy"] == "generated_reconstruction_public"

    photo_one = client.get(f"/tours/files/{slug}/generated-reconstruction/photo-01.jpg")
    assert photo_one.status_code == 200
    assert photo_one.content == b"photo-one-jpeg"
    assert photo_one.headers["x-propertyquarry-asset-privacy"] == "generated_reconstruction_public"

    photo_two = client.get(f"/tours/files/{slug}/generated-reconstruction/photo-02.jpg")
    assert photo_two.status_code == 200
    assert photo_two.content == b"photo-two-jpeg"
    assert photo_two.headers["x-propertyquarry-asset-privacy"] == "generated_reconstruction_public"
    assert client.get(f"/tours/files/{slug}/tour.json").status_code == 404
    assert client.get(f"/tours/files/{slug}/tour.private.json").status_code == 404
    assert client.get(f"/tours/files/{slug}/generated-reconstruction/private-source.jpg").status_code == 404


def test_generated_reconstruction_viewer_falls_back_when_provider_control_is_retired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    slug = "generated-reconstruction-with-real-tour"
    bundle_dir = tmp_path / slug
    reconstruction_dir = bundle_dir / "generated-reconstruction"
    reconstruction_dir.mkdir(parents=True)
    (reconstruction_dir / "viewer.html").write_text("<!doctype html><html><body>stale viewer</body></html>", encoding="utf-8")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "display_title": "Real Matterport tour",
                "source_virtual_tour_url": "https://my.matterport.com/show/?m=BmVWxvZQZLq",
                "generated_reconstruction": {
                    "provider": "propertyquarry_generated_reconstruction",
                    "viewer_relpath": "generated-reconstruction/viewer.html",
                    "verified_provider_capture": False,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    client = _client(principal_id="exec-generated-reconstruction-real-tour")

    viewer = client.get(f"/tours/files/{slug}/generated-reconstruction/viewer.html", follow_redirects=False)

    assert viewer.status_code == 302
    assert viewer.headers["location"] == f"/tours/{slug}"


def _write_shell_ready_generated_reconstruction_fixture(bundle_dir: Path) -> dict[str, object]:
    reconstruction_dir = bundle_dir / "generated-reconstruction"
    reconstruction_dir.mkdir(parents=True)
    (reconstruction_dir / "viewer.html").write_text("<!doctype html><html><body>viewer</body></html>", encoding="utf-8")
    (reconstruction_dir / "model.obj").write_text(
        "o propertyquarry_generated_layout\nv 0 0 0\n",
        encoding="utf-8",
    )
    (reconstruction_dir / "model.mtl").write_text(
        "newmtl walls\nKd 0.8 0.8 0.8\n",
        encoding="utf-8",
    )
    (reconstruction_dir / "generated-walkthrough.mp4").write_bytes(b"fake-mp4")
    (reconstruction_dir / "generated-walkthrough.quality.json").write_text(
        json.dumps(
            {
                "walkthrough_coverage_proof": {
                    "status": "pass",
                    "segments_expected": ["entry/hall", "living room", "bedroom"],
                    "segments_visited": ["entry/hall", "living room", "bedroom"],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (reconstruction_dir / "source-floorplan.jpg").write_bytes(b"floorplan")
    (reconstruction_dir / "photo-01.jpg").write_bytes(b"photo-1")
    (reconstruction_dir / "photo-02.jpg").write_bytes(b"photo-2")
    route_labels = ["entry/hall", "living room", "bedroom"]
    requested_style, style_scene, generated_style_fields, viewer_style_fields = (
        _generated_reconstruction_style_fixture(
            route_stop_count=len(route_labels),
        )
    )
    walkable_scene = {
        "kind": "generated_reconstruction_layout",
        "rooms": [
            {
                "label": label,
                "name": label,
                "position": {"x": float(index), "y": 0.0, "z": 0.0},
                "focus": {"x": float(index) + 0.4, "y": 1.4, "z": 0.5},
            }
            for index, label in enumerate(route_labels)
        ],
        "route": [
            {
                "label": label,
                "room": label,
                "focus": {"x": float(index) + 0.4, "y": 1.4, "z": 0.5},
                "camera": {"x": float(index) - 0.4, "y": 1.6, "z": 1.1},
            }
            for index, label in enumerate(route_labels)
        ],
    }
    coverage_proof = {
        "status": "pass",
        "segments_expected": route_labels,
        "segments_visited": route_labels,
        "coverage_segments": [
            {"index": index + 1, "segment": label, "start": float(index * 5), "end": float((index + 1) * 5)}
            for index, label in enumerate(route_labels)
        ],
    }
    (reconstruction_dir / "reconstruction.json").write_text(
        json.dumps(
            {
                "provider": "propertyquarry_generated_reconstruction",
                "verified_provider_capture": False,
                "satisfies_verified_tour_gate": False,
                "requested_style": requested_style,
                "style_scene": style_scene,
                "geometry": {"wall_rect_count": 4},
                "room_dimensions_m": {"width": 6.0, "depth": 5.0, "height": 2.7},
                "walkable_scene": walkable_scene,
                "route_labels": route_labels,
                "viewer": {
                    "relpath": "viewer.html",
                    **viewer_style_fields,
                    "sha256": hashlib.sha256(
                        (reconstruction_dir / "viewer.html").read_bytes()
                    ).hexdigest(),
                },
                "walkthrough_route_labels": route_labels,
                "walkthrough": {"status": "generated", "coverage_proof": coverage_proof},
                "model": {
                    "obj_sha256": hashlib.sha256(
                        (reconstruction_dir / "model.obj").read_bytes()
                    ).hexdigest(),
                    "mtl_sha256": hashlib.sha256(
                        (reconstruction_dir / "model.mtl").read_bytes()
                    ).hexdigest(),
                    "glb_export": {
                        "status": "failed",
                        "reason": "test_fixture_no_glb",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "provider": "propertyquarry_generated_reconstruction",
        **generated_style_fields,
        "viewer_relpath": "generated-reconstruction/viewer.html",
        "manifest_relpath": "generated-reconstruction/reconstruction.json",
        "model_relpath": "generated-reconstruction/model.obj",
        "material_relpath": "generated-reconstruction/model.mtl",
        "glb_model_relpath": "",
        "glb_export_status": "failed",
        "walkthrough_video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
        "walkthrough_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
        "floorplan_relpath": "generated-reconstruction/source-floorplan.jpg",
        "photo_relpaths": [
            "generated-reconstruction/photo-01.jpg",
            "generated-reconstruction/photo-02.jpg",
        ],
        "route_labels": route_labels,
        "room_stop_count": len(route_labels),
        "walkthrough_route_labels": route_labels,
        "walkthrough_stop_count": len(route_labels),
        "walkable_scene": walkable_scene,
        "walkthrough_coverage_proof": coverage_proof,
        "verified_provider_capture": False,
        "satisfies_verified_tour_gate": False,
    }


def test_public_tour_page_rejects_generated_reconstruction_launch_shell(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    slug = "generated-reconstruction-launch"
    bundle_dir = tmp_path / slug
    generated_reconstruction = _write_shell_ready_generated_reconstruction_fixture(bundle_dir)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "display_title": "Generated reconstruction launch",
                "video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
                "video_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
                "video_provider": "propertyquarry_generated_reconstruction",
                "video_provider_key": "propertyquarry_generated_reconstruction",
                "video_coverage_proof": "boundary_verified_frame_continuation",
                "generated_reconstruction": generated_reconstruction,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    client = _client(principal_id="exec-generated-reconstruction-launch")

    response = client.get(f"/tours/{slug}", follow_redirects=False)

    assert response.status_code == 404
    layout_preview = client.get(f"/tours/{slug}/layout-preview", follow_redirects=False)
    assert layout_preview.status_code == 200
    assert "PropertyQuarry styled 3D reconstruction" in layout_preview.text


def test_public_tour_page_rejects_generated_reconstruction_walkthrough_on_autoplay(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    slug = "generated-reconstruction-walkthrough-launch"
    bundle_dir = tmp_path / slug
    generated_reconstruction = _write_shell_ready_generated_reconstruction_fixture(bundle_dir)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "display_title": "Generated reconstruction walkthrough launch",
                "video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
                "video_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
                "video_provider": "propertyquarry_generated_reconstruction",
                "video_provider_key": "propertyquarry_generated_reconstruction",
                "video_coverage_proof": "boundary_verified_frame_continuation",
                "generated_reconstruction": generated_reconstruction,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    client = _client(principal_id="exec-generated-reconstruction-walkthrough-launch")

    response = client.get(f"/tours/{slug}?autoplay=1", follow_redirects=False)

    assert response.status_code == 404
    walkthrough = client.get(f"/tours/{slug}/walkthrough", follow_redirects=False)
    assert walkthrough.status_code == 404
    layout_preview = client.get(f"/tours/{slug}/layout-preview", follow_redirects=False)
    assert layout_preview.status_code == 200
    assert "PropertyQuarry styled 3D reconstruction" in layout_preview.text


def test_public_tour_json_never_exposes_listing_or_source_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "public-json-url-redaction"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.jpg").write_bytes(b"public-scene")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "display_title": "Public JSON redaction",
                "hosted_url": "https://private-host.example/tours/private",
                "public_url": "javascript:alert(1)",
                "listing_url": "https://portal.example/private-listing",
                "property_url": "https://broker.example/private-property",
                "source_url": "https://source.example/private-source",
                "source_virtual_tour_url": "https://360.kalandra.at/view/portal/id/private-live-360",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/private-live-360",
                "matterport_url": "https://my.matterport.com/show/?m=PrivateMatterport",
                "three_d_vista_url": "https://example.3dvista.com/private/index.html",
                "panorama_source": "private-source-host",
                "principal_id": "principal-private",
                "source_ref": "source-ref-private",
                "recipient_email": "recipient@example.test",
                "facts": {
                    "rooms": 3,
                    "area_sqm": 84,
                    "postal_name": "1020 Wien",
                    "exact_address": "Private Street 9",
                    "map_lat": 48.22,
                    "map_lng": 16.39,
                    "source_url": "https://facts.example/private",
                },
                "scenes": [
                    {
                        "name": "Public scene",
                        "role": "photo",
                        "asset_relpath": "scene-01.jpg",
                        "source_url": "https://cdn.example/private-original.jpg",
                        "property_url": "https://broker.example/private-property",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    client = _client(principal_id="public-tour-json-redaction")

    response = client.get(f"/tours/{slug}.json")

    assert response.status_code == 200
    body = response.json()
    serialized = json.dumps(body, sort_keys=True)
    assert body["hosted_url"] == f"/tours/{slug}"
    assert body["public_url"] == f"/tours/{slug}"
    assert body["facts"] == {"rooms": 3, "area_sqm": 84, "postal_name": "1020 Wien"}
    assert body["scenes"][0]["image_url"] == f"/tours/files/{slug}/scene-01.jpg"
    for private_marker in (
        "listing_url",
        "property_url",
        "source_url",
        "source_virtual_tour_url",
        "source_virtual_tour_origin",
        "matterport_url",
        "three_d_vista_url",
        "panorama_source",
        "portal.example",
        "broker.example",
        "source.example",
        "360.kalandra.at",
        "matterport.com",
        "3dvista",
        "principal-private",
        "source-ref-private",
        "recipient@example.test",
        "Private Street",
        "map_lat",
        "map_lng",
        "private-original",
        "private-host.example",
        "javascript:",
    ):
        assert private_marker not in serialized


def test_public_tour_canonical_path_rejects_unsafe_stored_slugs() -> None:
    from app.api.routes.public_tour_payloads import public_tour_canonical_path

    assert public_tour_canonical_path("kahlenberg-layout-first") == "/tours/kahlenberg-layout-first"
    assert public_tour_canonical_path("../private-tour") == ""
    assert public_tour_canonical_path("private/tour") == ""
    assert public_tour_canonical_path("private\\tour") == ""


def test_authenticated_public_tour_revocation_removes_served_manifest_and_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    slug = "revocable-kahlenberg-tour"
    owner_principal = "exec-public-tour-owner"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.jpg").write_bytes(b"served-scene")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Revocable Kahlenberg Tour",
                "display_title": "Revocable Kahlenberg Tour",
                "scene_count": 1,
                "principal_id": owner_principal,
                "hosted_url": f"https://propertyquarry.com/tours/{slug}",
                "facts": {"rooms": 2, "postal_name": "1190 Wien"},
                "scenes": [
                    {
                        "name": "Scene",
                        "role": "photo",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.private.json").write_text(
        json.dumps({"principal_id": owner_principal, "listing_url": "https://private.example.test/listing"}),
        encoding="utf-8",
    )

    intruder = _client(principal_id="exec-public-tour-other")
    denied = intruder.post(f"/app/api/property/public-tours/{slug}/revoke")
    assert denied.status_code == 404
    assert bundle_dir.exists()

    owner = _client(principal_id=owner_principal)
    assert owner.get(f"/tours/{slug}.json").status_code == 200
    assert owner.get(f"/tours/files/{slug}/scene-01.jpg").status_code == 200

    revoked = owner.post(f"/app/api/property/public-tours/{slug}/revoke")
    assert revoked.status_code == 200, revoked.text
    body = revoked.json()
    assert body["status"] == "revoked"
    assert body["slug"] == slug
    assert body["removed_file_count"] >= 3
    assert not bundle_dir.exists()
    receipt_path = tmp_path / ".revocations" / f"{slug}.json"
    assert receipt_path.exists()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "revoked"
    assert receipt["principal_id_sha256"]
    assert "private.example.test" not in json.dumps(receipt)

    assert owner.get(f"/tours/{slug}").status_code == 410
    assert owner.get(f"/tours/{slug}.json").status_code == 410
    assert owner.get(f"/tours/files/{slug}/scene-01.jpg").status_code == 410
    assert owner.get(f"/tours/.revocations").status_code == 404


def test_public_tour_removed_cube_file_gate_respects_privacy_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    private_slug = "owner-private-removed-cube"
    private_bundle_dir = tmp_path / private_slug
    private_bundle_dir.mkdir(parents=True)
    (private_bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": private_slug,
                "tour_privacy_mode": "owner_private",
                "cube_fallback_removed": True,
                "cube_fallback_policy": "private policy must not be exposed",
                "removed_cube_assets": ["pq-3d-top22.html"],
            }
        ),
        encoding="utf-8",
    )
    public_slug = "anonymous-public-removed-cube"
    public_bundle_dir = tmp_path / public_slug
    public_bundle_dir.mkdir(parents=True)
    (public_bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": public_slug,
                "tour_privacy_mode": "anonymous_public",
                "cube_fallback_removed": True,
                "cube_fallback_policy": "public removal notice",
                "removed_cube_assets": ["pq-3d-top22.html"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour")

    private_marker = client.get(f"/tours/files/{private_slug}/CUBE_FALLBACK_REMOVED.txt")
    assert private_marker.status_code == 404
    assert "private policy" not in private_marker.text
    private_removed_asset = client.get(f"/tours/files/{private_slug}/pq-3d-top22.html")
    assert private_removed_asset.status_code == 404
    assert "private policy" not in private_removed_asset.text

    public_marker = client.get(f"/tours/files/{public_slug}/CUBE_FALLBACK_REMOVED.txt")
    assert public_marker.status_code == 404
    assert "public removal notice" not in public_marker.text
    public_removed_asset = client.get(f"/tours/files/{public_slug}/pq-3d-top22.html")
    assert public_removed_asset.status_code == 410
    assert "This tour asset is no longer available." in public_removed_asset.text
    assert "cube fallback" not in public_removed_asset.text.lower()


def test_public_tour_routes_drop_untrusted_external_scene_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "external-image-only-tour"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "External Image Only",
                "display_title": "External Image Only",
                "facts": {"area_sqm": 58},
                "scenes": [
                    {
                        "name": "Remote image",
                        "role": "photo",
                        "image_url": "https://untrusted.invalid/scene.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour")
    payload = client.get(f"/tours/{slug}.json")

    assert payload.status_code == 200
    assert payload.json()["scenes"] == []


def test_public_tour_routes_render_pdf_floorplan_scenes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "auction-floorplan-tour"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "floorplan-01.pdf").write_bytes(b"%PDF-1.7 floorplan")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Auction Floorplan Tour",
                "display_title": "Auction Floorplan Tour",
                "variant_key": "layout_first",
                "variant_label": "floorplan",
                "scene_count": 1,
                "listing_url": "https://edikte2.justiz.gv.at/example",
                "hosted_url": f"https://propertyquarry.com/tours/{slug}",
                "facts": {"area_sqm": 126.5, "address_lines": ["1020 Vienna"], "has_floorplan": True},
                "brief": {"tour_style": "hosted floorplan review"},
                "scenes": [
                    {
                        "name": "Valuation PDF",
                        "role": "floorplan",
                        "asset_relpath": "floorplan-01.pdf",
                        "mime_type": "application/pdf",
                        "privacy_class": "floorplan_pdf_public",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour")

    page = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"})
    assert page.status_code == 200
    assert f"/tours/files/{slug}/floorplan-01.pdf" in page.text
    assert 'id="stage-frame"' in page.text
    assert "thumb-doc" in page.text
    assert '"mime_type":"application/pdf"' in page.text
    payload = client.get(f"/tours/{slug}.json")
    assert payload.status_code == 200
    assert payload.json()["public_assets"] == [
        {
            "mime_type": "application/pdf",
            "path": "floorplan-01.pdf",
            "privacy_class": "floorplan_pdf_public",
            "role": "floorplan",
            "sha256": hashlib.sha256(b"%PDF-1.7 floorplan").hexdigest(),
            "size_bytes": len(b"%PDF-1.7 floorplan"),
            "url": f"/tours/files/{slug}/floorplan-01.pdf",
        }
    ]

    asset = client.get(f"/tours/files/{slug}/floorplan-01.pdf")
    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("application/pdf")
    assert asset.headers["x-propertyquarry-asset-privacy"] == "floorplan_pdf_public"


def test_public_tour_routes_deny_pdf_without_floorplan_public_privacy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "private-pdf-tour"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "valuation.pdf").write_bytes(b"%PDF-1.7 private valuation")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Private PDF Tour",
                "display_title": "Private PDF Tour",
                "facts": {"area_sqm": 126.5, "has_floorplan": True},
                "scenes": [
                    {
                        "name": "Valuation PDF",
                        "role": "floorplan",
                        "asset_relpath": "valuation.pdf",
                        "mime_type": "application/pdf",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour")
    payload = client.get(f"/tours/{slug}.json")

    assert payload.status_code == 200
    assert payload.json()["scenes"] == []
    assert client.get(f"/tours/files/{slug}/valuation.pdf").status_code == 404


def test_public_results_no_longer_shadow_tour_routes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_RESULTS", "1")
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_MYEXTERNALBRAIN_SITE_ID", "33ff8f39-6213-4903-99d7-81048b5b3e1f")
    result_dir = tmp_path / "results"
    result_bundle = result_dir / "movie-demo"
    result_bundle.mkdir(parents=True)
    (result_bundle / "asset.html").write_text("<html><body>movie</body></html>", encoding="utf-8")
    (result_bundle / "result.json").write_text(
        json.dumps(
            {
                "slug": "movie-demo",
                "title": "Movie Demo",
                "service_key": "mootion_movie",
                "summary": "Demo movie",
                "body_text": "Demo movie",
                "mime_type": "text/html",
                "viewer_kind": "html",
                "asset_relpath": "asset.html",
                "hosted_url": "https://ea.example/results/movie-demo",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_RESULT_DIR", str(result_dir))
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path / "tours"))

    client = _client(principal_id="exec-public-result")

    result_page = client.get("/results/movie-demo", headers={"host": "myexternalbrain.com"})
    assert result_page.status_code == 200
    assert "Movie Demo" in result_page.text
    assert "https://js.clickrank.ai/seo/33ff8f39-6213-4903-99d7-81048b5b3e1f/script?" not in result_page.text

    missing_tour = client.get("/tours/movie-demo")
    assert missing_tour.status_code == 404
    assert "This tour link is no longer available." in missing_tour.text
    assert "Request a fresh tour" in missing_tour.text
    assert "workspace" not in missing_tour.text.lower()
    assert "queue" not in missing_tour.text.lower()
    assert "office" not in missing_tour.text.lower()
    missing_tour_payload = client.get("/tours/movie-demo.json")
    assert missing_tour_payload.status_code == 404
    assert missing_tour_payload.json()["error"]["code"] == "tour_not_found"


def test_public_tour_routes_embed_live_360_source_when_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_360_ALLOWED_HOSTS", "360.example.test")
    slug = "pioche-lecombe-live-360"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Pioche Lecombe Live 360",
                "display_title": "Pioche Lecombe Live 360",
                "listing_url": "https://www.willhaben.at/listing/live-360",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "source_virtual_tour_url": "https://360.example.test/view/portal/id/live-360",
                "panorama_source": "feelestate_kalandra",
                "brand_name": "Pioche Lecombe",
                "scene_count": 1,
                "facts": {
                    "rooms": 3,
                    "area_sqm": 81,
                    "total_rent_eur": 1490,
                    "availability": "sofort",
                    "address_lines": ["Währing, Wien"],
                    "teaser_attributes": ["360 Tour"],
                },
                "brief": {
                    "theme_name": "White-label 360",
                    "tour_style": "panorama first",
                    "audience": "buyers",
                    "creative_brief": "Lead with the real panorama viewer.",
                    "call_to_action": "Book a viewing.",
                },
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "photo",
                        "image_url": "https://example.test/original.jpg",
                        "source_url": "https://example.test/original.jpg",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-live-360")
    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})

    assert page.status_code == 200
    assert "Pioche Lecombe" in page.text
    assert 'src="https://360.example.test/view/portal/id/live-360"' in page.text
    assert 'href="#live-360"' in page.text
    assert "Open 3D tour" in page.text
    assert "Interactive tour" in page.text
    assert "Open Live 360" not in page.text
    assert "Live Panorama Viewer" not in page.text
    assert "Hosted on myexternalbrain.com" not in page.text
    assert "Open Source 360" not in page.text
    assert ">Source<" not in page.text


def test_public_tour_routes_retire_matterport_media_and_interactive_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "matterport-live-360"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Matterport Live 360",
                "display_title": "Matterport Live 360",
                "listing_url": "https://www.immobilienscout24.at/expose/matterport-live-360",
                "hosted_url": f"https://propertyquarry.com/tours/{slug}",
                "source_virtual_tour_url": "https://my.matterport.com/show/?m=BmVWxvZQZLq",
                "source_virtual_tour_origin": "https://my.matterport.com/show/?m=BmVWxvZQZLq",
                "panorama_source": "my.matterport.com",
                "brand_name": "PropertyQuarry",
                "scene_strategy": "live_360_embed",
                "scene_count": 1,
                "facts": {"has_360": True, "teaser_attributes": ["Live 360 tour"]},
                "brief": {"theme_name": "Matterport", "tour_style": "live 360", "audience": "buyers", "creative_brief": "Lead with live 360.", "call_to_action": "Open live 360."},
                "scenes": [
                    {
                        "name": "Live 360",
                        "role": "live_360",
                        "image_url": "https://my.matterport.com/api/v2/player/models/BmVWxvZQZLq/thumb/",
                        "source_url": "https://my.matterport.com/show/?m=BmVWxvZQZLq",
                        "mime_type": "image/jpeg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-matterport-live-360")
    entry = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"}, follow_redirects=False)
    page = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"})

    assert entry.status_code == 200
    assert page.status_code == 200
    assert "Matterport Live 360" in page.text
    assert 'src="https://my.matterport.com/api/v2/player/models/BmVWxvZQZLq/thumb/"' not in page.text
    assert "my.matterport.com" not in page.text
    assert 'src="https://my.matterport.com/show/?m=BmVWxvZQZLq"' not in page.text
    assert f"/tours/{slug}/control/matterport" not in page.text
    assert "Open 3D tour" not in page.text
    assert "Open fullscreen" not in page.text
    assert "Property Tour" not in page.text
    assert "Load 3D tour" not in page.text


def _reviewed_3dvista_target_provenance(
    *,
    slug: str,
    export_dir: Path | None = None,
    entry_relpath: str = "",
    provider_url: str = "",
) -> dict[str, object]:
    from scripts.property_tour_3dvista_provenance import (
        THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
        export_tree_sha256,
        sha256_text,
    )

    if export_dir is not None:
        artifact = {
            "kind": "local_export",
            "sha256": export_tree_sha256(export_dir),
            "entry_relpath": entry_relpath,
        }
    else:
        artifact = {
            "kind": "hosted_url",
            "sha256": sha256_text(provider_url),
        }
    proof: dict[str, object] = {
        "schema": THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
        "status": "pass",
        "provider": "3dvista",
        "target_slug": slug,
        "artifact": artifact,
        "authorization": {
            "status": "approved",
            "reference": "test-fixture-reviewed-export",
        },
        "review": {
            "property_match": "pass",
            "visual_match": "pass",
            "reviewed_by": "test-suite",
            "reviewed_at": "2026-01-01T00:00:00Z",
        },
    }
    if export_dir is not None:
        proof["target_subdir"] = export_dir.name
    return proof


def test_public_tour_routes_expose_propertyquarry_3dvista_private_viewer_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "propertyquarry-private-viewer-3dvista"
    bundle_dir = tmp_path / slug
    vista_dir = bundle_dir / "3dvista"
    vista_dir.mkdir(parents=True)
    (vista_dir / "index.htm").write_text("<html><script src='tdvplayer.js'></script></html>", encoding="utf-8")
    (vista_dir / "locale").mkdir()
    (vista_dir / "locale" / "en.txt").write_text("#: locale=en\n", encoding="utf-8")
    (vista_dir / "runtime.wasm").write_bytes(b"\0asm\x01\0\0\0")
    (vista_dir / "media" / "panorama_living_room").mkdir(parents=True)
    (vista_dir / "media" / "panorama_kitchen").mkdir(parents=True)
    (bundle_dir / "walkthrough.mp4").write_bytes(b"video")
    (bundle_dir / "scene-01.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_target_provenance": _reviewed_3dvista_target_provenance(
                    slug=slug,
                    export_dir=vista_dir,
                    entry_relpath="index.htm",
                )
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Private Viewer 3DVista",
                "display_title": "Private Viewer 3DVista",
                "hosted_url": f"https://propertyquarry.com/tours/{slug}",
                "scene_strategy": "walkable_panorama",
                "creation_mode": "hosted_walkable_360",
                "video_relpath": "walkthrough.mp4",
                "video_provider": "magicfit",
                "video_coverage_proof": "boundary_verified_frame_continuation",
                "scenes": [
                    {
                        "name": "Panorama",
                        "role": "photo",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_3dvista_private_receipt(
        bundle_dir,
        slug=slug,
        browser_rendered=True,
        export_dir=vista_dir,
        entry_relpath="3dvista/index.htm",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-private-viewer-3dvista")
    entry = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"}, follow_redirects=False)
    page = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"})
    control = client.get(f"/tours/{slug}/control/3dvista", headers={"host": "propertyquarry.com"})
    locale = client.get(f"/tours/3dvista/{slug}/3dvista/locale/en.txt", headers={"host": "propertyquarry.com"})
    wasm = client.get(f"/tours/3dvista/{slug}/3dvista/runtime.wasm", headers={"host": "propertyquarry.com"})

    assert entry.status_code == 302
    assert entry.headers["location"] == f"/tours/{slug}/control/3dvista"
    assert page.status_code == 200
    assert f'src="/tours/3dvista/{slug}/3dvista/index.htm"' in page.text
    assert "Property Tour" not in page.text
    assert "Load 3D tour" not in page.text
    assert control.status_code == 200
    assert "3D Tour" in control.text
    assert "3DVista Control" not in control.text
    assert "Property Tour" not in control.text
    assert "Open fullscreen" not in control.text
    assert f"/tours/3dvista/{slug}/3dvista/index.htm" in control.text
    assert locale.status_code == 200
    assert wasm.status_code == 200


def test_public_tour_routes_hide_forced_panorama_controls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("KRPANO_LICENSE_DOMAIN", "propertyquarry.com")
    monkeypatch.setenv("KRPANO_LICENSE_KEY", "test-license")
    slug = "propertyquarry-panorama-hidden"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Hidden panorama fallback",
                "display_title": "Hidden panorama fallback",
                "hosted_url": f"https://propertyquarry.com/tours/{slug}",
                "scene_strategy": "walkable_panorama",
                "creation_mode": "hosted_walkable_360",
                "walkable_scene": {
                    "projection": "equirectangular",
                    "panorama_relpath": "panorama.jpg",
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-panorama-hidden")
    krpano = client.get(f"/tours/{slug}/control/krpano", headers={"host": "propertyquarry.com"})
    pano2vr = client.get(f"/tours/{slug}/control/pano2vr", headers={"host": "propertyquarry.com"})

    assert krpano.status_code == 404
    assert krpano.json()["error"]["code"] == "tour_control_panorama_export_hidden"
    assert pano2vr.status_code == 404
    assert pano2vr.json()["error"]["code"] == "tour_control_panorama_export_hidden"


def test_public_tour_hides_3dvista_link_without_browser_render_proof_but_keeps_probe_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "propertyquarry-private-viewer-3dvista-unrendered"
    bundle_dir = tmp_path / slug
    vista_dir = bundle_dir / "3dvista"
    vista_dir.mkdir(parents=True)
    (vista_dir / "index.htm").write_text("<html><script src='tdvplayer.js'></script><div>tourviewer</div></html>", encoding="utf-8")
    (vista_dir / "tdvplayer.js").write_text("window.TDVPlayer = true;", encoding="utf-8")
    (vista_dir / "viewer.woff2").write_bytes(b"font-data")
    (bundle_dir / "scene-01.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_target_provenance": _reviewed_3dvista_target_provenance(
                    slug=slug,
                    export_dir=vista_dir,
                    entry_relpath="index.htm",
                )
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Unrendered 3DVista",
                "display_title": "Unrendered 3DVista",
                "hosted_url": f"https://propertyquarry.com/tours/{slug}",
                "walkable_scene": {"representation_kind": "ai_reconstruction"},
                "scenes": [
                    {
                        "name": "Panorama",
                        "role": "photo",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_3dvista_private_receipt(
        bundle_dir,
        slug=slug,
        browser_rendered=False,
        export_dir=vista_dir,
        entry_relpath="3dvista/index.htm",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-private-viewer-3dvista-unrendered")
    page = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"})
    control = client.get(f"/tours/{slug}/control/3dvista", headers={"host": "propertyquarry.com"})
    asset = client.get(f"/tours/3dvista/{slug}/3dvista/index.htm", headers={"host": "propertyquarry.com"})
    font = client.get(f"/tours/3dvista/{slug}/3dvista/viewer.woff2", headers={"host": "propertyquarry.com"})

    assert page.status_code == 200
    assert "Open 3D tour" not in page.text
    assert f"/tours/{slug}/control/3dvista" not in page.text
    assert control.status_code == 200
    assert f"/tours/3dvista/{slug}/3dvista/index.htm" in control.text
    assert asset.status_code == 200
    assert font.status_code == 200


def test_public_tour_fullscreen_provider_shell_has_accessible_main_and_frame() -> None:
    from app.api.routes.public_tours import _tour_control_external_iframe_html

    body = _tour_control_external_iframe_html(
        title="Accessible 3DVista",
        iframe_src="/tours/3dvista/accessible-tour/3dvista/index.htm",
        badge="3D Tour",
        payload={"slug": "accessible-tour"},
        nonce="test-nonce",
    )

    assert '<main class="provider-frame-wrap"' in body
    assert '<iframe class="provider-frame"' in body
    assert 'title="Accessible 3DVista"' in body
    assert 'aria-label="3D Tour: Accessible 3DVista"' in body


def test_public_tour_hides_external_3dvista_without_browser_render_proof_but_keeps_probe_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "external-3dvista-unrendered"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.jpg").write_bytes(b"fake-jpeg-data")
    external_url = "https://show.3dvista.com/propertyquarry/unrendered"
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_target_provenance": _reviewed_3dvista_target_provenance(
                    slug=slug,
                    provider_url=external_url,
                )
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "External 3DVista without proof",
                "display_title": "External 3DVista without proof",
                "hosted_url": f"https://propertyquarry.com/tours/{slug}",
                "scenes": [
                    {
                        "name": "Panorama preview",
                        "role": "photo",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_3dvista_private_receipt(
        bundle_dir,
        slug=slug,
        browser_rendered=False,
        provider_url=external_url,
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-external-3dvista-unrendered")
    page = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"})
    control = client.get(f"/tours/{slug}/control/3dvista", headers={"host": "propertyquarry.com"})

    assert page.status_code == 200
    assert external_url not in page.text
    assert "Open 3D tour" not in page.text
    assert f"/tours/{slug}/control/3dvista" not in page.text
    assert control.status_code == 200
    assert external_url in control.text


def test_public_tour_exposes_external_3dvista_only_with_browser_render_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "external-3dvista-rendered"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.jpg").write_bytes(b"fake-jpeg-data")
    external_url = "https://show.3dvista.com/propertyquarry/rendered"
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_target_provenance": _reviewed_3dvista_target_provenance(
                    slug=slug,
                    provider_url=external_url,
                )
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "External 3DVista with proof",
                "display_title": "External 3DVista with proof",
                "hosted_url": f"https://propertyquarry.com/tours/{slug}",
                "scenes": [
                    {
                        "name": "Panorama preview",
                        "role": "photo",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_3dvista_private_receipt(
        bundle_dir,
        slug=slug,
        browser_rendered=True,
        provider_url=external_url,
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-external-3dvista-rendered")
    entry = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"}, follow_redirects=False)
    page = client.get(
        f"/tours/{slug}?pane=overview-pane",
        headers={"host": "propertyquarry.com"},
    )

    assert entry.status_code == 302
    assert entry.headers["location"] == f"/tours/{slug}/control/3dvista"
    assert page.status_code == 200
    assert f'href="/tours/{slug}/control/3dvista"' in page.text
    assert external_url not in page.text
    assert "Open 3D tour" in page.text
    assert f"/tours/{slug}/control/3dvista" in page.text


def test_public_tour_routes_render_pure_360_cube_with_continuing_links(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_MYEXTERNALBRAIN_SITE_ID", "33ff8f39-6213-4903-99d7-81048b5b3e1f")
    slug = "pioche-lecombe-pure-360"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01-f.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-l.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-r.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-u.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-d.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-b.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-02-f.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-02-l.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-02-r.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-02-u.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-02-d.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-02-b.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "tour.mp4").write_bytes(b"fake-video-data")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Pioche Lecombe Pure 360",
                "display_title": "Pioche Lecombe Pure 360",
                "listing_url": "https://www.example.test/listing",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "pure_360_cube",
                "scene_count": 2,
                "video_relpath": "tour.mp4",
                "facts": {
                    "rooms": 3,
                    "area_sqm": 81,
                    "total_rent_eur": 1490,
                    "availability": "sofort",
                    "address_lines": ["Währing, Wien"],
                    "teaser_attributes": ["360 Tour"],
                },
                "brief": {
                    "theme_name": "White-label 360",
                    "tour_style": "panorama first",
                    "audience": "buyers",
                    "creative_brief": "Lead with the real panorama viewer.",
                    "call_to_action": "Book a viewing.",
                },
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "photo",
                        "location_id": 201,
                        "scene_id": "living",
                        "asset_relpath": "scene-01-f.jpg",
                        "next_scene_index": 1,
                        "prev_scene_index": 1,
                        "cube_faces": {
                            "f": "scene-01-f.jpg",
                            "b": "scene-01-b.jpg",
                            "r": "scene-01-r.jpg",
                            "l": "scene-01-l.jpg",
                            "u": "scene-01-u.jpg",
                            "d": "scene-01-d.jpg",
                        },
                    },
                    {
                        "name": "Bedroom",
                        "role": "photo",
                        "location_id": 202,
                        "scene_id": "bedroom",
                        "asset_relpath": "scene-02-f.jpg",
                        "next_scene_index": 0,
                        "prev_scene_index": 0,
                        "cube_faces": {
                            "f": "scene-02-f.jpg",
                            "b": "scene-02-b.jpg",
                            "r": "scene-02-r.jpg",
                            "l": "scene-02-l.jpg",
                            "u": "scene-02-u.jpg",
                            "d": "scene-02-d.jpg",
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-pure-360")
    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})

    assert page.status_code == 404
    assert "This tour link is no longer available." in page.text
    assert "Unavailable" in page.text
    assert "@photo-sphere-viewer" not in page.text
    assert "CubemapAdapter" not in page.text
    assert "scene-01-f.jpg" not in page.text


def test_public_tour_payload_ignores_legacy_video_fallback_relpath(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "pioche-lecombe-video-clean"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01-f.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "tour.mp4").write_bytes(b"fake-video-data")
    (bundle_dir / "tour-fallback.mp4").write_bytes(b"fake-video-data")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Pioche Lecombe Video Clean",
                "display_title": "Pioche Lecombe Video Clean",
                "listing_url": "https://www.example.test/listing",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "pure_360_cube",
                "video_relpath": "tour.mp4",
                "video_fallback_relpath": "tour-fallback.mp4",
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "photo",
                        "scene_id": "living",
                        "asset_relpath": "scene-01-f.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-video-clean")
    response = client.get(f"/tours/{slug}.json")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "tour_disabled_fallback"


def test_public_tour_routes_keep_pure_360_white_labeled_when_origin_present(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "pioche-lecombe-pure-360-origin"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01-f.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-l.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-r.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-u.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-d.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "scene-01-b.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Pioche Lecombe Pure 360",
                "display_title": "Pioche Lecombe Pure 360",
                "listing_url": "https://www.example.test/listing",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "pure_360_cube",
                "scene_count": 1,
                "principal_id": "cf-email:person@example.test",
                "source_ref": "gmail-thread:person@example.test:test-fit-priority-1",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "facts": {
                    "rooms": 3,
                    "area_sqm": 81,
                    "total_rent_eur": 1490,
                    "availability": "sofort",
                    "address_lines": ["Währing, Wien"],
                    "postal_name": "Währing",
                    "heating_type": "Fernwaerme",
                    "has_floorplan": True,
                    "lift": True,
                    "personal_fit_assessment": {
                        "fit_score": 96.0,
                        "recommendation": "shortlist",
                        "match_reasons_json": ["The district matches the established shortlist."],
                        "mismatch_reasons_json": ["Check heating type on site."],
                        "unknowns_json": ["Confirm noise level with a visit."],
                        "location_fit_score": 5,
                        "livability_snapshot": {
                            "nearest_supermarket_m": 190,
                            "nearest_transit_m": 280,
                            "nearest_playground_m": 140,
                        },
                    },
                    "public_preference_snapshot": {
                        "domain": "willhaben",
                        "person_id": "self",
                        "preference_nodes": [
                            {"key": "preferred_areas", "category": "soft_preference", "value_json": ["Waehring"], "confidence": 1.0},
                            {"key": "avoid_heating_types", "category": "aversion", "value_json": ["Gasheizung"], "confidence": 1.0},
                            {"key": "prefer_lift", "category": "soft_preference", "value_json": True, "confidence": 1.0},
                            {"key": "playground_nearby", "category": "soft_preference", "value_json": True, "confidence": 0.9},
                        ],
                    },
                },
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "pure_360",
                        "scene_id": "living",
                        "asset_relpath": "scene-01-f.jpg",
                        "cube_faces": {
                            "f": "scene-01-f.jpg",
                            "b": "scene-01-b.jpg",
                            "r": "scene-01-r.jpg",
                            "l": "scene-01-l.jpg",
                            "u": "scene-01-u.jpg",
                            "d": "scene-01-d.jpg",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-pure-360-origin")
    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})

    assert page.status_code == 404
    assert 'src="https://360.kalandra.at/view/portal/id/VZ8P1"' not in page.text
    assert "Unavailable" in page.text
    assert 'id="prev-link"' not in page.text
    assert 'id="next-link"' not in page.text
    assert 'id="cube"' not in page.text
    assert "Live Panorama Viewer" not in page.text
    assert "Hosted tour page with the original 360 viewer embedded." not in page.text


def test_public_tour_request_details_requires_authenticated_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.api.routes import public_tours

    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "pioche-lecombe-detail-request"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Pioche Lecombe Pure 360",
                "display_title": "Pioche Lecombe Pure 360",
                "principal_id": "cf-email:person@example.test",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "source_ref": "gmail-thread:person@example.test:test-fit-priority-1",
                "variant_key": "layout_first",
                "listing_url": "https://www.kalandra.at/objekt/14997053",
                "scene_strategy": "live_360_embed",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "facts": {"has_360": True},
                "scenes": [{"name": "Living room", "role": "live_360", "asset_relpath": "scene.jpg"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    captured: dict[str, object] = {}

    class _FakeService:
        def request_property_tour_detail_refresh(self, **kwargs):
            captured.update(kwargs)
            return {"status": "requested", "human_task_id": "human_task:123"}

    monkeypatch.setattr(public_tours, "build_product_service", lambda container: _FakeService())
    client = _client(principal_id="exec-public-tour-request-details")
    unsigned = client.post(f"/tours/{slug}/request-details", headers={"host": "myexternalbrain.com"}, json={})
    assert unsigned.status_code == 403
    assert unsigned.json()["error"]["code"] == "request-details_requires_authenticated_account"
    assert captured == {}

    legacy_token_attempt = client.post(
        f"/tours/{slug}/request-details",
        headers={"host": "myexternalbrain.com"},
        json={"action_token": "v1.9999999999.legacy-browser-token"},
    )
    assert legacy_token_attempt.status_code == 403
    assert legacy_token_attempt.json()["error"]["code"] == "request-details_requires_authenticated_account"
    assert captured == {}


def test_public_tour_feedback_updates_learning_loop_and_live_assessment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_360_ALLOWED_HOSTS", "360.kalandra.at")
    slug = "pioche-lecombe-feedback-loop"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Feedback Loop Property",
                "display_title": "Feedback Loop Property",
                "listing_url": "https://www.kalandra.at/objekt/14997053",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "live_360_embed",
                "scene_count": 1,
                "principal_id": "cf-email:person@example.test",
                "source_virtual_tour_url": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "facts": {
                    "postal_name": "Waehring",
                    "district": "Waehring",
                    "rooms": 3,
                    "area_sqm": 68,
                    "total_rent_eur": 2450,
                    "heating_type": "Gasheizung",
                    "has_floorplan": False,
                    "lift": False,
                    "nearest_subway_m": 1400,
                },
                "scenes": [
                        {
                            "name": "Live 360",
                            "role": "live_360",
                            "scene_id": "living",
                            "image_url": "https://my.matterport.com/api/v2/player/models/VZ8P1/thumb/",
                            "source_url": "https://360.kalandra.at/view/portal/id/VZ8P1",
                            "mime_type": "image/jpeg",
                        }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-feedback")
    first_page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})
    assert first_page.status_code == 200
    assert "Teach the system what to rank higher or lower" in first_page.text
    assert "Public-link feedback is captured as an external signal" in first_page.text
    assert "Too expensive" in first_page.text
    assert "No lift" in first_page.text
    assert "tour-action-tokens" not in first_page.text
    assert '"feedback":' not in first_page.text

    feedback = client.post(
        f"/tours/{slug}/feedback",
        headers={"host": "myexternalbrain.com"},
        json={"reaction": "dislike", "reason_keys": ["gas_heating", "no_lift"], "note": "This is exactly what I do not want."},
    )
    assert feedback.status_code == 200
    body = feedback.json()
    assert body["status"] == "captured_external"
    assert body["trust"] == "untrusted_external"
    assert body["reaction"] == "dislike"
    assert body["reason_keys"] == ["gas_heating", "no_lift"]
    assert "evidence" not in body

    second_page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})
    assert second_page.status_code == 200
    assert "What the system has learned from you" not in second_page.text
    assert "Avoid heating: Gasheizung" not in second_page.text


def test_public_tour_feedback_reports_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_RATE_LIMIT_DIR", str(tmp_path / "rates"))
    slug = "pioche-lecombe-feedback-persistence-failure"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Feedback Persistence Guard",
                "display_title": "Feedback Persistence Guard",
                "listing_url": "https://www.kalandra.at/objekt/14997053",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "live_360_embed",
                "scene_count": 1,
                "principal_id": "cf-email:person@example.test",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "facts": {"postal_name": "Waehring", "has_floorplan": True},
                "scenes": [{"name": "Living room", "role": "live_360", "asset_relpath": "scene.jpg"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-feedback-fails")

    def _fail_ingest(**_kwargs):
        raise RuntimeError("observation_store_unavailable")

    client.app.state.container.channel_runtime.ingest_observation = _fail_ingest
    response = client.post(
        f"/tours/{slug}/feedback",
        headers={"host": "myexternalbrain.com"},
        json={"reaction": "maybe", "reason_keys": [], "note": "Save should be honest."},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_captured"
    assert body["retryable"] is True
    assert body["error"] == "public_tour_feedback_persistence_failed"


def test_public_tour_feedback_rate_limit_ignores_untrusted_x_forwarded_for(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("PROPERTYQUARRY_TRUST_X_FORWARDED_FOR", "0")
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_RATE_LIMIT_DIR", str(tmp_path / "rates"))
    slug = "pioche-lecombe-feedback-rate-limit"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Feedback Rate Limit Guard",
                "display_title": "Feedback Rate Limit Guard",
                "listing_url": "https://www.kalandra.at/objekt/14997053",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "live_360_embed",
                "scene_count": 1,
                "principal_id": "cf-email:person@example.test",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "facts": {"postal_name": "Waehring", "has_floorplan": True},
                "scenes": [{"name": "Living room", "role": "live_360", "asset_relpath": "scene.jpg"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    client = _client(principal_id="exec-public-tour-feedback-rate-limit")

    for index in range(12):
        response = client.post(
            f"/tours/{slug}/feedback",
            headers={"host": "myexternalbrain.com", "x-forwarded-for": f"198.51.100.{index + 1}"},
            json={"reaction": "maybe", "reason_keys": [], "note": f"Feedback {index}"},
        )
        assert response.status_code == 200, response.text

    limited = client.post(
        f"/tours/{slug}/feedback",
        headers={"host": "myexternalbrain.com", "x-forwarded-for": "203.0.113.250"},
        json={"reaction": "maybe", "reason_keys": [], "note": "Spoofed IP should not bypass."},
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "public_tour_feedback_rate_limited"


def test_public_tour_feedback_rejects_invalid_payload_and_unknown_reasons(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "pioche-lecombe-feedback-invalid-payload"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Feedback Guard Property",
                "display_title": "Feedback Guard Property",
                "listing_url": "https://www.kalandra.at/objekt/guard-1",
                "property_url": "https://www.kalandra.at/objekt/guard-1",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "live_360_embed",
                "scene_count": 1,
                "principal_id": "cf-email:person@example.test",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/guard",
                "facts": {
                    "postal_name": "Waehring",
                    "district": "Waehring",
                    "rooms": 3,
                    "area_sqm": 68,
                    "total_rent_eur": 2450,
                    "heating_type": "Gasheizung",
                    "has_floorplan": False,
                    "lift": False,
                    "nearest_subway_m": 1400,
                },
                "scenes": [
                    {
                        "name": "Living room",
                            "role": "live_360",
                        "scene_id": "living",
                        "asset_relpath": "scene-01-f.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-feedback-guard")

    invalid = client.post(
        f"/tours/{slug}/feedback",
        headers={"host": "myexternalbrain.com"},
        json={
            "reaction": "dislike",
            "reason_keys": "gas_heating",
            "note": "invalid payload",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_tour_feedback_reason_keys"

    unknown_reason = client.post(
        f"/tours/{slug}/feedback",
        headers={"host": "myexternalbrain.com"},
        json={
            "reaction": "dislike",
            "reason_keys": ["not_a_reason"],
            "note": "unknown reason",
        },
    )
    assert unknown_reason.status_code == 422
    assert unknown_reason.json()["error"]["code"] == "invalid_tour_feedback_reason_key"

    invalid_reaction = client.post(
        f"/tours/{slug}/feedback",
        headers={"host": "myexternalbrain.com"},
        json={
            "reaction": "nah",
            "reason_keys": ["gas_heating"],
            "note": "invalid reaction",
        },
    )
    assert invalid_reaction.status_code == 422
    assert invalid_reaction.json()["error"]["code"] == "invalid_tour_feedback_reaction"


def test_public_tour_filter_update_requires_authenticated_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "pioche-lecombe-filter-update"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Filter Update Property",
                "display_title": "Filter Update Property",
                "listing_url": "https://www.kalandra.at/objekt/14997053",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "live_360_embed",
                "scene_count": 1,
                "principal_id": "cf-email:person@example.test",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "facts": {
                    "postal_name": "1190 Wien",
                    "district": "Salmannsdorf",
                    "rooms": 3,
                    "area_sqm": 101.2,
                    "total_rent_eur": 2599.8,
                    "heating_type": "Hauszentralheizung (Gas)",
                    "has_floorplan": True,
                    "lift": True,
                },
                "scenes": [
                    {
                        "name": "Living room",
                            "role": "live_360",
                        "scene_id": "living",
                        "asset_relpath": "scene-01-f.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-filter-update")
    first_page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})
    assert first_page.status_code == 200
    assert "Search Filters" not in first_page.text
    assert "Prefer 1190 Wien" not in first_page.text
    assert "tour-action-tokens" not in first_page.text
    assert '"filters":' not in first_page.text
    assert "What the system has learned from you" not in first_page.text

    unsigned = client.post(
        f"/tours/{slug}/filters",
        headers={"host": "myexternalbrain.com"},
        json={"filter_key": "avoid_gas_heating", "enabled": True},
    )
    assert unsigned.status_code == 403
    assert unsigned.json()["error"]["code"] == "filters_requires_authenticated_account"

    legacy_token_attempt = client.post(
        f"/tours/{slug}/filters",
        headers={"host": "myexternalbrain.com"},
        json={
            "filter_key": "avoid_gas_heating",
            "enabled": True,
            "action_token": "v1.9999999999.legacy-browser-token",
        },
    )
    assert legacy_token_attempt.status_code == 403
    assert legacy_token_attempt.json()["error"]["code"] == "filters_requires_authenticated_account"

    second_page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})
    assert second_page.status_code == 200
    assert "Avoid gas heating" not in second_page.text
    assert "Active filters" not in second_page.text
    assert "Hard blocks and must-haves" not in second_page.text


def test_public_tour_filter_update_rejects_invalid_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "pioche-lecombe-filter-invalid"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Filter Guard Property",
                "display_title": "Filter Guard Property",
                "listing_url": "https://www.kalandra.at/objekt/14997053",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "live_360_embed",
                "scene_count": 1,
                "principal_id": "cf-email:person@example.test",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "facts": {
                    "postal_name": "1190 Wien",
                    "district": "Salmannsdorf",
                    "rooms": 3,
                    "area_sqm": 101.2,
                    "total_rent_eur": 2599.8,
                    "heating_type": "Hauszentralheizung (Gas)",
                    "has_floorplan": True,
                    "lift": True,
                },
                "scenes": [
                    {
                        "name": "Living room",
                            "role": "live_360",
                        "scene_id": "living",
                        "asset_relpath": "scene-01-f.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-filter-invalid")

    missing_filter = client.post(
        f"/tours/{slug}/filters",
        headers={"host": "myexternalbrain.com"},
        json={"filter_key": "", "enabled": True},
    )
    assert missing_filter.status_code == 403
    assert missing_filter.json()["error"]["code"] == "filters_requires_authenticated_account"

    invalid_filter = client.post(
        f"/tours/{slug}/filters",
        headers={"host": "myexternalbrain.com"},
        json={
            "filter_key": "does_not_exist",
            "enabled": True,
        },
    )
    assert invalid_filter.status_code == 403
    assert invalid_filter.json()["error"]["code"] == "filters_requires_authenticated_account"

    invalid_enabled = client.post(
        f"/tours/{slug}/filters",
        headers={"host": "myexternalbrain.com"},
        json={
            "filter_key": "prefer_subway_nearby",
            "enabled": "maybe",
        },
    )
    assert invalid_enabled.status_code == 403
    assert invalid_enabled.json()["error"]["code"] == "filters_requires_authenticated_account"

    string_false = client.post(
        f"/tours/{slug}/filters",
        headers={"host": "myexternalbrain.com"},
        json={
            "filter_key": "prefer_subway_nearby",
            "enabled": "false",
        },
    )
    assert string_false.status_code == 403
    assert string_false.json()["error"]["code"] == "filters_requires_authenticated_account"


def test_shortlist_float_parsing_is_locale_aware() -> None:
    from app.api.routes import public_tours

    assert public_tours._shortlist_as_float("2.599,80 EUR") == 2599.8
    assert public_tours._shortlist_as_float("1,234.56") == 1234.56
    assert public_tours._shortlist_as_float("3.200") == 3200.0
    assert public_tours._shortlist_as_float("1200") == 1200.0
    assert public_tours._shortlist_as_float("84 m²") == 84.0
    assert public_tours._shortlist_as_float(None) is None


def test_public_tour_renders_active_shortlist_panel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("PROPERTYQUARRY_PUBLIC_360_ALLOWED_HOSTS", "360.kalandra.at")
    slug = "pioche-lecombe-shortlist-compare"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Shortlist Compare Property",
                "display_title": "Shortlist Compare Property",
                "listing_url": "https://www.kalandra.at/objekt/14997053",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "live_360_embed",
                "scene_count": 1,
                "principal_id": "cf-email:person@example.test",
                "source_virtual_tour_url": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "facts": {
                    "postal_name": "1190 Wien",
                    "district": "Salmannsdorf",
                    "rooms": 3,
                    "area_sqm": 101.2,
                    "total_rent_eur": 2599.8,
                    "personal_fit_assessment": {
                        "fit_score": 83.0,
                        "good_fit_reasons": ["Current property has a strong layout and district fit."],
                    },
                },
                "scenes": [
                        {
                            "name": "Live 360",
                            "role": "live_360",
                            "scene_id": "living",
                            "image_url": "https://my.matterport.com/api/v2/player/models/VZ8P1/thumb/",
                            "source_url": "https://360.kalandra.at/view/portal/id/VZ8P1",
                            "mime_type": "image/jpeg",
                        }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    from app.api.routes import public_tours

    shortlist_candidate_dir = tmp_path / "k-1411708198"
    shortlist_candidate_dir.mkdir()
    (shortlist_candidate_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": "k-1411708198",
                "title": "Strong Waehring candidate",
                "external_id": "1411708198",
                "property_url": "https://www.willhaben.at/objekt/1411708198",
                "facts": {
                    "postal_name": "1190 Wien",
                    "rooms": 4,
                    "area_sqm": 84,
                    "total_rent_eur": 2799.0,
                    "lift": True,
                    "has_floorplan": True,
                    "heating_type": "Gas",
                    "nearest_supermarket_m": 420,
                    "nearest_subway_m": 360,
                    "nearest_playground_m": 190,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    shortlist_candidate_dir2 = tmp_path / "k-1071155412"
    shortlist_candidate_dir2.mkdir()
    (shortlist_candidate_dir2 / "tour.json").write_text(
        json.dumps(
            {
                "slug": "k-1071155412",
                "title": "Strong Doebling candidate",
                "external_id": "1071155412",
                "property_url": "https://www.willhaben.at/objekt/1071155412",
                "facts": {
                    "postal_name": "1210 Wien",
                    "rooms": 2,
                    "area_sqm": 92,
                    "total_rent_eur": 2400,
                    "lift": False,
                    "has_floorplan": False,
                    "heating_type": "Fernwaerme",
                    "nearest_supermarket_m": 140,
                    "nearest_subway_m": 180,
                    "nearest_playground_m": 95,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    class _FakeService:
        def get_preference_profile(self, *, principal_id: str, person_id: str = "self"):
            return {"profile": {}, "preference_nodes": [], "recent_evidence_events": [], "recent_decision_assessments": [], "recent_corrections": []}

        def preview_preference_candidate(self, **kwargs):
            return {"fit_score": 83.0, "good_fit_reasons": ["Current property has a strong layout and district fit."]}

        def property_feedback_suggestions(self, **kwargs):
            return {"negative": [], "positive": []}

        def property_feedback_learning_summary(self, **kwargs):
            return {"likes": [], "dislikes": [], "hard_rules": [], "recent_feedback": []}

        def list_brief_items(self, *, principal_id: str, limit: int = 8, **kwargs):
            from types import SimpleNamespace

            return (
                SimpleNamespace(
                    title="Strong Waehring listing",
                    score=97.0,
                    why_now="High-fit property alert with 360 media and preferred district match.",
                    recommended_action="review property alert",
                    object_ref="willhaben:1411708198",
                ),
                SimpleNamespace(
                    title="Strong Doebling listing",
                    score=91.0,
                    why_now="Another high-fit property alert with lift and bike access.",
                    recommended_action="compare against shortlist",
                    object_ref="willhaben:1071155412",
                ),
            )

    monkeypatch.setattr(public_tours, "build_product_service", lambda container: _FakeService())

    client = _client(principal_id="exec-public-tour-shortlist-compare")
    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})

    assert page.status_code == 200
    assert "Shortlist Ranking" not in page.text
    assert "Current property in the active shortlist" in page.text
    assert "Strong Waehring listing" in page.text
    assert "Strong Doebling listing" in page.text
    assert "High-fit property alert with 360 media and preferred district match." in page.text
    assert "Another high-fit property alert with lift and bike access." in page.text
    assert "Open ranked property" in page.text
    assert "Review ranked alternative" in page.text
    assert "compare against shortlist" not in page.text
    assert "pure_360_cube" not in page.text
    assert "3D fallback blocked" not in page.text


def test_public_tour_routes_ignore_unsafe_live_360_source_urls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "pioche-lecombe-unsafe-live-360"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Unsafe Live 360",
                "display_title": "Unsafe Live 360",
                "source_virtual_tour_url": "javascript:alert(1)",
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "photo",
                        "image_url": "https://example.test/original.jpg",
                        "source_url": "https://example.test/original.jpg",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-unsafe-live-360")
    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})

    assert page.status_code == 200
    assert "Live Panorama Viewer" not in page.text
    assert 'href="#viewer"' in page.text
    assert "javascript:alert(1)" not in page.text


def test_public_tour_routes_use_listing_research_to_fill_decision_brief(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.api.routes import public_tours

    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "listing-research-tour"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Research-backed property page",
                "display_title": "Research-backed property page",
                "listing_url": "https://www.kalandra.at/objekt/14997053",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "hosted_url": f"https://ea.example/tours/{slug}",
                "scene_strategy": "pure_360_cube",
                "scene_count": 1,
                "source_virtual_tour_origin": "https://360.kalandra.at/view/portal/id/VZ8P1",
                "facts": {
                    "has_360": True,
                    "street_address": "",
                    "address_lines": ["", ""],
                    "nearest_supermarket_m": 0,
                    "listing_research_snapshot": {
                        "has_floorplan": True,
                        "lift": True,
                        "availability": "Sofort",
                        "heating_type": "Hauszentralheizung (Gas)",
                        "street_address": "Hameaustraße 34",
                        "nearest_supermarket_m": 951,
                        "nearest_pharmacy_m": 882,
                        "nearest_playground_m": 532,
                        "nearest_subway_m": 4752,
                        "terrace_area_sqm": 43.0,
                        "building_units": 8,
                    },
                    "listing_research_meta": {
                        "strategy": "provider_html_plus_geo",
                    },
                },
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "pure_360",
                        "scene_id": "living",
                        "asset_relpath": "scene-01-f.jpg",
                        "cube_faces": {
                            "f": "scene-01-f.jpg",
                            "b": "scene-01-b.jpg",
                            "r": "scene-01-r.jpg",
                            "l": "scene-01-l.jpg",
                            "u": "scene-01-u.jpg",
                            "d": "scene-01-d.jpg",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    assert not hasattr(public_tours, "_fetch_listing_research")

    client = _client(principal_id="exec-public-tour-research")
    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})

    assert page.status_code == 404
    assert "Unavailable" in page.text
    assert "Lift and floor plan materially reduce remote-viewing uncertainty." not in page.text
    assert "43 m² of terrace area adds meaningful private outdoor space." not in page.text
    assert "Immersive 360 tour is available." not in page.text
    assert "Hameaustraße 34" not in page.text
    assert "@photo-sphere-viewer" not in page.text


def test_public_tour_page_does_not_fetch_live_listing_research_at_render_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from app.api.routes import public_tours

    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv("EA_PUBLIC_TOUR_ENABLE_RENDER_RESEARCH_FALLBACK", "1")
    slug = "snapshot-only-research-tour"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.jpg").write_bytes(b"fake-jpeg-data")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Snapshot-only research tour",
                "display_title": "Snapshot-only research tour",
                "listing_url": "https://www.kalandra.at/objekt/14997053",
                "property_url": "https://www.kalandra.at/objekt/14997053",
                "scene_count": 1,
                "facts": {"rooms": 2, "area_sqm": 58},
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "photo",
                        "asset_relpath": "scene-01.jpg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    assert not hasattr(public_tours, "_fetch_listing_research")

    client = _client(principal_id="exec-public-tour-research")
    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})

    assert page.status_code == 200
    assert "Snapshot-only research tour" in page.text


def test_public_tour_routes_refuse_generated_fallback_tours(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "fallback-tour-disabled"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "scene-01.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>", encoding="utf-8")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Fallback tour",
                "display_title": "Fallback tour",
                "scene_strategy": "generated_listing_summary",
                "creation_mode": "hosted_listing_fallback",
                "scenes": [
                    {
                        "name": "Generated listing overview",
                        "role": "generated_overview",
                        "asset_relpath": "scene-01.svg",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-tour-fallback-disabled")
    page = client.get(f"/tours/{slug}", headers={"host": "myexternalbrain.com"})
    payload = client.get(f"/tours/{slug}.json")

    assert page.status_code == 404
    assert "This old link no longer opens as a 3D tour." in page.text
    assert "Request a fresh 3D tour" in page.text
    assert payload.status_code == 404
    assert payload.json()["error"]["code"] == "tour_disabled_fallback"


def test_public_tour_routes_refuse_unverified_generated_reconstruction_tours(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "generated-reconstruction-disabled"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "diorama-preview.png").write_bytes(b"fake-png-data")
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Generated reconstruction",
                "display_title": "Generated reconstruction",
                "publication_status": "ready",
                "creation_mode": "generated_reconstruction_tour",
                "scene_strategy": "generated_reconstruction",
                "generated_reconstruction": {
                    "provider": "propertyquarry_generated_reconstruction",
                    "verified_provider_capture": False,
                    "satisfies_verified_tour_gate": False,
                },
                "scenes": [
                    {
                        "name": "Diorama",
                        "role": "diorama",
                        "asset_relpath": "diorama-preview.png",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-generated-reconstruction-disabled")
    page = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"})
    payload = client.get(f"/tours/{slug}.json")
    walkthrough = client.get(f"/tours/{slug}/walkthrough")

    assert page.status_code == 404
    assert payload.status_code == 404
    assert payload.json()["error"]["code"] == "tour_disabled_fallback"
    assert walkthrough.status_code == 404
    assert walkthrough.json()["error"]["code"] == "tour_disabled_fallback"


def test_public_tour_routes_refuse_generated_showcase_shell_even_with_3dvista_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A local showcase shell must never be promoted as a vendor tour."""
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "1")
    slug = "generated-showcase-shell-disabled"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Generated showcase shell",
                "display_title": "Generated showcase shell",
                "creation_mode": "generated_private_showcase",
                "control_mode": "3dvista",
                "three_d_vista_entry_relpath": "3dvista/index.htm",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (bundle_dir / "tour.private.json").write_text(
        json.dumps(
            {
                "three_d_vista_import": {
                    "source": "propertyquarry_generated_showcase_shell",
                },
                "three_d_vista_white_label_proof": {
                    "proof_kind": "local_showcase_shell",
                    "private_viewer_verified": True,
                    "non_trial_export_verified": True,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))

    client = _client(principal_id="exec-generated-showcase-shell-disabled")
    page = client.get(f"/tours/{slug}", headers={"host": "propertyquarry.com"})
    payload = client.get(f"/tours/{slug}.json")

    assert page.status_code == 404
    assert "This old link no longer opens as a 3D tour." in page.text
    assert payload.status_code == 404
    assert payload.json()["error"]["code"] == "tour_disabled_fallback"


def test_public_memorial_routes_render_original_voice_without_voice_clone(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_MEMORIALS", "1")
    monkeypatch.setenv("EA_ENABLE_CLICKRANK", "1")
    monkeypatch.setenv("CLICKRANK_AI_MYEXTERNALBRAIN_SITE_ID", "33ff8f39-6213-4903-99d7-81048b5b3e1f")
    slug = "manfred"
    bundle_dir = tmp_path / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "audio").mkdir()
    (bundle_dir / "audio" / "hanusch-enhanced.mp3").write_bytes(b"fake-mp3-data")
    (bundle_dir / "memorial.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "person_name": "Manfred",
                "title": "Erinnerungen an Manfred",
                "relationship": "Vater",
                "subtitle": "Eine ruhige Seite fuer Erinnerungen und Originalstimme.",
                "disclosure": "Originalaufnahmen sind als Original gekennzeichnet.",
                "intro": "Neue Texte sind keine direkte Rede.",
                "audio_clips": [
                    {
                        "label": "Originalaufnahme",
                        "title": "Hanusch Gespraech",
                        "description": "Freigegebener Ausschnitt aus dem Archiv.",
                        "asset_relpath": "audio/hanusch-enhanced.mp3",
                    }
                ],
                "memory_cards": [
                    {
                        "source_label": "Transkript",
                        "title": "Schach",
                        "body": "Das Schach soll in der Familie bleiben.",
                    }
                ],
                "suggested_prompts": ["Was ist wirklich belegt?"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_MEMORIAL_DIR", str(tmp_path))

    client = _client(principal_id="exec-public-memorial")
    page = client.get(f"/memorials/{slug}", headers={"host": "myexternalbrain.com"})

    assert page.status_code == 200
    assert "Manfred" in page.text
    assert "Seine Stimme hoeren" in page.text
    assert "Sprich mit der Erinnerung" in page.text
    assert "voice clone" not in page.text.lower()
    assert f"/memorials/files/{slug}/audio/hanusch-enhanced.mp3" in page.text
    assert "clickrank.ai" not in page.text

    payload = client.get(f"/memorials/{slug}.json")
    assert payload.status_code == 200
    assert payload.json()["person_name"] == "Manfred"

    audio = client.get(f"/memorials/files/{slug}/audio/hanusch-enhanced.mp3")
    assert audio.status_code == 200
    assert audio.content == b"fake-mp3-data"
    assert audio.headers["content-type"].startswith("audio/mpeg")


def test_public_memorial_chat_uses_private_context_without_public_diagnosis_leak(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_MEMORIALS", "1")
    slug = "manfred"
    bundle_dir = tmp_path / "public" / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "memorial.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "person_name": "Manfred Hoza",
                "audio_clips": [],
                "memory_cards": [{"source_label": "Archiv", "title": "Schach", "body": "Das Schach bleibt in der Familie."}],
                "source_grounded_profile": [{"trait": "Gerechtigkeit", "evidence": "Opferschutz war ein wiederkehrendes Thema."}],
                "external_sources": [{"label": "RIS Suche", "url": "https://www.ris.bka.gv.at/Suchergebnis.wxe?Suchworte=Manfred%20Hoza"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    private_dir = tmp_path / "private" / slug
    private_dir.mkdir(parents=True)
    (private_dir / "llm_profile_notes.json").write_text(
        json.dumps(
            {
                "visibility": "private_llm_context_only_not_public_page",
                "family_context_notes": [
                    {
                        "label": "narcissistic_and_adhd_like_traits_private_hypothesis",
                        "confidence": "family_observation_no_diagnosis",
                        "note": "Private style hint only.",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (private_dir / "tts_voice.json").write_text(
        json.dumps(
            {
                "tts_mode": "browser_speech_synthesis",
                "voice_profile_id": "tibor-consented-placeholder",
                "voice_label": "Tibor freigegebene synthetische Stimme",
                "lang": "de-AT",
                "rate": 0.88,
                "pitch": 0.86,
                "volume": 1,
                "voice_name_hints": ["Tibor", "de-AT"],
                "synthetic_voice_clone_of_memorial_person": True,
                "provider_secret": "must-not-leak",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_MEMORIAL_DIR", str(tmp_path / "public"))
    monkeypatch.setenv("EA_PRIVATE_MEMORIAL_PROFILE_DIR", str(tmp_path / "private"))

    client = _client(principal_id="exec-public-memorial-chat")
    page = client.get(f"/memorials/{slug}", headers={"host": "myexternalbrain.com"})
    assert page.status_code == 200
    assert "narcissistic" not in page.text.lower()
    assert "adhd" not in page.text.lower()
    assert "/memorials/manfred/chat" in page.text
    assert "/memorials/manfred/speech-transcribe" in page.text
    assert "memorial-speech-listen" in page.text
    assert "memorial-server-stt" in page.text
    assert "memorial-conversation" in page.text
    assert "memorial-speech-speak" in page.text
    assert "SpeechRecognition" in page.text
    assert "MediaRecorder" in page.text
    assert "SpeechSynthesisUtterance" in page.text
    assert "Austauschbare synthetische Stimme" in page.text
    assert "Mikrofonzugriff braucht HTTPS" in page.text
    assert "not-allowed" in page.text
    assert "no-speech" in page.text
    assert "speechHadError" in page.text
    assert "Browser-Spracherkennung hat ein Netzwerkproblem. Bitte Server-STT starten." in page.text
    assert "readJsonResponse" in page.text
    assert "Gespräch läuft. Ich transkribiere fortlaufend." in page.text
    assert "recorder.start(900)" in page.text

    voice = client.get(f"/memorials/{slug}/voice-config")
    assert voice.status_code == 200
    voice_body = voice.json()
    assert voice_body["voice_profile_id"] == "tibor-consented-placeholder"
    assert voice_body["voice_label"] == "Tibor freigegebene synthetische Stimme"
    assert voice_body["rate"] == 0.88
    assert voice_body["pitch"] == 0.86
    assert voice_body["synthetic_voice_clone_of_memorial_person"] is False
    assert "provider_secret" not in voice_body

    response = client.post(f"/memorials/{slug}/chat", json={"question": "Wie ging er mit Kritik um?"})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "memorial_first_person_memory_chat"
    assert body["private_context_used"] is True
    assert "Ich bin nicht Manfred Hoza" not in body["answer"]
    assert "Ich lasse mir nicht einreden" in body["answer"]
    assert "immer ich schuld" in body["answer"]
    assert "Das tut mir leid" not in body["answer"]
    assert "ADHS" not in body["answer"]
    assert "narcissistic" not in body["answer"].lower()
    assert "Erinnerungsanker" not in body["answer"]

    discipline = client.post(f"/memorials/{slug}/chat", json={"question": "Was dachte er ueber Kinder schlagen?"})
    assert discipline.status_code == 200
    discipline_body = discipline.json()
    assert discipline_body["private_context_used"] is True
    assert "Ein Kind muss lernen" in discipline_body["answer"]
    assert "nicht so tun" in discipline_body["answer"]

    household = client.post(f"/memorials/{slug}/chat", json={"question": "Was war seine Haltung zu Haushalt, Hemden und Kindererziehung?"})
    assert household.status_code == 200
    household_body = household.json()
    assert "Ich habe meinen Teil getan" in household_body["answer"]
    assert "Aufgabe der Frau" in household_body["answer"]

    politics = client.post(f"/memorials/{slug}/chat", json={"question": "Warum war er bei MFG und gegen Auslaender?"})
    assert politics.status_code == 200
    politics_body = politics.json()
    assert "nicht gern von oben" in politics_body["answer"]
    assert "Bei Zuwanderung war ich hart" in politics_body["answer"]

    covid = client.post(f"/memorials/{slug}/chat", json={"question": "Warum wollte er keine Covid Impfung und was dachte er ueber Aerzte und Pharma?"})
    assert covid.status_code == 200
    covid_body = covid.json()
    assert "Aerzten und Pharmafirmen" in covid_body["answer"]
    assert "ich sehe da klarer" in covid_body["answer"]


def test_public_memorial_speech_transcribe_uploads_audio_and_returns_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_MEMORIALS", "1")
    slug = "manfred"
    bundle_dir = tmp_path / "public" / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "memorial.json").write_text(
        json.dumps({"slug": slug, "person_name": "Manfred Hoza"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_MEMORIAL_DIR", str(tmp_path / "public"))

    from app.api.routes import public_memorials

    seen: dict[str, object] = {}

    def _fake_transcribe(*, payload, content_type):
        seen["payload"] = payload
        seen["content_type"] = content_type
        return {"transcription_status": "transcribed", "transcript_text": "Was war ihm bei Familie wichtig?", "transcriber": "test"}

    monkeypatch.setattr(public_memorials, "_memorial_transcribe_audio_blob", _fake_transcribe)
    client = _client(principal_id="exec-public-memorial-speech")

    response = client.post(
        f"/memorials/{slug}/speech-transcribe",
        content=b"fake-webm-audio",
        headers={"content-type": "audio/wav"},
    )

    assert response.status_code == 200
    assert response.json()["transcript_text"] == "Was war ihm bei Familie wichtig?"
    assert seen["payload"] == b"fake-webm-audio"
    assert seen["content_type"] == "audio/wav"


def test_public_memorial_speech_transcribe_normalizes_json_text_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_MEMORIALS", "1")
    slug = "manfred"
    bundle_dir = tmp_path / "public" / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "memorial.json").write_text(
        json.dumps({"slug": slug, "person_name": "Manfred Hoza"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_MEMORIAL_DIR", str(tmp_path / "public"))

    from app.api.routes import public_memorials

    from app.product import service as product_service

    monkeypatch.setattr(product_service, "_pocket_onemin_api_keys", lambda: ("key-1",))
    monkeypatch.setattr(product_service, "_onemin_asset_upload", lambda **kwargs: {"fileContent": {"path": "asset/audio.webm"}})
    monkeypatch.setattr(
        product_service,
        "_onemin_speech_to_text",
        lambda **kwargs: {
            "aiRecord": {
                "aiRecordDetail": {
                    "responseObject": {
                        "text": json.dumps({"text": "Was war ihm bei Familie wichtig?", "language": "german"})
                    }
                }
            }
        },
    )
    client = _client(principal_id="exec-public-memorial-speech-json")

    response = client.post(
        f"/memorials/{slug}/speech-transcribe",
        content=b"fake-webm-audio",
        headers={"content-type": "audio/wav"},
    )

    assert response.status_code == 200
    assert response.json()["transcript_text"] == "Was war ihm bei Familie wichtig?"


def test_public_memorial_speech_transcribe_converts_browser_webm_before_upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_MEMORIALS", "1")
    slug = "manfred"
    bundle_dir = tmp_path / "public" / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "memorial.json").write_text(
        json.dumps({"slug": slug, "person_name": "Manfred Hoza"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_MEMORIAL_DIR", str(tmp_path / "public"))

    from app.api.routes import public_memorials
    from app.product import service as product_service

    seen: dict[str, object] = {}
    monkeypatch.setattr(public_memorials, "_convert_audio_to_wav", lambda **kwargs: b"converted-wav")
    monkeypatch.setattr(product_service, "_pocket_onemin_api_keys", lambda: ("key-1",))

    def _upload(**kwargs):
        seen.update(kwargs)
        return {"fileContent": {"path": "asset/audio.wav"}}

    monkeypatch.setattr(product_service, "_onemin_asset_upload", _upload)
    monkeypatch.setattr(
        product_service,
        "_onemin_speech_to_text",
        lambda **kwargs: {
            "aiRecord": {
                "aiRecordDetail": {
                    "responseObject": {"text": "Was war ihm bei Familie wichtig?"}
                }
            }
        },
    )
    client = _client(principal_id="exec-public-memorial-speech-webm-convert")

    response = client.post(
        f"/memorials/{slug}/speech-transcribe",
        content=b"browser-webm",
        headers={"content-type": "audio/webm;codecs=opus"},
    )

    assert response.status_code == 200
    assert response.json()["transcript_text"] == "Was war ihm bei Familie wichtig?"
    assert seen["filename"] == "memorial-speech.wav"
    assert seen["content_type"] == "audio/wav"
    assert seen["payload"] == b"converted-wav"


def test_public_memorial_speech_transcribe_returns_retryable_json_for_provider_audio_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_MEMORIALS", "1")
    slug = "manfred"
    bundle_dir = tmp_path / "public" / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "memorial.json").write_text(
        json.dumps({"slug": slug, "person_name": "Manfred Hoza"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_MEMORIAL_DIR", str(tmp_path / "public"))

    from app.product import service as product_service

    monkeypatch.setattr(product_service, "_pocket_onemin_api_keys", lambda: ("key-1",))
    monkeypatch.setattr(product_service, "_onemin_asset_upload", lambda **kwargs: {"fileContent": {"path": "asset/audio.webm"}})

    def _audio_error(**kwargs):
        raise RuntimeError('onemin_transcribe_http_400:{"errorCode":"AUDIO_FORMAT_NOT_SUPPORTED"}')

    monkeypatch.setattr(product_service, "_onemin_speech_to_text", _audio_error)
    client = _client(principal_id="exec-public-memorial-speech-provider-error")

    response = client.post(
        f"/memorials/{slug}/speech-transcribe",
        content=b"fake-webm-audio",
        headers={"content-type": "audio/wav"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["transcription_status"] == "no_speech"
    assert body["transcript_text"] == ""
    assert body["retryable"] is True
    assert "AUDIO_FORMAT_NOT_SUPPORTED" in body["detail"]


def test_public_memorial_voice_profile_routes_support_config_and_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_MEMORIALS", "1")
    slug = "manfred"
    public_root = tmp_path / "public"
    private_root = tmp_path / "private"
    bundle_dir = public_root / slug
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "audio").mkdir()
    (bundle_dir / "audio" / "hanusch-enhanced.mp3").write_bytes(b"fake-mp3")
    (bundle_dir / "memorial.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "person_name": "Manfred Hoza",
                "audio_clips": [{"asset_relpath": "audio/hanusch-enhanced.mp3"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_PUBLIC_MEMORIAL_DIR", str(public_root))
    monkeypatch.setenv("EA_PRIVATE_MEMORIAL_PROFILE_DIR", str(private_root))

    from app.api.routes import public_memorials
    from app.services import memorial_voice_profile

    monkeypatch.setattr(public_memorials, "build_memorial_voice_profile", memorial_voice_profile.build_memorial_voice_profile)
    monkeypatch.setattr(memorial_voice_profile, "_search_youtube_urls", lambda query, max_results: ["https://www.youtube.com/watch?v=abc"])

    def _fake_download_youtube_audio(*, urls, output_dir):  # type: ignore[override]
        output_dir.mkdir(parents=True, exist_ok=True)
        asset = output_dir / "youtube_download.mp3"
        asset.write_bytes(b"youtube-bytes")
        return [asset], []

    def _fake_compute_signature(*, source_path):
        return {
            "duration_seconds": 12.0,
            "sample_rate": 16000,
            "channels": 1,
            "frame_count": 192000,
            "size_bytes": source_path.stat().st_size,
            "audio_features": {
                "rms": 0.012,
                "mean_abs": 0.002,
                "peak": 0.11,
                "zero_crossing_ratio": 0.01,
                "speech_ratio": 0.95,
                "silence_ratio": 0.05,
            },
        }

    monkeypatch.setattr(memorial_voice_profile, "_download_youtube_audio", _fake_download_youtube_audio)
    monkeypatch.setattr(memorial_voice_profile, "_compute_audio_signature", _fake_compute_signature)

    client = _client(principal_id="exec-public-memorial-voice-profile")
    config = client.get(f"/memorials/{slug}/voice-config")
    assert config.status_code == 200
    initial_config = config.json()
    assert initial_config["voice_label"] == "Austauschbare synthetische Stimme"
    assert initial_config["voice_profile_ready"] is False

    saved = client.post(
        f"/memorials/{slug}/voice-config",
        json={
            "voice_label": "Archiv Stimme",
            "lang": "de-DE",
            "rate": 1.05,
            "pitch": 0.98,
            "volume": 0.92,
            "voice_name_hints": ["de-DE", "de-AT", "male"],
            "synthetic_voice_clone_of_memorial_person": True,
            "provider_secret": "not-allowed",
        },
    )
    assert saved.status_code == 200
    saved_body = saved.json()
    assert saved_body["voice_label"] == "Archiv Stimme"
    assert saved_body["lang"] == "de-DE"
    assert saved_body["rate"] == 1.05
    assert saved_body["pitch"] == 0.98
    assert saved_body["volume"] == 0.92
    assert saved_body["tts_mode"] == "browser_speech_synthesis"
    assert saved_body["synthetic_voice_clone_of_memorial_person"] is False
    assert "provider_secret" not in saved_body

    manifest = private_root / slug / "voice_profile_manifest.json"
    assert not manifest.exists()

    build = client.post(
        f"/memorials/{slug}/voice-profile/build",
        json={
            "youtube_query": "Manfred Hoza interview",
            "youtube_urls": "https://www.youtube.com/watch?v=abc\nhttps://www.youtube.com/watch?v=xyz",
            "youtube_limit": 2,
        },
    )
    assert build.status_code == 200
    build_body = build.json()
    assert build_body["voice_profile_slug"] == slug
    assert build_body["voice_profile_ready"] is True
    assert build_body["voice_profile_sources"]["public_clips"] >= 1
    assert build_body["voice_profile_sources"]["youtube_downloads"] >= 1
    assert manifest.exists()

    summary = client.get(f"/memorials/{slug}/voice-profile")
    assert summary.status_code == 200
    summary_body = summary.json()
    assert summary_body["voice_profile_slug"] == slug
    assert summary_body["voice_profile_ready"] is True

    stored_config_path = private_root / slug / "tts_voice.json"
    assert stored_config_path.is_file()
    stored_config = json.loads(stored_config_path.read_text(encoding="utf-8"))
    assert stored_config["voice_label"] == "Archiv Stimme"
    assert stored_config["tts_mode"] == "browser_speech_synthesis"
    assert stored_config["synthetic_voice_clone_of_memorial_person"] is False
    assert "provider_secret" not in stored_config
    assert "voice_name_hints" in stored_config

    empty_slug = "manfred-no-source"
    empty_bundle = public_root / empty_slug
    empty_bundle.mkdir(parents=True)
    (empty_bundle / "memorial.json").write_text(json.dumps({"slug": empty_slug, "person_name": "Nobody"}, ensure_ascii=False), encoding="utf-8")
    failed = client.post(f"/memorials/{empty_slug}/voice-profile/build", json={"youtube_query": "", "youtube_urls": ""})
    assert failed.status_code == 400
    failed_json = failed.json()
    failed_detail = (
        failed_json.get("detail")
        or failed_json.get("error", {}).get("message")
        or failed_json.get("error", {}).get("code")
    )
    assert failed_detail == "voice_profile_no_source"


def test_public_side_surfaces_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_ENABLE_PUBLIC_SIDE_SURFACES", "0")
    monkeypatch.setenv("EA_ENABLE_PUBLIC_RESULTS", "0")
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "0")
    monkeypatch.setenv("EA_ENABLE_PUBLIC_MEMORIALS", "0")
    client = _client(principal_id="exec-public-disabled")

    for response in (
        client.get("/tours/example-tour"),
        client.get("/results/example-result"),
        client.get("/memorials/example-person"),
    ):
        assert response.status_code == 404
        error = response.json()["error"]
        assert error["code"] == "Not Found"
        assert error["message"] == "Not Found"
        assert error["details"] == "Not Found"
        assert error["correlation_id"]


def test_public_results_and_tours_can_be_enabled_independently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    result_dir = tmp_path / "results"
    result_bundle = result_dir / "movie-demo"
    result_bundle.mkdir(parents=True)
    (result_bundle / "asset.html").write_text("<html><body>movie</body></html>", encoding="utf-8")
    (result_bundle / "result.json").write_text(
        json.dumps(
            {
                "slug": "movie-demo",
                "title": "Movie Demo",
                "service_key": "mootion_movie",
                "summary": "Demo movie",
                "body_text": "Demo movie",
                "mime_type": "text/html",
                "viewer_kind": "html",
                "asset_relpath": "asset.html",
                "hosted_url": "https://ea.example/results/movie-demo",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EA_ENABLE_PUBLIC_RESULTS", "1")
    monkeypatch.setenv("EA_ENABLE_PUBLIC_TOURS", "0")
    monkeypatch.setenv("EA_PUBLIC_RESULT_DIR", str(result_dir))

    client = _client(principal_id="exec-public-result-only")

    assert client.get("/results/movie-demo").status_code == 200
    assert client.get("/tours/movie-demo").status_code == 404


def test_onemin_manager_binding_overlay_and_occupancy_are_principal_scoped() -> None:
    from types import SimpleNamespace

    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    provider_health = {
        "providers": {
            "onemin": {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "slot_env_name": "ONEMIN_AI_API_KEY",
                        "slot": "primary",
                        "slot_name": "primary",
                        "credential_id": "primary",
                        "state": "ready",
                        "estimated_remaining_credits": 15000,
                    }
                ]
            }
        }
    }
    binding = SimpleNamespace(
        binding_id="binding-1",
        auth_metadata_json={"slot_env_name": "ONEMIN_AI_API_KEY"},
        external_account_ref="",
    )

    first_view = manager.accounts_snapshot(provider_health=provider_health, binding_rows=[binding])
    assert first_view[0]["browseract_binding_ids"] == ["binding-1"]

    second_view = manager.accounts_snapshot(provider_health=provider_health, binding_rows=[])
    assert second_view[0]["browseract_binding_ids"] == []

    aggregate = manager.aggregate_snapshot(provider_health=provider_health, binding_rows=[], principal_id="exec-2")
    assert aggregate["bound_account_count"] == 0
    assert aggregate["bound_actual_free_credits_total"] == 0

    lease = manager.reserve_for_candidates(
        candidates=[
            {
                "account_name": "ONEMIN_AI_API_KEY",
                "account_id": "ONEMIN_AI_API_KEY",
                "slot_name": "primary",
                "credential_id": "primary",
                "secret_env_name": "ONEMIN_AI_API_KEY",
                "state": "ready",
                "estimated_remaining_credits": 15000,
                "api_key": "test-key",
            }
        ],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-1",
        estimated_credits=50,
        allow_reserve=False,
    )
    assert lease is not None
    assert manager.occupancy_snapshot(principal_id="exec-1")["active_lease_count"] == 1


def test_onemin_manager_does_not_count_unparsed_page_views_as_actual_billing() -> None:
    from types import SimpleNamespace

    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    provider_health = {
        "providers": {
            "onemin": {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "slot_env_name": "ONEMIN_AI_API_KEY",
                        "slot": "primary",
                        "slot_name": "primary",
                        "credential_id": "primary",
                        "state": "ready",
                        "estimated_remaining_credits": 15572,
                        "billing_basis": "page_seen_but_unparsed",
                        "last_billing_snapshot_at": "2026-03-27T21:24:46Z",
                    }
                ]
            }
        }
    }
    binding = SimpleNamespace(
        binding_id="binding-1",
        auth_metadata_json={"slot_env_name": "ONEMIN_AI_API_KEY"},
        external_account_ref="",
    )

    aggregate = manager.aggregate_snapshot(provider_health=provider_health, binding_rows=[], principal_id="")
    actual = manager.actual_credits_snapshot(provider_health=provider_health, binding_rows=[binding], principal_id="exec-1")

    assert aggregate["actual_billing_account_count"] == 0
    assert aggregate["actual_free_credits_total"] == 0
    assert aggregate["account_count"] == 1
    assert actual["actual_billing_account_count"] == 0
    assert actual["binding_account_count"] == 1
    assert actual["accounts_without_actual_billing_count"] == 1
    assert manager.occupancy_snapshot(principal_id="exec-2")["active_lease_count"] == 0


def test_onemin_manager_reserve_for_candidates_prefers_persisted_actual_credits() -> None:
    from app.domain.models import OneminAccount, OneminCredential
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    repo = InMemoryOneminManagerRepository()
    manager = OneminManagerService(repo=repo)
    repo.replace_state(
        accounts=[
            OneminAccount(
                account_id="ONEMIN_AI_API_KEY_FALLBACK_60",
                account_label="ONEMIN_AI_API_KEY_FALLBACK_60",
                status="ready",
                remaining_credits=1049,
                max_credits=15000,
                details_json={
                    "credit_basis": "actual_billing_usage_page",
                    "has_actual_billing": True,
                    "actual_remaining_credits": 1049.0,
                    "actual_max_credits": 15000.0,
                },
            ),
            OneminAccount(
                account_id="ONEMIN_AI_API_KEY_FALLBACK_61",
                account_label="ONEMIN_AI_API_KEY_FALLBACK_61",
                status="ready",
                remaining_credits=40000,
                max_credits=15000,
                details_json={
                    "credit_basis": "actual_billing_usage_page",
                    "has_actual_billing": True,
                    "actual_remaining_credits": 40000.0,
                    "actual_max_credits": 15000.0,
                },
            ),
        ],
        credentials=[
            OneminCredential(
                credential_id="fallback_60",
                account_id="ONEMIN_AI_API_KEY_FALLBACK_60",
                slot_name="fallback_60",
                secret_env_name="ONEMIN_AI_API_KEY_FALLBACK_60",
                state="ready",
                remaining_credits=1049,
            ),
            OneminCredential(
                credential_id="fallback_61",
                account_id="ONEMIN_AI_API_KEY_FALLBACK_61",
                slot_name="fallback_61",
                secret_env_name="ONEMIN_AI_API_KEY_FALLBACK_61",
                state="ready",
                remaining_credits=40000,
            ),
        ],
    )

    lease = manager.reserve_for_candidates(
        candidates=[
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "account_id": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "slot_name": "fallback_60",
                "credential_id": "fallback_60",
                "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "state": "ready",
                "estimated_remaining_credits": 5000000,
                "api_key": "low-key",
            },
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
                "account_id": "ONEMIN_AI_API_KEY_FALLBACK_61",
                "slot_name": "fallback_61",
                "credential_id": "fallback_61",
                "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
                "state": "ready",
                "estimated_remaining_credits": None,
                "api_key": "high-key",
            },
        ],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-actual-credits",
        estimated_credits=25662,
        allow_reserve=False,
    )

    assert lease is not None
    assert lease["account_name"] == "ONEMIN_AI_API_KEY_FALLBACK_61"
    assert lease["api_key"] == "high-key"


def test_onemin_manager_keeps_unknown_budget_candidates_eligible_when_known_budget_is_insufficient() -> None:
    from app.domain.models import OneminAccount, OneminCredential
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    repo = InMemoryOneminManagerRepository()
    manager = OneminManagerService(repo=repo)
    repo.replace_state(
        accounts=[
            OneminAccount(
                account_id="ONEMIN_AI_API_KEY_FALLBACK_60",
                account_label="ONEMIN_AI_API_KEY_FALLBACK_60",
                status="ready",
                remaining_credits=1049,
                max_credits=15000,
                details_json={
                    "credit_basis": "actual_billing_usage_page",
                    "has_actual_billing": True,
                    "actual_remaining_credits": 1049.0,
                    "actual_max_credits": 15000.0,
                },
            )
        ],
        credentials=[
            OneminCredential(
                credential_id="fallback_60",
                account_id="ONEMIN_AI_API_KEY_FALLBACK_60",
                slot_name="fallback_60",
                secret_env_name="ONEMIN_AI_API_KEY_FALLBACK_60",
                state="ready",
                remaining_credits=1049,
            )
        ],
    )

    lease = manager.reserve_for_candidates(
        candidates=[
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "account_id": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "slot_name": "fallback_60",
                "credential_id": "fallback_60",
                "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "state": "ready",
                "estimated_remaining_credits": 5000000,
                "api_key": "low-key",
            },
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_62",
                "account_id": "ONEMIN_AI_API_KEY_FALLBACK_62",
                "slot_name": "fallback_62",
                "credential_id": "fallback_62",
                "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_62",
                "state": "ready",
                "estimated_remaining_credits": None,
                "api_key": "unknown-key",
            },
        ],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-unknown-budget",
        estimated_credits=25662,
        allow_reserve=False,
    )

    assert lease is not None
    assert lease["account_name"] == "ONEMIN_AI_API_KEY_FALLBACK_62"
    assert lease["api_key"] == "unknown-key"


def test_onemin_manager_budget_limited_quarantine_only_blocks_requests_above_observed_remaining() -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    candidate = {
        "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
        "account_id": "ONEMIN_AI_API_KEY_FALLBACK_60",
        "slot_name": "fallback_60",
        "credential_id": "fallback_60",
        "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
        "state": "quarantine",
        "remaining_credits": 1650,
        "estimated_remaining_credits": 1650,
        "billing_remaining_credits": 4_200_000,
        "last_error": "INSUFFICIENT_CREDITS:The feature requires 57451 credits, but the Finland Office team only has 1650 credits",
        "api_key": "budget-key",
    }

    lease = manager.reserve_for_candidates(
        candidates=[candidate],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-budget-recovery",
        estimated_credits=1200,
        allow_reserve=False,
    )

    assert lease is not None
    assert lease["api_key"] == "budget-key"
    manager.release_lease(lease_id=str(lease["lease_id"]))

    oversized = manager.reserve_for_candidates(
        candidates=[candidate],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-budget-too-large",
        estimated_credits=50000,
        allow_reserve=False,
    )

    assert oversized is None


def test_onemin_manager_probe_ok_allows_billing_backed_budget_override() -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    candidate = {
        "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
        "account_id": "ONEMIN_AI_API_KEY_FALLBACK_60",
        "slot_name": "fallback_60",
        "credential_id": "fallback_60",
        "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
        "state": "quarantine",
        "remaining_credits": 1650,
        "estimated_remaining_credits": 1650,
        "billing_remaining_credits": 4_200_000,
        "billing_max_credits": 4_450_000,
        "billing_basis": "actual_provider_api",
        "last_probe_result": "ok",
        "last_error": "INSUFFICIENT_CREDITS:The feature requires 73111 credits, but the Finland Office team only has 1650 credits",
        "api_key": "probe-ok-key",
    }

    lease = manager.reserve_for_candidates(
        candidates=[candidate],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-probe-ok-budget-override",
        estimated_credits=73111,
        allow_reserve=False,
    )

    assert lease is not None
    assert lease["api_key"] == "probe-ok-key"


def test_onemin_manager_probe_ok_does_not_trust_mismatched_billing_override() -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    candidate = {
        "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
        "account_id": "ONEMIN_AI_API_KEY_FALLBACK_60",
        "slot_name": "fallback_60",
        "credential_id": "fallback_60",
        "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
        "state": "quarantine",
        "remaining_credits": 1650,
        "estimated_remaining_credits": 1650,
        "billing_remaining_credits": 4_200_000,
        "billing_max_credits": 4_450_000,
        "billing_basis": "actual_provider_api",
        "billing_team_mismatch": True,
        "last_probe_result": "ok",
        "last_error": "INSUFFICIENT_CREDITS:The feature requires 73111 credits, but the Finland Office team only has 1650 credits",
        "api_key": "probe-ok-key",
    }

    lease = manager.reserve_for_candidates(
        candidates=[candidate],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-probe-ok-mismatch",
        estimated_credits=73111,
        allow_reserve=False,
    )

    assert lease is None


def test_onemin_manager_probe_ok_billing_override_does_not_mask_zero_live_balance() -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    candidates = [
        {
            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
            "account_id": "ONEMIN_AI_API_KEY_FALLBACK_60",
            "slot_name": "fallback_60",
            "credential_id": "fallback_60",
            "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
            "state": "degraded",
            "remaining_credits": 0,
            "estimated_remaining_credits": 0,
            "billing_remaining_credits": 4_200_000,
            "billing_max_credits": 4_450_000,
            "billing_basis": "actual_provider_api",
            "last_probe_result": "ok",
            "last_error": "INSUFFICIENT_CREDITS:The feature requires 1877 credits, but the team only has 0 credits",
            "api_key": "zero-live-key",
        },
        {
            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
            "account_id": "ONEMIN_AI_API_KEY_FALLBACK_61",
            "slot_name": "fallback_61",
            "credential_id": "fallback_61",
            "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
            "state": "quarantine",
            "remaining_credits": 1650,
            "estimated_remaining_credits": 1650,
            "billing_remaining_credits": 4_200_000,
            "billing_max_credits": 4_450_000,
            "billing_basis": "actual_provider_api",
            "last_error": "INSUFFICIENT_CREDITS:The feature requires 1726 credits, but the team only has 1650 credits",
            "api_key": "positive-live-key",
        },
    ]

    lease = manager.reserve_for_candidates(
        candidates=candidates,
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-zero-live-mask",
        estimated_credits=699,
        allow_reserve=False,
    )

    assert lease is not None
    assert lease["api_key"] == "positive-live-key"


def test_onemin_manager_prefers_exact_live_budget_before_billing_backed_recovery() -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    candidates = [
        {
            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
            "account_id": "ONEMIN_AI_API_KEY_FALLBACK_60",
            "slot_name": "fallback_60",
            "credential_id": "fallback_60",
            "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
            "state": "quarantine",
            "remaining_credits": 1650,
            "estimated_remaining_credits": 1650,
            "billing_remaining_credits": 4_200_000,
            "billing_max_credits": 4_450_000,
            "billing_basis": "actual_provider_api",
            "last_error": "INSUFFICIENT_CREDITS:The feature requires 57451 credits, but the team only has 1650 credits",
            "last_probe_result": "ok",
            "api_key": "billing-recovery-key",
        },
        {
            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
            "account_id": "ONEMIN_AI_API_KEY_FALLBACK_61",
            "slot_name": "fallback_61",
            "credential_id": "fallback_61",
            "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
            "state": "ready",
            "remaining_credits": 2400,
            "estimated_remaining_credits": 2400,
            "api_key": "exact-live-key",
        },
    ]

    lease = manager.reserve_for_candidates(
        candidates=candidates,
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-exact-live-preferred",
        estimated_credits=699,
        allow_reserve=False,
    )

    assert lease is not None
    assert lease["api_key"] == "exact-live-key"


def test_onemin_manager_uses_billing_backed_recovery_after_new_success_or_billing_snapshot() -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    now = time.time()
    lease = manager.reserve_for_candidates(
        candidates=[
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "account_id": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "slot_name": "fallback_60",
                "credential_id": "fallback_60",
                "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "state": "quarantine",
                "remaining_credits": 1650,
                "estimated_remaining_credits": 1650,
                "billing_remaining_credits": 4_200_000,
                "billing_max_credits": 4_450_000,
                "billing_basis": "actual_provider_api",
                "last_error": "INSUFFICIENT_CREDITS:The feature requires 57451 credits, but the team only has 1650 credits",
                "last_failure_at": now - 600,
                "last_success_at": now - 60,
                "api_key": "recent-success-key",
            },
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
                "account_id": "ONEMIN_AI_API_KEY_FALLBACK_61",
                "slot_name": "fallback_61",
                "credential_id": "fallback_61",
                "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
                "state": "quarantine",
                "remaining_credits": 1650,
                "estimated_remaining_credits": 1650,
                "billing_remaining_credits": 4_200_000,
                "billing_max_credits": 4_450_000,
                "billing_basis": "actual_provider_api",
                "last_error": "INSUFFICIENT_CREDITS:The feature requires 57451 credits, but the team only has 1650 credits",
                "last_failure_at": now - 600,
                "last_billing_snapshot_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 30)),
                "api_key": "fresh-billing-key",
            },
        ],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-billing-recovery",
        estimated_credits=50000,
        allow_reserve=False,
    )

    assert lease is not None
    assert lease["api_key"] in {"recent-success-key", "fresh-billing-key"}


def test_onemin_manager_uses_fresh_billing_snapshot_to_override_older_depleted_probe() -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    lease = manager.reserve_for_candidates(
        candidates=[
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "account_id": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "slot_name": "fallback_60",
                "credential_id": "fallback_60",
                "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                "state": "ready",
                "remaining_credits": 0,
                "estimated_remaining_credits": 0,
                "billing_remaining_credits": 4_255_550,
                "billing_max_credits": 4_450_000,
                "billing_basis": "actual_provider_api",
                "last_probe_result": "depleted",
                "last_error": "INSUFFICIENT_CREDITS:The feature requires 1726 credits, but the team only has 0 credits",
                "last_probe_at": 1000.0,
                "last_billing_snapshot_at": "2026-04-30T14:23:21Z",
                "api_key": "fresh-billing-key",
            },
            {
                "account_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
                "account_id": "ONEMIN_AI_API_KEY_FALLBACK_61",
                "slot_name": "fallback_61",
                "credential_id": "fallback_61",
                "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_61",
                "state": "ready",
                "remaining_credits": 0,
                "estimated_remaining_credits": 0,
                "billing_remaining_credits": 4_255_550,
                "billing_max_credits": 4_450_000,
                "billing_basis": "actual_provider_api",
                "last_probe_result": "depleted",
                "last_error": "INSUFFICIENT_CREDITS:The feature requires 1726 credits, but the team only has 0 credits",
                "last_probe_at": 2000.0,
                "last_billing_snapshot_at": "1970-01-01T00:00:01Z",
                "api_key": "stale-billing-key",
            },
        ],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-fresh-billing-overrides-stale-probe",
        estimated_credits=1726,
        allow_reserve=False,
    )

    assert lease is not None
    assert lease["api_key"] == "fresh-billing-key"


def test_onemin_manager_candidate_repo_state_preserves_zero_live_estimate_over_persisted_billing() -> None:
    from app.domain.models import OneminAccount, OneminCredential
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    repo = InMemoryOneminManagerRepository()
    manager = OneminManagerService(repo=repo)
    repo.replace_state(
        accounts=[
            OneminAccount(
                account_id="ONEMIN_AI_API_KEY_FALLBACK_48",
                account_label="ONEMIN_AI_API_KEY_FALLBACK_48",
                status="ready",
                remaining_credits=16169,
                max_credits=16169,
                details_json={
                    "credit_basis": "actual_provider_api",
                    "has_actual_billing": True,
                    "actual_remaining_credits": 16169.0,
                    "actual_max_credits": 16169.0,
                    "estimated_remaining_credits": 0.0,
                },
            )
        ],
        credentials=[
            OneminCredential(
                credential_id="fallback_48",
                account_id="ONEMIN_AI_API_KEY_FALLBACK_48",
                slot_name="fallback_48",
                secret_env_name="ONEMIN_AI_API_KEY_FALLBACK_48",
                state="ready",
                remaining_credits=16169,
            )
        ],
    )

    candidate = {
        "account_name": "ONEMIN_AI_API_KEY_FALLBACK_48",
        "account_id": "ONEMIN_AI_API_KEY_FALLBACK_48",
        "slot_name": "fallback_48",
        "credential_id": "fallback_48",
        "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_48",
        "state": "ready",
        "remaining_credits": None,
        "estimated_remaining_credits": 0,
        "billing_remaining_credits": 16169,
        "billing_max_credits": 16169,
        "billing_basis": "actual_provider_api",
        "last_probe_result": "depleted",
        "last_probe_detail": "INSUFFICIENT_CREDITS:The feature requires 1726 credits, but the team only has 1049 credits",
        "api_key": "zero-estimate-key",
    }

    lease = manager.reserve_for_candidates(
        candidates=[candidate],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-preserve-zero-estimate",
        estimated_credits=699,
        allow_reserve=False,
    )

    assert lease is None


def test_onemin_manager_actual_snapshot_ignores_mismatched_actual_billing() -> None:
    from types import SimpleNamespace

    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    provider_health = {
        "providers": {
            "onemin": {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                        "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60",
                        "slot": "fallback_60",
                        "slot_name": "fallback_60",
                        "credential_id": "fallback_60",
                        "state": "degraded",
                        "remaining_credits": 1650,
                        "estimated_remaining_credits": 1650,
                        "estimated_credit_basis": "observed_error",
                        "billing_remaining_credits": 4_200_000,
                        "billing_max_credits": 4_450_000,
                        "billing_basis": "actual_provider_api",
                        "billing_team_name": "Aziliz Tanguy",
                        "billing_team_mismatch": True,
                        "billing_team_match_subject": "Finland Office team",
                    }
                ]
            }
        }
    }
    binding = SimpleNamespace(
        binding_id="binding-1",
        auth_metadata_json={"slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_60"},
        external_account_ref="",
    )

    actual = manager.actual_credits_snapshot(provider_health=provider_health, binding_rows=[binding], principal_id="exec-1")
    accounts = manager.accounts_snapshot(provider_health=provider_health, binding_rows=[binding], principal_id="exec-1")

    assert actual["actual_billing_account_count"] == 0
    assert actual["actual_free_credits_total"] == 0
    assert actual["accounts_without_actual_billing_count"] == 1
    assert accounts[0]["has_actual_billing"] is False
    assert accounts[0]["actual_remaining_credits"] is None
    assert accounts[0]["estimated_remaining_credits"] == 1650
    assert accounts[0]["credit_basis"] == "observed_error"


def test_onemin_manager_non_authoritative_provider_health_does_not_block_persisted_ready_account() -> None:
    from app.domain.models import OneminAccount, OneminCredential
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    manager._repo.replace_state(
        accounts=[
            OneminAccount(
                account_id="ONEMIN_AI_API_KEY_FALLBACK_1",
                account_label="ONEMIN_AI_API_KEY_FALLBACK_1",
                status="ready",
                remaining_credits=40000,
                max_credits=15000,
                last_billing_snapshot_at="2026-04-28T08:30:00Z",
                details_json={
                    "credit_basis": "actual_billing_usage_page",
                    "has_actual_billing": True,
                    "actual_remaining_credits": 40000.0,
                    "actual_max_credits": 15000.0,
                },
            )
        ],
        credentials=[
            OneminCredential(
                credential_id="fallback_1",
                account_id="ONEMIN_AI_API_KEY_FALLBACK_1",
                slot_name="fallback_1",
                secret_env_name="ONEMIN_AI_API_KEY_FALLBACK_1",
                state="ready",
                remaining_credits=40000,
            )
        ],
    )
    candidate = {
        "account_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
        "account_id": "ONEMIN_AI_API_KEY_FALLBACK_1",
        "slot_name": "fallback_1",
        "credential_id": "fallback_1",
        "secret_env_name": "ONEMIN_AI_API_KEY_FALLBACK_1",
        "state": "quarantine",
        "slot_role": "active",
        "remaining_credits": 0,
        "estimated_remaining_credits": 0,
        "billing_remaining_credits": 0,
        "last_probe_result": "depleted",
        "api_key": "high-key",
    }
    provider_health = {
        "providers": {
            "onemin": {
                "slots": [candidate],
            }
        }
    }

    lease = manager.reserve_for_candidates(
        candidates=[candidate],
        lane="core",
        capability="code_generate",
        principal_id="exec-1",
        request_id="req-non-authoritative-provider-health",
        estimated_credits=25662,
        allow_reserve=False,
        provider_health=provider_health,
    )

    assert lease is not None
    assert lease["api_key"] == "high-key"
    assert lease["account_name"] == "ONEMIN_AI_API_KEY_FALLBACK_1"


def test_refresh_onemin_api_account_uses_credit_subject_hint_to_select_matching_team(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import providers as providers_route

    requested_urls: list[str] = []

    monkeypatch.setattr(
        providers_route,
        "_onemin_api_login",
        lambda **_: {
            "token": "session-token",
            "teams": [
                {"teamId": "team-wrong", "team": {"uuid": "team-wrong", "name": "Aziliz Tanguy"}},
                {"teamId": "team-right", "team": {"uuid": "team-right", "name": "Finland Office"}},
            ],
        },
    )

    def fake_get_json(*, url: str, headers: dict[str, str], timeout_seconds: int) -> dict[str, object]:
        requested_urls.append(url)
        assert headers["X-Auth-Token"] == "Bearer session-token"
        if url.endswith("/topups"):
            return {"topupList": []}
        if url.endswith("/usages"):
            return {"usageList": []}
        if url.endswith("/invoices"):
            return {"invoiceList": []}
        raise AssertionError(url)

    monkeypatch.setattr(providers_route, "_onemin_api_get_json", fake_get_json)
    monkeypatch.setattr(
        providers_route.upstream,
        "onemin_credit_subject_hint_for_account",
        lambda *, account_name: {"credit_subject": "Finland Office team"} if account_name == "ONEMIN_AI_API_KEY_FALLBACK_60" else {},
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "_latest_provider_billing_snapshot",
        lambda **_: None,
        raising=False,
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "record_onemin_billing_snapshot",
        lambda **kwargs: dict(kwargs["snapshot_json"]),
    )

    billing_result, member_result = providers_route._refresh_onemin_api_account(
        account_name="ONEMIN_AI_API_KEY_FALLBACK_60",
        owner_email="owner@example.com",
        include_members=False,
        timeout_seconds=120,
    )

    assert member_result is None
    assert billing_result["team_id"] == "team-right"
    assert billing_result["structured_output_json"]["team_name"] == "Finland Office"
    assert billing_result["structured_output_json"]["team_selection"]["reason"] == "credit_subject_hint"
    assert all("/team-right/" in url for url in requested_urls)


def test_refresh_onemin_api_account_prefers_configured_team_id_over_credit_subject_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.routes import providers as providers_route

    requested_urls: list[str] = []

    monkeypatch.setattr(
        providers_route,
        "_onemin_api_login",
        lambda **_: {
            "token": "session-token",
            "teams": [
                {"teamId": "team-wrong", "team": {"uuid": "team-wrong", "name": "Finland Office"}},
                {"teamId": "team-right", "team": {"uuid": "team-right", "name": "Saga Silfverberg"}},
            ],
        },
    )

    def fake_get_json(*, url: str, headers: dict[str, str], timeout_seconds: int) -> dict[str, object]:
        requested_urls.append(url)
        assert headers["X-Auth-Token"] == "Bearer session-token"
        if url.endswith("/topups"):
            return {"topupList": []}
        if url.endswith("/usages"):
            return {"usageList": []}
        if url.endswith("/invoices"):
            return {"invoiceList": []}
        raise AssertionError(url)

    monkeypatch.setattr(providers_route, "_onemin_api_get_json", fake_get_json)
    monkeypatch.setattr(
        providers_route.upstream,
        "onemin_credit_subject_hint_for_account",
        lambda *, account_name: {"credit_subject": "Finland Office team"} if account_name == "ONEMIN_AI_API_KEY_FALLBACK_60" else {},
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "_latest_provider_billing_snapshot",
        lambda **_: None,
        raising=False,
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "record_onemin_billing_snapshot",
        lambda **kwargs: dict(kwargs["snapshot_json"]),
    )

    billing_result, member_result = providers_route._refresh_onemin_api_account(
        account_name="ONEMIN_AI_API_KEY_FALLBACK_60",
        owner_email="owner@example.com",
        include_members=False,
        timeout_seconds=120,
        preferred_team_id="team-right",
    )

    assert member_result is None
    assert billing_result["team_id"] == "team-right"
    assert billing_result["structured_output_json"]["team_name"] == "Saga Silfverberg"
    assert billing_result["structured_output_json"]["team_selection"]["reason"] == "configured_team_id"
    assert all("/team-right/" in url for url in requested_urls)


def test_operator_can_record_onemin_billing_snapshot_into_live_manager_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_RESPONSES_PROVIDER_LEDGER_DIR", str(tmp_path))
    monkeypatch.setenv("ONEMIN_AI_API_KEY_FALLBACK_54", "business-54")

    owner = _client(principal_id="codex-fleet", operator=True)

    recorded = owner.post(
        "/v1/providers/onemin/billing-snapshots",
        json={
            "account_label": "ONEMIN_AI_API_KEY_FALLBACK_54",
            "source": "browseract.onemin_billing_usage.fastestvpn_refresh",
            "snapshot_json": {
                "observed_at": "2026-04-04T08:24:48Z",
                "remaining_credits": 4280000,
                "max_credits": 4280000,
                "basis": "actual_billing_usage_page",
                "source_url": "https://app.1min.ai/billing-usage",
                "structured_output_json": {
                    "billing_overview_json": {
                        "plan_name": "BUSINESS",
                        "billing_cycle": "LIFETIME",
                        "subscription_status": "Active",
                    }
                },
            },
        },
    )

    assert recorded.status_code == 200
    body = recorded.json()
    assert body["snapshot"]["account_name"] == "ONEMIN_AI_API_KEY_FALLBACK_54"
    assert body["snapshot"]["remaining_credits"] == 4280000.0
    assert body["account_snapshot"]["account_id"] == "ONEMIN_AI_API_KEY_FALLBACK_54"
    assert body["account_snapshot"]["actual_remaining_credits"] == 4280000.0
    assert body["account_snapshot"]["credit_basis"] == "actual_billing_usage_page"
    assert body["account_snapshot"]["has_actual_billing"] is True
    assert body["aggregate_snapshot"]["sum_free_credits"] == 4280000.0
    assert body["aggregate_snapshot"]["actual_free_credits_total"] == 4280000.0
    assert body["aggregate_snapshot"]["actual_billing_account_count"] == 1

    aggregate = owner.get("/v1/providers/onemin/aggregate?scope=global")
    assert aggregate.status_code == 200
    aggregate_body = aggregate.json()
    assert aggregate_body["sum_free_credits"] == 4280000.0
    assert aggregate_body["actual_free_credits_total"] == 4280000.0
    assert aggregate_body["actual_billing_account_count"] == 1


def test_onemin_image_reservation_and_release_are_principal_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = _client(principal_id="exec-image", operator=True)
    from app.api.routes import providers as providers_route

    monkeypatch.setattr(
        providers_route.upstream,
        "_provider_health_report",
        lambda: {
            "providers": {
                "onemin": {
                    "slots": [
                        {
                            "account_name": "ONEMIN_AI_API_KEY_FALLBACK_22",
                            "slot_env_name": "ONEMIN_AI_API_KEY_FALLBACK_22",
                            "slot": "fallback_22",
                            "slot_name": "fallback_22",
                            "credential_id": "fallback_22",
                            "state": "ready",
                            "estimated_remaining_credits": 24000,
                            "slot_role": "image",
                        }
                    ]
                }
            }
        },
    )

    reserved = owner.post("/v1/providers/onemin/reserve-image", json={"estimated_credits": 900})
    assert reserved.status_code == 200
    reserved_body = reserved.json()
    assert reserved_body["principal_id"] == "exec-image"
    assert reserved_body["secret_env_name"] == "ONEMIN_AI_API_KEY_FALLBACK_22"
    lease_id = reserved_body["lease_id"]

    occupancy = owner.get("/v1/providers/onemin/occupancy")
    assert occupancy.status_code == 200
    assert occupancy.json()["active_lease_count"] == 1

    foreign = owner.post(
        f"/v1/providers/onemin/leases/{lease_id}/release",
        json={"status": "released"},
        headers={"X-EA-Principal-ID": "exec-foreign"},
    )
    assert foreign.status_code == 404

    released = owner.post(
        f"/v1/providers/onemin/leases/{lease_id}/release",
        json={"status": "released", "actual_credits_delta": 900},
    )
    assert released.status_code == 200
    assert released.json()["actual_credits_delta"] == 900


def test_onemin_aggregate_exposes_media_and_core_lease_breakout() -> None:
    from app.repositories.onemin_manager import InMemoryOneminManagerRepository
    from app.services.onemin_manager import OneminManagerService

    manager = OneminManagerService(repo=InMemoryOneminManagerRepository())
    provider_health = {
        "providers": {
            "onemin": {
                "slots": [
                    {
                        "account_name": "ONEMIN_AI_API_KEY",
                        "slot_env_name": "ONEMIN_AI_API_KEY",
                        "slot": "primary",
                        "slot_name": "primary",
                        "credential_id": "primary",
                        "state": "ready",
                        "estimated_remaining_credits": 15000,
                        "slot_role": "mixed",
                    }
                ]
            }
        }
    }

    image = manager.reserve_for_provider_health(
        provider_health=provider_health,
        lane="image",
        capability="image_generate",
        principal_id="exec-image",
        request_id="img-1",
        estimated_credits=800,
        allow_reserve=False,
    )
    assert image is not None
    manager.record_usage(lease_id=str(image["lease_id"]), actual_credits_delta=800, status="success")
    manager.release_lease(lease_id=str(image["lease_id"]), status="released")

    core = manager.reserve_for_provider_health(
        provider_health=provider_health,
        lane="core",
        capability="code_generate",
        principal_id="exec-core",
        request_id="core-1",
        estimated_credits=300,
        allow_reserve=False,
    )
    assert core is not None

    aggregate = manager.aggregate_snapshot(provider_health=provider_health, binding_rows=[], principal_id="exec-core")
    assert aggregate["active_image_generation_lease_count"] == 0
    assert aggregate["active_core_code_lease_count"] == 1
    assert aggregate["lease_actual_credits_by_task_class"]["image_generation"] == 800.0
