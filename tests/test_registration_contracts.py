from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.product_test_helpers import start_workspace


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("EA_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("EA_API_TOKEN", "")
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")
    from app.api.app import create_app

    return TestClient(create_app())


def _configure_id_austria(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ID_AUSTRIA_CLIENT_ID", "https://propertyquarry.com")
    monkeypatch.setenv("PROPERTYQUARRY_ID_AUSTRIA_CLIENT_SECRET", "test-id-austria-client-secret")
    monkeypatch.setenv("PROPERTYQUARRY_ID_AUSTRIA_REDIRECT_URI", "https://propertyquarry.com/id-austria/callback")
    monkeypatch.setenv("PROPERTYQUARRY_ID_AUSTRIA_STATE_SECRET", "test-id-austria-state-secret")
    monkeypatch.setenv("PROPERTYQUARRY_ID_AUSTRIA_ENVIRONMENT", "production")


def _configure_google_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://propertyquarry.com/google/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "test-google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "test-provider-secret-key")
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI", "https://propertyquarry.com/google/callback")
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET", "test-propertyquarry-google-state-secret")
    monkeypatch.setenv("PROPERTYQUARRY_IDENTITY_SESSION_SECRET", "test-propertyquarry-identity-session-secret")


def _clear_google_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "EA_GOOGLE_OAUTH_CLIENT_ID",
        "EA_GOOGLE_OAUTH_CLIENT_SECRET",
        "EA_GOOGLE_OAUTH_REDIRECT_URI",
        "EA_GOOGLE_OAUTH_STATE_SECRET",
        "EA_PROVIDER_SECRET_KEY",
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
        "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI",
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


def test_register_template_uses_named_secure_verification_links() -> None:
    source = (Path(__file__).resolve().parents[1] / "ea/app/templates/register.html").read_text(encoding="utf-8")

    assert ">${escapeHtml(absoluteMagicLink)}</a>" not in source
    assert "magic link" not in source.lower()
    assert "verification mail" not in source.lower()
    assert "the secure verification link" in source
    assert 'data-register-return-to="{{ register_return_to }}"' in source
    assert 'data-register-progress-summary role="status" aria-live="polite"' in source
    assert "Step 1 of 4: Email" in source
    assert "First search checklist" in source
    assert "First shortlist" not in source
    assert "node.setAttribute('aria-current', 'step')" in source
    assert "const registerReturnTo = String(app.dataset.registerReturnTo || '').trim() || '/app/search';" in source
    assert "brand.key === 'propertyquarry'" not in source
    assert 'data-action="resend-code"' in source
    assert 'data-action="change-email"' in source
    assert "propertyquarry-register-session-v2" in source
    assert "window.sessionStorage.setItem(storageKey" in source
    assert "window.localStorage.setItem(storageKey" not in source
    assert "state.start = null;" in source
    assert "access_token" not in source
    assert "window.history.replaceState" in source
    assert "const googleConnectStorageKey" not in source
    assert "window.addEventListener('storage'" not in source
    assert "Google confirmed your PropertyQuarry identity" in source
    assert "No inbox or workspace data was connected" in source
    assert "@media (max-width: 760px)" in source


def test_register_page_preserves_only_safe_internal_return_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    safe = client.get("/register?return_to=%2Fpricing")
    unsafe = client.get("/register?return_to=https%3A%2F%2Fexample.invalid%2Fescape")

    assert safe.status_code == 200
    assert 'data-register-return-to="/pricing"' in safe.text
    assert '<a class="btn primary" href="/pricing">Continue</a>' in safe.text
    assert unsafe.status_code == 200
    assert 'data-register-return-to="/app/search"' in unsafe.text
    assert "example.invalid" not in unsafe.text


@pytest.mark.parametrize(
    "unsafe_target",
    (
        "https://example.invalid/escape",
        "//example.invalid/escape",
        "/\\example.invalid/escape",
        "/%5Cexample.invalid/escape",
        "/%255Cexample.invalid/escape",
        "/%2F%2Fexample.invalid/escape",
        "/%252F%252Fexample.invalid/escape",
        "/app/search%0d%0aLocation:%20https://example.invalid/escape",
    ),
)
def test_browser_return_target_rejects_cross_host_and_encoded_variants(
    unsafe_target: str,
) -> None:
    from app.api.routes.landing import _normalize_browser_return_to

    assert _normalize_browser_return_to(unsafe_target, default="/app/search") == "/app/search"


def test_browser_return_target_keeps_safe_internal_path_query_and_fragment() -> None:
    from app.api.routes.landing import _normalize_browser_return_to

    safe_target = "/app/support?source=pricing#new-request"
    assert _normalize_browser_return_to(safe_target, default="/app/search") == safe_target


def test_browser_return_target_rejects_oversized_internal_target() -> None:
    from app.api.routes.landing import _normalize_browser_return_to

    oversized_target = "/app/search?note=" + ("x" * 2048)
    assert _normalize_browser_return_to(oversized_target, default="/app/search") == "/app/search"

    oversized_multibyte_target = "/app/search?note=" + ("🏠" * 200)
    assert _normalize_browser_return_to(oversized_multibyte_target, default="/app/search") == "/app/search"

    oversized_spaced_target = "/app/support?note=" + (" a" * 520)
    assert _normalize_browser_return_to(oversized_spaced_target, default="/app/search") == "/app/search"


@pytest.mark.parametrize(
    "unsafe_target",
    (
        "\n/app/search",
        "\r/app/search",
        "\t/app/search",
        "/app/search\n",
        "/app/search\r",
        "/app/search\t",
        "/app/search\r\n",
        (" " * 700) + "/app/search" + (" " * 700),
    ),
)
def test_browser_return_target_rejects_surrounding_controls_and_overlong_whitespace(
    unsafe_target: str,
) -> None:
    from app.api.routes.landing import _normalize_browser_return_to

    fallback = "/safe-default"
    assert _normalize_browser_return_to(unsafe_target, default=fallback) == fallback


def _id_austria_claims(*, bpk: str = "ZP-MH:test-bpk", subject: str = "id-austria-subject") -> dict[str, object]:
    return {
        "iss": "https://idp.id-austria.gv.at",
        "aud": "https://propertyquarry.com",
        "iat": 1_787_300_000,
        "exp": 1_787_303_600,
        "sub": subject,
        "urn:pvpgvat:oidc.bpk": bpk,
        "given_name": "Tibor",
        "family_name": "Girschele",
    }


def test_register_start_returns_magic_link_and_local_code_without_email_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    client = _client(monkeypatch)

    response = client.post("/v1/register/start", json={"email": "Tibor.Girschele@Gmail.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "tibor.girschele@gmail.com"
    assert len(body["verification_code"]) == 6
    assert body["magic_link_url"].startswith("/register?token=")
    assert body["workspace_name"] == "Tibor Girschele"
    assert body["email_delivery_status"] == ""


def test_register_challenge_is_opaque_and_response_never_exposes_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    started = client.post(
        "/v1/register/start",
        json={"email": "opaque.registration@example.com"},
    )

    assert started.status_code == 200
    start_body = started.json()
    token = str(start_body["verification_token"])
    assert token.startswith("pqrv2_")
    assert "." not in token
    assert "opaque" not in token.lower()
    assert "example" not in token.lower()
    assert start_body["resend_cooldown_seconds"] == 60
    assert start_body["resend_available_at"] > 0

    verified = client.post(
        "/v1/register/verify",
        json={
            "verification_token": token,
            "verification_code": start_body["verification_code"],
            "workspace_name": "Opaque Registration",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )

    assert verified.status_code == 200
    assert "access_token" not in verified.json()
    assert "access_token" not in verified.text


def test_register_resend_enforces_cooldown_and_invalidates_previous_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    from app.services import propertyquarry_registration_identity as registration_identity

    clock = {"now": 1_800_000_000}
    monkeypatch.setattr(registration_identity.time, "time", lambda: clock["now"])
    client = _client(monkeypatch)

    first = client.post(
        "/v1/register/start",
        json={"email": "resend@example.com"},
    )
    assert first.status_code == 200

    too_soon = client.post(
        "/v1/register/start",
        json={"email": "resend@example.com"},
    )
    assert too_soon.status_code == 429
    assert too_soon.json()["error"]["code"] == "registration_verification_resend_too_soon"
    assert int(too_soon.headers["retry-after"]) == 60

    clock["now"] += 61
    fresh = client.post(
        "/v1/register/start",
        json={"email": "resend@example.com"},
    )
    assert fresh.status_code == 200
    assert fresh.json()["verification_token"] != first.json()["verification_token"]

    old = client.post(
        "/v1/register/verify",
        json={
            "verification_token": first.json()["verification_token"],
            "verification_code": first.json()["verification_code"],
            "workspace_name": "Old Code",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert old.status_code == 400
    assert old.json()["error"]["code"] == "registration_verification_invalid"

    current = client.post(
        "/v1/register/verify",
        json={
            "verification_token": fresh.json()["verification_token"],
            "verification_code": fresh.json()["verification_code"],
            "workspace_name": "Fresh Code",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert current.status_code == 200


def test_register_verification_locks_after_five_wrong_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    client = _client(monkeypatch)
    started = client.post(
        "/v1/register/start",
        json={"email": "attempt-lock@example.com"},
    )
    assert started.status_code == 200
    body = started.json()
    wrong_code = "000000" if body["verification_code"] != "000000" else "000001"

    for attempt in range(1, 6):
        wrong = client.post(
            "/v1/register/verify",
            json={
                "verification_token": body["verification_token"],
                "verification_code": wrong_code,
                "workspace_name": "Attempt Lock",
                "timezone": "Europe/Vienna",
                "language": "en",
            },
        )
        if attempt < 5:
            assert wrong.status_code == 400
            assert wrong.json()["error"]["code"] == "registration_verification_code_invalid"
        else:
            assert wrong.status_code == 429
            assert wrong.json()["error"]["code"] == "registration_verification_locked"

    correct_after_lock = client.post(
        "/v1/register/verify",
        json={
            "verification_token": body["verification_token"],
            "verification_code": body["verification_code"],
            "workspace_name": "Attempt Lock",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert correct_after_lock.status_code == 429
    assert correct_after_lock.json()["error"]["code"] == "registration_verification_locked"


def test_register_resend_is_limited_to_five_messages_per_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    from app.services import propertyquarry_registration_identity as registration_identity

    clock = {"now": 1_800_100_000}
    monkeypatch.setattr(registration_identity.time, "time", lambda: clock["now"])
    client = _client(monkeypatch)

    for _send_number in range(5):
        sent = client.post(
            "/v1/register/start",
            json={"email": "send-window@example.com"},
        )
        assert sent.status_code == 200
        clock["now"] += 61

    limited = client.post(
        "/v1/register/start",
        json={"email": "send-window@example.com"},
    )
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "registration_verification_rate_limited"
    assert int(limited.headers["retry-after"]) > 0


@pytest.mark.parametrize("path", ("/v1/register/start", "/v1/register/verify"))
def test_register_api_is_not_available_on_another_product_host(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    client = _client(monkeypatch)
    payload = (
        {"email": "cross-product@example.com"}
        if path.endswith("/start")
        else {"verification_token": "pqrv2_invalid", "verification_code": "000000"}
    )

    response = client.post(
        path,
        json=payload,
        headers={"host": "myexternalbrain.example"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "propertyquarry_registration_host_required"


def test_register_start_uses_absolute_magic_link_when_email_delivery_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    client = _client(monkeypatch)

    from app.api.routes import onboarding as onboarding_route
    from app.services.registration_email import RegistrationEmailReceipt

    observed: dict[str, object] = {}

    def _fake_send_registration_email(**kwargs) -> RegistrationEmailReceipt:
        observed.update(kwargs)
        return RegistrationEmailReceipt(
            provider="emailit",
            message_id="emailit-message-1",
            accepted_at="2026-03-26T00:00:00+00:00",
        )

    monkeypatch.setattr(onboarding_route, "send_registration_email", _fake_send_registration_email)

    response = client.post(
        "/v1/register/start",
        json={"email": "exec@example.com", "return_to": "/pricing"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["email_delivery_status"] == "sent"
    assert body["email_delivery_provider"] == "emailit"
    assert body["email_delivery_id"] == "emailit-message-1"
    assert observed["recipient_email"] == "exec@example.com"
    assert str(observed["magic_link_url"]).startswith("https://propertyquarry.com/register?token=")
    magic_link_query = urllib.parse.parse_qs(
        urllib.parse.urlparse(str(observed["magic_link_url"])).query
    )
    assert magic_link_query["return_to"] == ["/pricing"]


def test_register_start_reports_email_delivery_failure_without_aborting_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    client = _client(monkeypatch)

    from app.api.routes import onboarding as onboarding_route

    monkeypatch.setattr(
        onboarding_route,
        "send_registration_email",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("registration_email_send_failed:422:Domain not verified")),
    )

    response = client.post("/v1/register/start", json={"email": "broken@example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "broken@example.com"
    assert body["email_delivery_status"] == "failed"
    assert "Domain not verified" in body["email_delivery_error"]
    assert len(body["verification_code"]) == 6


def test_register_start_prod_returns_safe_token_without_returning_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv(
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
        "test-propertyquarry-production-registration-session-secret",
    )
    client = _client(monkeypatch)
    object.__setattr__(client.app.state.container.settings.runtime, "mode", "prod")

    from app.api.routes import onboarding as onboarding_route
    from app.services.registration_email import RegistrationEmailReceipt

    observed: dict[str, object] = {}

    def _fake_send_registration_email(**kwargs) -> RegistrationEmailReceipt:
        observed.update(kwargs)
        return RegistrationEmailReceipt(
            provider="emailit",
            message_id="emailit-production-1",
            accepted_at="2026-07-16T11:20:00+00:00",
        )

    monkeypatch.setattr(onboarding_route, "send_registration_email", _fake_send_registration_email)

    response = client.post("/v1/register/start", json={"email": "prod@example.com"})

    assert response.status_code == 200
    body = response.json()
    assert body["email_delivery_status"] == "sent"
    assert body["verification_token"]
    assert body["verification_code"] == ""
    assert body["magic_link_url"] == ""
    assert body["email_delivery_error"] == ""
    assert "token=" in str(observed["magic_link_url"])
    assert "code=" in str(observed["magic_link_url"])
    assert str(observed["verification_code"]) not in body["verification_token"]

    verified = client.post(
        "/v1/register/verify",
        json={
            "verification_token": body["verification_token"],
            "verification_code": observed["verification_code"],
            "workspace_name": "Production Example",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert verified.status_code == 200


def test_register_start_prod_fails_closed_without_email_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    monkeypatch.setenv(
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
        "test-propertyquarry-production-registration-session-secret",
    )
    client = _client(monkeypatch)
    object.__setattr__(client.app.state.container.settings.runtime, "mode", "prod")

    response = client.post("/v1/register/start", json={"email": "prod@example.com"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "registration_email_delivery_unavailable"
    assert "verification_token" not in response.text
    assert "verification_code" not in response.text
    assert "magic_link_url" not in response.text


def test_register_start_prod_redacts_email_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv(
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
        "test-propertyquarry-production-registration-session-secret",
    )
    client = _client(monkeypatch)
    object.__setattr__(client.app.state.container.settings.runtime, "mode", "prod")

    from app.api.routes import onboarding as onboarding_route

    provider_detail = "registration_email_send_failed:422:private-provider-detail"
    monkeypatch.setattr(
        onboarding_route,
        "send_registration_email",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError(provider_detail)),
    )

    response = client.post("/v1/register/start", json={"email": "prod@example.com"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "registration_email_delivery_unavailable"
    assert provider_detail not in response.text


def test_register_start_prod_fails_closed_without_dedicated_verification_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.delenv("PROPERTYQUARRY_IDENTITY_SESSION_SECRET", raising=False)
    client = _client(monkeypatch)
    object.__setattr__(client.app.state.container.settings.runtime, "mode", "prod")

    response = client.post("/v1/register/start", json={"email": "prod@example.com"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "registration_verification_unavailable"
    assert "verification_token" not in response.text
    assert "verification_code" not in response.text


def test_register_start_prod_fails_closed_with_weak_verification_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("PROPERTYQUARRY_IDENTITY_SESSION_SECRET", "too-short")
    client = _client(monkeypatch)
    object.__setattr__(client.app.state.container.settings.runtime, "mode", "prod")

    response = client.post("/v1/register/start", json={"email": "prod@example.com"})

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "registration_verification_unavailable"
    assert "too-short" not in response.text


def test_sign_in_email_link_reissues_workspace_access_for_existing_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    client = _client(monkeypatch)
    start_workspace(client, mode="personal", workspace_name="Founder Office")

    issued = client.post(
        "/app/api/access-sessions",
        json={"email": "founder@example.com", "role": "principal", "display_name": "Founder Office"},
    )
    assert issued.status_code == 200

    from app.product import service as product_service
    from app.services.registration_email import RegistrationEmailReceipt

    observed: dict[str, object] = {}

    def _fake_send_workspace_access_email(**kwargs) -> RegistrationEmailReceipt:
        observed.update(kwargs)
        return RegistrationEmailReceipt(
            provider="emailit",
            message_id="access-message-1",
            accepted_at="2026-03-26T00:00:00+00:00",
        )

    monkeypatch.setattr(product_service, "send_workspace_access_email", _fake_send_workspace_access_email)

    response = client.post(
        "/sign-in/email-link",
        data={"email": "Founder@Example.com", "return_to": "/app/support"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "link_status=submitted" in response.headers["location"]
    assert "link_count=" not in response.headers["location"]
    assert "return_to=%2Fapp%2Fsupport" in response.headers["location"]
    followup = client.get(response.headers["location"])
    assert followup.status_code == 200
    assert "Check your inbox." in followup.text
    assert "If founder@example.com already has access" in followup.text
    assert "founder@example.com" in followup.text
    assert observed["recipient_email"] == "founder@example.com"
    assert observed["workspace_name"] == "Founder Office"
    assert str(observed["access_url"]).startswith("https://propertyquarry.com/workspace-access/")
    access_path = urllib.parse.urlparse(str(observed["access_url"])).path
    opened = client.get(access_path, follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/app/support"


def test_sign_in_email_link_reports_missing_workspace_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    client = _client(monkeypatch)

    response = client.post(
        "/sign-in/email-link",
        data={"email": "unknown@example.com", "return_to": "https://example.invalid/escape"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "link_status=submitted" in response.headers["location"]
    redirect_query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
    assert redirect_query["return_to"] == ["/app/settings/access"]
    followup = client.get(response.headers["location"])
    assert followup.status_code == 200
    assert "Check your inbox." in followup.text
    assert "If unknown@example.com already has access" in followup.text


def test_sign_in_page_offers_google_return_path(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    response = client.get("/sign-in")

    assert response.status_code == 200
    assert "Google unavailable" not in response.text
    assert "Facebook unavailable" not in response.text
    assert "Continue with Facebook" not in response.text
    assert 'href="/sign-in/google"' not in response.text
    assert 'href="/sign-in/facebook"' not in response.text
    assert 'data-auth-provider="google" data-auth-provider-state="disabled"' not in response.text
    assert 'data-auth-provider="facebook" data-auth-provider-state="disabled"' not in response.text
    assert "opacity: 0.68" not in response.text
    assert 'data-auth-provider-status role="status" aria-live="polite"' not in response.text
    assert "If nothing opens, use email instead." in response.text
    assert "Still here. Try again or use email instead." in response.text
    assert "}, 3500);" in response.text
    assert "Google?" not in response.text
    assert "Facebook?" not in response.text
    assert "Continue with ID Austria" not in response.text
    assert "Identity only" not in response.text
    assert "Identity-only." not in response.text
    assert "grid-template-columns: 28px minmax(0, 1fr) max-content;" in response.text
    assert "background: transparent;" in response.text
    assert "Choose the narrowest sign-in path" not in response.text


def test_sign_in_page_uses_one_real_email_action_and_compact_provider_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    response = client.get("/sign-in?return_to=%2Fapp%2Fsupport")

    assert response.status_code == 200
    assert response.text.count('action="/sign-in/email-link"') == 1
    assert '<input type="hidden" name="return_to" value="/app/support">' in response.text
    assert "Send secure sign-in link" in response.text
    assert "Create an account with email." in response.text
    assert "Sign-in providers verify who you are, then open the same local account or create it if needed." in response.text
    assert "Creates account if needed" not in response.text
    assert 'role="list" aria-label="Sign-in providers"' in response.text
    assert 'aria-label="Continue with Google"' in response.text
    assert 'href="/sign-in/google?return_to=%2Fapp%2Fsupport"' in response.text
    assert 'href="/register?return_to=%2Fapp%2Fsupport"' in response.text
    assert '<button class="btn primary" type="button" data-focus-sign-in-email>Email sign-in</button>' not in response.text

    unsafe = client.get("/sign-in?return_to=https%3A%2F%2Fexample.invalid%2Fescape")
    assert '<input type="hidden" name="return_to" value="/app/search">' in unsafe.text
    assert "example.invalid" not in unsafe.text


def test_sign_in_page_shows_google_when_oauth_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    response = client.get("/sign-in")

    assert response.status_code == 200
    assert "Continue with Google" in response.text
    assert 'href="/sign-in/google?return_to=%2Fapp%2Fsearch"' in response.text
    assert "Google unavailable" not in response.text
    assert "same local account" in response.text.lower()
    assert "Sign-in providers verify who you are, then open the same local account or create it if needed." in response.text


@pytest.mark.parametrize(
    ("query", "provider"),
    (
        ("google_connected=1", "Google"),
        ("facebook_connected=1", "Facebook"),
        ("id_austria_connected=1", "ID Austria"),
    ),
)
def test_sign_in_page_shows_provider_return_status(monkeypatch: pytest.MonkeyPatch, query: str, provider: str) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    client = _client(monkeypatch)

    response = client.get(f"/sign-in?{query}")

    assert response.status_code == 200
    assert f"{provider} returned to PropertyQuarry." in response.text
    assert "Use email if you prefer." in response.text
    assert 'data-sign-in-provider-connected' in response.text
    assert 'href="/sign-in/current-session"' not in response.text
    assert "data-focus-sign-in-email" in response.text


def test_sign_in_page_shows_id_austria_when_oidc_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_id_austria(monkeypatch)
    client = _client(monkeypatch)

    response = client.get("/sign-in", headers={"CF-IPCountry": "AT"})

    assert response.status_code == 200
    assert "Continue with ID Austria" in response.text
    assert 'href="/sign-in/id-austria?return_to=%2Fapp%2Fsearch"' in response.text


def test_sign_in_page_hides_id_austria_outside_austria(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_id_austria(monkeypatch)
    client = _client(monkeypatch)

    response = client.get("/sign-in", headers={"CF-IPCountry": "DE"})
    direct = client.get("/sign-in/id-austria", headers={"CF-IPCountry": "DE"}, follow_redirects=False)

    assert response.status_code == 200
    assert "Continue with ID Austria" not in response.text
    assert direct.status_code == 303
    assert "id_austria_error=id_austria_austria_ip_required" in direct.headers["location"]
    followup = client.get(direct.headers["location"], headers={"CF-IPCountry": "DE"})
    assert "ID Austria sign-in is offered only when this request is detected from Austria." in followup.text
    assert "Retry ID Austria sign-in" not in followup.text


def test_sign_in_page_shows_disabled_id_austria_for_austrian_request_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_ID",
        "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_SECRET",
        "PROPERTYQUARRY_ID_AUSTRIA_STATE_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    client = _client(monkeypatch)

    response = client.get("/sign-in", headers={"CF-IPCountry": "AT"})

    assert response.status_code == 200
    assert "Continue with ID Austria" not in response.text
    assert "ID Austria unavailable" not in response.text
    assert 'data-auth-provider="id-austria" data-auth-provider-state="disabled"' not in response.text
    assert 'href="/sign-in/id-austria"' not in response.text


def test_sign_in_id_austria_get_starts_oidc_for_visible_link(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_id_austria(monkeypatch)
    client = _client(monkeypatch)

    response = client.get("/sign-in/id-austria", headers={"CF-IPCountry": "AT"}, follow_redirects=False)

    assert response.status_code == 303
    parsed = urllib.parse.urlparse(response.headers["location"])
    query = urllib.parse.parse_qs(parsed.query)
    assert response.headers["location"].startswith("https://idp.id-austria.gv.at/auth/idp/profile/oidc/authorize")
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["https://propertyquarry.com"]
    assert query["redirect_uri"] == ["https://propertyquarry.com/id-austria/callback"]
    assert query["scope"] == ["openid profile"]
    assert query.get("nonce")


def test_id_austria_unknown_identity_returns_to_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_id_austria(monkeypatch)
    client = _client(monkeypatch)

    sign_in_start = client.get(
        "/sign-in/id-austria?return_to=%2Fapp%2Fsupport",
        headers={"CF-IPCountry": "AT"},
        follow_redirects=False,
    )
    assert sign_in_start.status_code == 303
    state = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)["state"][0]

    from app.services import id_austria_oidc

    monkeypatch.setattr(
        id_austria_oidc,
        "_exchange_id_austria_code_for_tokens",
        lambda **kwargs: {"id_token": "header.payload.signature", "expires_in": 3600},
    )
    monkeypatch.setattr(
        id_austria_oidc,
        "_decode_id_austria_id_token",
        lambda **kwargs: _id_austria_claims(bpk="ZP-MH:unknown-bpk", subject="unknown-subject"),
    )

    callback = client.get(
        "/id-austria/callback",
        params={"code": "code-123", "state": state},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/workspace-access/")
    opened = client.get(callback.headers["location"], follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/app/support"
    assert "ea_workspace_session=" in str(opened.headers.get("set-cookie") or "")


def test_sign_in_id_austria_callback_rejects_replayed_state_before_second_token_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_id_austria(monkeypatch)
    client = _client(monkeypatch)

    sign_in_start = client.get("/sign-in/id-austria", headers={"CF-IPCountry": "AT"}, follow_redirects=False)
    assert sign_in_start.status_code == 303
    state = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)["state"][0]

    from app.services import id_austria_oidc

    id_austria_oidc._ID_AUSTRIA_USED_STATE_KEYS.clear()  # noqa: SLF001
    token_exchanges = {"count": 0}

    def _exchange(**kwargs):  # noqa: ANN003
        token_exchanges["count"] += 1
        return {"id_token": "header.payload.signature", "expires_in": 3600}

    monkeypatch.setattr(id_austria_oidc, "_exchange_id_austria_code_for_tokens", _exchange)
    monkeypatch.setattr(
        id_austria_oidc,
        "_decode_id_austria_id_token",
        lambda **kwargs: _id_austria_claims(bpk="ZP-MH:unknown-bpk", subject="unknown-subject"),
    )

    first_callback = client.get(
        "/id-austria/callback",
        params={"code": "code-123", "state": state},
        follow_redirects=False,
    )
    assert first_callback.status_code == 303
    assert first_callback.headers["location"].startswith("/workspace-access/")
    assert token_exchanges["count"] == 1

    second_callback = client.get(
        "/id-austria/callback",
        params={"code": "code-456", "state": state},
        follow_redirects=False,
    )
    assert second_callback.status_code == 303
    assert "id_austria_error=id_austria_state_replayed" in second_callback.headers["location"]
    assert token_exchanges["count"] == 1


def test_id_austria_connect_links_workspace_and_sign_in_reopens_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_id_austria(monkeypatch)
    principal_id = "user-id-austria-linked"
    client = _client(monkeypatch)
    client.headers.update({"X-EA-Principal-ID": principal_id})
    start_workspace(client, mode="personal", workspace_name="Verified Austrian Workspace")

    from app.services import id_austria_oidc

    monkeypatch.setattr(
        id_austria_oidc,
        "_exchange_id_austria_code_for_tokens",
        lambda **kwargs: {"id_token": "header.payload.signature", "expires_in": 3600},
    )
    monkeypatch.setattr(
        id_austria_oidc,
        "_decode_id_austria_id_token",
        lambda **kwargs: _id_austria_claims(),
    )

    connect_start = client.get(
        "/app/actions/id-austria/connect",
        params={"return_to": "/app/account"},
        headers={"CF-IPCountry": "AT"},
        follow_redirects=False,
    )
    assert connect_start.status_code == 303
    connect_state = urllib.parse.parse_qs(urllib.parse.urlparse(connect_start.headers["location"]).query)["state"][0]

    connected = client.get(
        "/id-austria/callback",
        params={"code": "code-connect", "state": connect_state},
        follow_redirects=False,
    )
    assert connected.status_code == 303
    assert connected.headers["location"] == "/app/account?id_austria_status=connected"

    container = client.app.state.container
    records = container.provider_registry.list_persisted_binding_records(principal_id=principal_id, limit=20)
    id_austria_records = [record for record in records if record.provider_key == "id_austria"]
    assert len(id_austria_records) == 1
    metadata = dict(id_austria_records[0].auth_metadata_json or {})
    assert "id_austria_bpk" not in metadata
    assert metadata["id_austria_bpk_hash"]
    connector_bindings = container.tool_runtime.list_connector_bindings_for_connector("id_austria", limit=20)
    assert len(connector_bindings) == 1
    assert connector_bindings[0].principal_id == principal_id
    assert connector_bindings[0].external_account_ref != "ZP-MH:test-bpk"

    sign_in_start = client.get("/sign-in/id-austria", headers={"CF-IPCountry": "AT"}, follow_redirects=False)
    assert sign_in_start.status_code == 303
    sign_in_state = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)["state"][0]

    signed_in = client.get(
        "/id-austria/callback",
        params={"code": "code-sign-in", "state": sign_in_state},
        follow_redirects=False,
    )
    assert signed_in.status_code == 303
    assert signed_in.headers["location"].startswith("/workspace-access/")
    opened = client.get(signed_in.headers["location"], follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/app/search"
    assert "ea_workspace_session=" in str(opened.headers.get("set-cookie") or "")


def test_sign_in_page_shows_facebook_when_oauth_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    client = _client(monkeypatch)

    response = client.get("/sign-in")

    assert response.status_code == 200
    assert "Continue with Facebook" in response.text
    assert 'href="/sign-in/facebook?return_to=%2Fapp%2Fsearch"' in response.text


def test_sign_in_page_only_shows_facebook_when_configured_and_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN", "1")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    client = _client(monkeypatch)

    response = client.get("/sign-in")

    assert response.status_code == 200
    assert "Continue with Facebook" in response.text
    assert 'href="/sign-in/facebook?return_to=%2Fapp%2Fsearch"' in response.text


def test_sign_in_facebook_requires_dedicated_state_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN", "1")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.delenv("EA_FACEBOOK_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "test-google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "test-provider-secret-key")
    monkeypatch.setenv("EA_SIGNING_SECRET", "test-signing-secret")
    client = _client(monkeypatch)

    response = client.get("/sign-in")
    direct = client.get("/sign-in/facebook", follow_redirects=False)

    assert response.status_code == 200
    assert "Facebook unavailable" not in response.text
    assert "Continue with Facebook" not in response.text
    assert 'href="/sign-in/facebook"' not in response.text
    assert direct.status_code == 303
    assert "facebook_error=facebook_sign_in_disabled" in direct.headers["location"]


def test_sign_in_google_get_starts_oauth_for_visible_link(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    response = client.get("/sign-in/google", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth")


def test_sign_in_facebook_post_fails_closed_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN", "0")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    client = _client(monkeypatch)

    response = client.post("/sign-in/facebook", follow_redirects=False)

    assert response.status_code == 303
    assert "facebook_error=facebook_sign_in_disabled" in response.headers["location"]


def test_sign_in_facebook_get_starts_oauth_for_visible_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    client = _client(monkeypatch)

    response = client.get("/sign-in/facebook", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("https://www.facebook.com/")


def test_sign_in_facebook_ignores_stale_email_scope_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN", "1")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_SCOPES", "public_profile,email")
    client = _client(monkeypatch)

    response = client.get("/sign-in/facebook", follow_redirects=False)

    assert response.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(response.headers["location"]).query)
    assert query["scope"] == ["public_profile"]


def test_sign_in_google_reopens_existing_workspace_after_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    existing_principal = "user-4a1702ea0e8d9ec5"
    client.headers.update({"X-EA-Principal-ID": existing_principal})
    start_workspace(client, mode="personal", workspace_name="Tibor Property Workspace")
    from app.api.routes.landing import build_product_service

    product = build_product_service(client.app.state.container)
    product.issue_workspace_access_session(
        principal_id=existing_principal,
        email="tibor.girschele@gmail.com",
        role="principal",
        display_name="Tibor Property Workspace",
        source_kind="registration",
        default_target="/app/search",
    )

    sign_in_start = client.post(
        "/sign-in/google?return_to=%2Fapp%2Fsupport",
        follow_redirects=False,
    )
    assert sign_in_start.status_code == 303
    auth_url = sign_in_start.headers["location"]
    assert auth_url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    parsed = urllib.parse.urlparse(auth_url)
    query = urllib.parse.parse_qs(parsed.query)
    assert query["redirect_uri"][0] == "https://propertyquarry.com/google/callback"

    from app.services import propertyquarry_google_identity as google_identity

    monkeypatch.setattr(
        google_identity,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_identity,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-signin",
            "email": "tibor.girschele@gmail.com",
            "email_verified": True,
        },
    )

    callback = client.get(
        "/google/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/app/support"
    assert "propertyquarry_identity_session=pqis1." in str(callback.headers.get("set-cookie") or "")
    assert any(
        header.startswith('ea_workspace_session=""') and "Max-Age=0" in header
        for header in callback.headers.get_list("set-cookie")
    )


def test_sign_in_google_ignores_google_connector_binding_and_uses_local_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    existing_principal = "user-a2a5c1d8b7e2f4"
    client.headers.update({"X-EA-Principal-ID": existing_principal})
    start_workspace(client, mode="personal", workspace_name="Connector Binding Workspace")

    from app.services import google_oauth as google_service

    client.app.state.container.tool_runtime.upsert_connector_binding(
        principal_id=existing_principal,
        connector_name=google_service.GOOGLE_CONNECTOR_NAME,
        external_account_ref="tibor.girschele@gmail.com",
        scope_json={"scopes": ()},
        auth_metadata_json={"google_email": "tibor.girschele@gmail.com"},
        status="enabled",
    )

    sign_in_start = client.post(
        "/sign-in/google",
        follow_redirects=False,
    )
    assert sign_in_start.status_code == 303
    auth_url = sign_in_start.headers["location"]
    assert auth_url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    parsed = urllib.parse.urlparse(auth_url)
    query = urllib.parse.parse_qs(parsed.query)

    from app.services import propertyquarry_google_identity as google_identity

    monkeypatch.setattr(
        google_identity,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_identity,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-signin",
            "email": "tibor.girschele@gmail.com",
            "email_verified": True,
        },
    )

    callback = client.get(
        "/google/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/app/search"
    assert "propertyquarry_identity_session=pqis1." in str(callback.headers.get("set-cookie") or "")
    local_principal = f"user-{hashlib.sha256(b'tibor.girschele@gmail.com').hexdigest()[:16]}"
    assert local_principal in google_identity._MEMORY_ACCOUNTS  # noqa: SLF001
    assert existing_principal not in google_identity._MEMORY_ACCOUNTS  # noqa: SLF001


def test_sign_in_google_prefers_real_workspace_over_temporary_cf_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://propertyquarry.com/google/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "test-google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "test-provider-secret-key")
    client = _client(monkeypatch)

    real_principal = "user-a2a5c1d8b7e2f4"
    client.headers.update({"X-EA-Principal-ID": real_principal})
    start_workspace(client, mode="personal", workspace_name="Connector Binding Workspace")

    from app.api.routes.landing import build_product_service

    product = build_product_service(client.app.state.container)
    product.issue_workspace_access_session(
        principal_id=real_principal,
        email="tibor.girschele@gmail.com",
        role="principal",
        display_name="Connector Binding Workspace",
        source_kind="registration",
        default_target="/app/search",
    )
    temporary_principal = "cf-email:tibor.girschele@gmail.com"
    product.issue_workspace_access_session(
        principal_id=temporary_principal,
        email="tibor.girschele@gmail.com",
        role="principal",
        display_name="Temporary Principal",
        source_kind="registration",
        default_target="/app/search",
    )

    access = product.issue_google_sign_in_workspace_session(
        google_email="tibor.girschele@gmail.com",
        fallback_principal_id=temporary_principal,
        display_name="Tibor",
    )
    assert access["principal_id"] == real_principal


def test_sign_in_google_reopens_existing_cf_email_workspace_without_access_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://propertyquarry.com/google/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "test-google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "test-provider-secret-key")
    client = _client(monkeypatch)

    email_principal = "cf-email:tibor.girschele@gmail.com"
    client.headers.update({"X-EA-Principal-ID": email_principal})
    start_workspace(client, mode="personal", workspace_name="Tibor Email Workspace")

    from app.api.routes.landing import build_product_service

    product = build_product_service(client.app.state.container)
    access = product.issue_google_sign_in_workspace_session(
        google_email="Tibor.Girschele@Gmail.com",
        fallback_principal_id=email_principal,
        display_name="Tibor Girschele",
    )

    assert access["principal_id"] == email_principal
    assert access["email"] == "tibor.girschele@gmail.com"
    assert access["access_url"].startswith("/workspace-access/")


def test_sign_in_google_reopens_registered_workspace_without_access_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    email = "returner@example.com"
    registered_principal = f"user-{hashlib.sha256(email.encode('utf-8')).hexdigest()[:16]}"
    client.headers.update({"X-EA-Principal-ID": registered_principal})
    start_workspace(client, mode="personal", workspace_name="Returner Property Workspace")

    sign_in_start = client.post("/sign-in/google", follow_redirects=False)
    assert sign_in_start.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)

    from app.services import propertyquarry_google_identity as google_identity

    monkeypatch.setattr(
        google_identity,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_identity,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-returner",
            "email": email,
            "email_verified": True,
        },
    )

    callback = client.get(
        "/google/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "/app/search"
    assert "propertyquarry_identity_session=pqis1." in str(callback.headers.get("set-cookie") or "")
    assert registered_principal in google_identity._MEMORY_ACCOUNTS  # noqa: SLF001


def test_sign_in_google_does_not_create_wrong_workspace_for_unknown_email(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    sign_in_start = client.post("/sign-in/google", follow_redirects=False)
    assert sign_in_start.status_code == 303
    parsed = urllib.parse.urlparse(sign_in_start.headers["location"])
    query = urllib.parse.parse_qs(parsed.query)

    from app.services import propertyquarry_google_identity as google_identity

    monkeypatch.setattr(
        google_identity,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_identity,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-unknown",
            "email": "unknown.google@example.com",
            "email_verified": True,
        },
    )

    callback = client.get(
        "/google/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "/app/search"
    assert "propertyquarry_identity_session=pqis1." in str(callback.headers.get("set-cookie") or "")
    unknown_principal = f"user-{hashlib.sha256(b'unknown.google@example.com').hexdigest()[:16]}"
    assert unknown_principal in google_identity._MEMORY_ACCOUNTS  # noqa: SLF001


def test_sign_in_google_callback_google_error_is_returned_to_sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    sign_in_start = client.post("/sign-in/google", follow_redirects=False)
    assert sign_in_start.status_code == 303
    parsed = urllib.parse.urlparse(sign_in_start.headers["location"])
    query = urllib.parse.parse_qs(parsed.query)

    callback = client.get(
        "/google/callback",
        params={
            "error": "access_denied",
            "error_description": "The user denied the request",
            "state": query["state"][0],
        },
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/sign-in?")
    assert "google_error=google_oauth_propertyquarry_access_denied" in callback.headers["location"]


def test_sign_in_page_shows_friendly_identity_only_google_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)
    response = client.get("/sign-in?google_error=Google+Identity-only.&return_to=%2Fapp%2Fsupport")

    assert response.status_code == 200
    assert "Retry Google." in response.text
    assert "You can also use a secure email link for the same account." in response.text
    assert "Google Identity-only." not in response.text
    assert "Retry Google sign-in" in response.text
    assert 'href="/sign-in/google?return_to=%2Fapp%2Fsupport"' in response.text
    assert "data-submitting-label=\"Opening Google...\"" in response.text
    assert 'action="/sign-in/email-link"' in response.text
    assert "Send secure sign-in link" in response.text


def test_sign_in_error_hides_retry_when_provider_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_google_sign_in(monkeypatch)
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    client = _client(monkeypatch)

    response = client.get("/sign-in?google_error=Google+Identity-only.")

    assert response.status_code == 200
    assert "Google is not available right now." in response.text
    assert "Contact support if you still cannot sign in." in response.text
    assert "Retry Google sign-in" not in response.text
    assert 'href="/sign-in/google?' not in response.text


def test_sign_in_google_only_error_does_not_invent_another_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    client = _client(monkeypatch)

    response = client.get("/sign-in?google_error=Google+Identity-only.")
    connected = client.get("/sign-in?google_connected=1")

    assert response.status_code == 200
    assert "Retry Google." in response.text
    assert "Contact support if you still cannot sign in." in response.text
    assert "Retry Google sign-in" in response.text
    assert "Retry Google. Choose another available sign-in option." not in response.text
    assert "No other sign-in option is available right now. Contact support if this account did not open." in connected.text


def test_sign_in_hides_stale_provider_error_after_account_is_signed_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)
    client.headers.update({"X-EA-Principal-ID": "signed-in-provider-error"})

    response = client.get("/sign-in?google_error=Google+Identity-only.&return_to=%2Fapp%2Fsupport")

    assert response.status_code == 200
    assert '<a class="btn primary" href="/app/support">Continue</a>' in response.text
    assert "Google could not open on this attempt." not in response.text
    assert "Retry Google sign-in" not in response.text


def test_sign_in_google_identity_callback_does_not_reflect_provider_error_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    sign_in_start = client.post("/sign-in/google", follow_redirects=False)
    assert sign_in_start.status_code == 303
    parsed = urllib.parse.urlparse(sign_in_start.headers["location"])
    query = urllib.parse.parse_qs(parsed.query)

    callback = client.get(
        "/google/callback",
        params={
            "error": "access_denied",
            "error_description": "Google Identity-only.",
            "state": query["state"][0],
        },
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/sign-in?")
    assert "google_error=google_oauth_propertyquarry_access_denied" in callback.headers["location"]
    assert "Google+Identity-only" not in callback.headers["location"]


def test_sign_in_google_callback_accepts_verified_userinfo_without_scope_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    sign_in_start = client.post("/sign-in/google", follow_redirects=False)
    assert sign_in_start.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)

    from app.services import propertyquarry_google_identity as google_identity

    monkeypatch.setattr(
        google_identity,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {
            "access_token": "access-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        google_identity,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-signin",
            "email": "tibor.girschele@gmail.com",
            "email_verified": True,
        },
    )

    callback = client.get(
        "/google/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "/app/search"
    assert "propertyquarry_identity_session=pqis1." in str(callback.headers.get("set-cookie") or "")


def test_sign_in_google_callback_rejects_reused_browser_flow_before_second_token_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    sign_in_start = client.post("/sign-in/google", follow_redirects=False)
    assert sign_in_start.status_code == 303
    state = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)["state"][0]

    from app.services import propertyquarry_google_identity as google_identity

    google_identity.reset_propertyquarry_google_identity_memory_for_tests()
    token_exchanges = {"count": 0}

    def _exchange(**kwargs):  # noqa: ANN003
        token_exchanges["count"] += 1
        return {
            "access_token": "access-token",
            "expires_in": 3600,
        }

    monkeypatch.setattr(google_identity, "_exchange_google_code_for_tokens", _exchange)
    monkeypatch.setattr(
        google_identity,
        "_fetch_google_userinfo",
        lambda access_token: {
            "sub": "google-sub-unknown",
            "email": "unknown.google@example.com",
            "email_verified": True,
        },
    )

    first_callback = client.get(
        "/google/callback",
        params={"code": "code-123", "state": state},
        follow_redirects=False,
    )
    assert first_callback.status_code == 303
    assert first_callback.headers["location"] == "/app/search"
    assert token_exchanges["count"] == 1

    second_callback = client.get(
        "/google/callback",
        params={"code": "code-456", "state": state},
        follow_redirects=False,
    )
    assert second_callback.status_code == 303
    assert "google_error=google_oauth_propertyquarry_flow_mismatch" in second_callback.headers["location"]
    assert token_exchanges["count"] == 1


def test_google_oauth_start_rejects_cross_host_redirect_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "test-google-client-id")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "test-google-client-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_REDIRECT_URI", "https://propertyquarry.com/v1/providers/google/oauth/callback")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "test-google-state-secret")
    monkeypatch.setenv("EA_PROVIDER_SECRET_KEY", "test-provider-secret-key")

    from app.services import google_oauth as google_service

    with pytest.raises(RuntimeError, match="google_oauth_redirect_uri_invalid"):
        google_service.build_google_oauth_start(
            principal_id="user-google-redirect",
            scope_bundle="identity",
            redirect_uri_override="https://evil.example/google/callback",
        )


def test_facebook_oauth_start_rejects_cross_host_redirect_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")

    from app.services import facebook_oauth as facebook_service

    with pytest.raises(RuntimeError, match="facebook_oauth_redirect_uri_invalid"):
        facebook_service.build_facebook_oauth_start(
            principal_id="user-facebook-redirect",
            redirect_uri_override="https://evil.example/facebook/callback",
        )


def test_facebook_oauth_start_ignores_unsupported_email_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_SCOPES", "public_profile,email")

    from app.services import facebook_oauth as facebook_service

    packet = facebook_service.build_facebook_oauth_start(principal_id="user-facebook-scopes")
    parsed = urllib.parse.urlparse(packet.auth_url)
    query = urllib.parse.parse_qs(parsed.query)

    assert packet.requested_scopes == ("public_profile",)
    assert query["scope"][0] == "public_profile"
    assert "email" not in query["scope"][0]


def _patch_facebook_profile_only(monkeypatch: pytest.MonkeyPatch, *, subject: str = "facebook-user-signin", name: str = "Tibor Girschele") -> None:
    from app.services import facebook_oauth as facebook_service

    monkeypatch.setattr(
        facebook_service,
        "_exchange_facebook_code_for_token",
        lambda **kwargs: {
            "access_token": "facebook-access-token",
            "scope": "public_profile",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        facebook_service,
        "_fetch_facebook_userinfo",
        lambda **kwargs: {
            "id": subject,
            "name": name,
        },
    )


def test_sign_in_facebook_requires_linked_workspace_after_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN", "1")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    client = _client(monkeypatch)

    existing_principal = "user-4a1702ea0e8d9ec5"
    client.headers.update({"X-EA-Principal-ID": existing_principal})
    start_workspace(client, mode="personal", workspace_name="Tibor Property Workspace")

    sign_in_start = client.post(
        "/sign-in/facebook",
        follow_redirects=False,
    )
    assert sign_in_start.status_code == 303
    auth_url = sign_in_start.headers["location"]
    assert auth_url.startswith("https://www.facebook.com/v21.0/dialog/oauth")
    parsed = urllib.parse.urlparse(auth_url)
    query = urllib.parse.parse_qs(parsed.query)
    assert query["redirect_uri"][0] == "https://propertyquarry.com/facebook/callback"
    assert query["scope"][0] == "public_profile"
    assert "email" not in query["scope"][0]
    assert query["auth_type"][0] == "rerequest"

    _patch_facebook_profile_only(monkeypatch)

    callback = client.get(
        "/facebook/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/workspace-access/")
    opened = client.get(callback.headers["location"], follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/app/search"
    assert "ea_workspace_session=" in str(opened.headers.get("set-cookie") or "")


def test_facebook_connect_links_workspace_and_sign_in_reopens_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN", "1")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    client = _client(monkeypatch)

    existing_principal = "user-4a1702ea0e8d9ec5"
    client.headers.update({"X-EA-Principal-ID": existing_principal})
    start_workspace(client, mode="personal", workspace_name="Tibor Property Workspace")

    _patch_facebook_profile_only(monkeypatch)

    connect_start = client.get(
        "/app/actions/facebook/connect",
        params={"return_to": "/app/account"},
        follow_redirects=False,
    )
    assert connect_start.status_code == 303
    connect_query = urllib.parse.parse_qs(urllib.parse.urlparse(connect_start.headers["location"]).query)

    connected = client.get(
        "/facebook/callback",
        params={"code": "code-connect", "state": connect_query["state"][0]},
        follow_redirects=False,
    )
    assert connected.status_code == 303
    assert connected.headers["location"] == "/app/account?facebook_status=connected"

    sign_in_start = client.post(
        "/sign-in/facebook",
        follow_redirects=False,
    )
    assert sign_in_start.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)

    callback = client.get(
        "/facebook/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/workspace-access/")

    opened = client.get(callback.headers["location"], follow_redirects=False)
    assert opened.status_code == 303
    assert opened.headers["location"] == "/app/search"
    assert "ea_workspace_session=" in str(opened.headers.get("set-cookie") or "")


def test_sign_in_facebook_callback_fails_closed_without_returned_scopes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN", "1")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    client = _client(monkeypatch)

    sign_in_start = client.post("/sign-in/facebook", follow_redirects=False)
    assert sign_in_start.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)

    from app.services import facebook_oauth as facebook_service

    monkeypatch.setattr(
        facebook_service,
        "_exchange_facebook_code_for_token",
        lambda **kwargs: {
            "access_token": "facebook-access-token",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        facebook_service,
        "_fetch_facebook_userinfo",
        lambda **kwargs: {
            "id": "facebook-user-signin",
            "email": "tibor.girschele@gmail.com",
            "name": "Tibor Girschele",
        },
    )

    callback = client.get(
        "/facebook/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/sign-in?")
    assert "facebook_error=facebook_oauth_granted_scopes_missing" in callback.headers["location"]


def test_sign_in_facebook_callback_fails_closed_on_unexpected_email_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN", "1")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    client = _client(monkeypatch)

    sign_in_start = client.post("/sign-in/facebook", follow_redirects=False)
    assert sign_in_start.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)

    from app.services import facebook_oauth as facebook_service

    monkeypatch.setattr(
        facebook_service,
        "_exchange_facebook_code_for_token",
        lambda **kwargs: {
            "access_token": "facebook-access-token",
            "scope": "public_profile,email",
            "expires_in": 3600,
        },
    )
    monkeypatch.setattr(
        facebook_service,
        "_fetch_facebook_userinfo",
        lambda **kwargs: {
            "id": "facebook-user-signin",
            "name": "Tibor Girschele",
        },
    )

    callback = client.get(
        "/facebook/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"].startswith("/sign-in?")
    assert "facebook_error=facebook_oauth_unexpected_granted_scopes" in callback.headers["location"]


def test_sign_in_facebook_callback_rejects_replayed_state_before_second_token_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_FACEBOOK_SIGN_IN", "1")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_ID", "test-facebook-app-id")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_APP_SECRET", "test-facebook-app-secret")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_REDIRECT_URI", "https://propertyquarry.com/facebook/callback")
    monkeypatch.setenv("EA_FACEBOOK_OAUTH_STATE_SECRET", "test-facebook-state-secret")
    client = _client(monkeypatch)

    sign_in_start = client.post("/sign-in/facebook", follow_redirects=False)
    assert sign_in_start.status_code == 303
    query = urllib.parse.parse_qs(urllib.parse.urlparse(sign_in_start.headers["location"]).query)

    from app.services import facebook_oauth as facebook_service

    facebook_service._FACEBOOK_USED_STATE_KEYS.clear()  # noqa: SLF001
    token_exchanges = {"count": 0}

    def _exchange(**kwargs):  # noqa: ANN003
        token_exchanges["count"] += 1
        return {
            "access_token": "facebook-access-token",
            "scope": "public_profile",
            "expires_in": 3600,
        }

    monkeypatch.setattr(facebook_service, "_exchange_facebook_code_for_token", _exchange)
    monkeypatch.setattr(
        facebook_service,
        "_fetch_facebook_userinfo",
        lambda **kwargs: {
            "id": "facebook-user-signin",
            "name": "Tibor Girschele",
        },
    )

    first_callback = client.get(
        "/facebook/callback",
        params={"code": "code-123", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert first_callback.status_code == 303
    assert first_callback.headers["location"].startswith("/workspace-access/")
    assert token_exchanges["count"] == 1

    second_callback = client.get(
        "/facebook/callback",
        params={"code": "code-456", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert second_callback.status_code == 303
    assert "facebook_error=facebook_oauth_state_replayed" in second_callback.headers["location"]
    assert token_exchanges["count"] == 1


def test_sign_in_page_does_not_require_email_field_for_google(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    response = client.get("/sign-in")

    assert response.status_code == 200
    assert 'href="/sign-in/google?return_to=%2Fapp%2Fsearch"' in response.text
    assert "Continue with Google" in response.text
    assert "Facebook unavailable" not in response.text
    assert "Continue with Facebook" not in response.text
    assert 'href="/sign-in/facebook"' not in response.text
    assert 'id="google_sign_in_email"' not in response.text
    assert 'placeholder="you@company.com"' not in response.text


def test_expired_sign_in_page_never_renders_an_inert_recovery_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    _clear_google_sign_in(monkeypatch)
    for key in (
        "EA_FACEBOOK_OAUTH_CLIENT_ID",
        "EA_FACEBOOK_OAUTH_CLIENT_SECRET",
        "EA_FACEBOOK_OAUTH_REDIRECT_URI",
        "EA_FACEBOOK_OAUTH_STATE_SECRET",
        "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_ID",
        "PROPERTYQUARRY_ID_AUSTRIA_CLIENT_SECRET",
        "PROPERTYQUARRY_ID_AUSTRIA_REDIRECT_URI",
        "PROPERTYQUARRY_ID_AUSTRIA_STATE_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
    client = _client(monkeypatch)

    unavailable = client.get(
        "/sign-in?return_to=%2Fapp%2Fproperties%3Frun_id%3Drun-1&session=expired"
    )

    assert unavailable.status_code == 200
    assert "Sign-in is temporarily unavailable; try again later." in unavailable.text
    assert "data-sign-in-unavailable" in unavailable.text
    assert "data-pq-next-action" not in unavailable.text
    assert 'href="#sign-in-options"' not in unavailable.text

    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    configured = client.get(
        "/sign-in?return_to=%2Fapp%2Fproperties%3Frun_id%3Drun-1&session=expired"
    )

    assert configured.status_code == 200
    assert "Sign-in is temporarily unavailable" not in configured.text
    assert "data-pq-next-action data-focus-sign-in-options" in configured.text
    assert "Show sign-in options" in configured.text
    assert 'href="#sign-in-options"' not in configured.text


def test_register_verify_requires_matching_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    started = client.post("/v1/register/start", json={"email": "verify@example.com"})
    assert started.status_code == 200
    body = started.json()

    missing_code = client.post(
        "/v1/register/verify",
        json={
            "verification_token": body["verification_token"],
            "verification_code": "",
            "workspace_name": "Verify Example",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert missing_code.status_code == 400
    assert missing_code.json()["error"]["code"] == "registration_verification_code_invalid"

    verified = client.post(
        "/v1/register/verify",
        json={
            "verification_token": body["verification_token"],
            "verification_code": body["verification_code"],
            "workspace_name": "Verify Example",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert verified.status_code == 200
    verified_body = verified.json()
    assert verified_body["access_url"].startswith("/workspace-access/")
    google_start = dict(verified_body["google_start"])
    assert google_start["ready"] is True
    assert google_start["identity_only"] is True
    assert google_start["auth_url"].startswith("/sign-in/google?")
    assert google_start["start_url"] == google_start["auth_url"]
    google_query = urllib.parse.parse_qs(
        urllib.parse.urlparse(google_start["start_url"]).query
    )
    assert len(google_query["identity_binding"][0]) == 64

    replayed = client.post(
        "/v1/register/verify",
        json={
            "verification_token": body["verification_token"],
            "verification_code": body["verification_code"],
            "workspace_name": "Verify Example",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert replayed.status_code == 409
    assert replayed.json()["error"]["code"] == "registration_verification_already_used"


def test_register_verify_routes_google_through_propertyquarry_identity_only_sign_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    started = client.post("/v1/register/start", json={"email": "browser-callback@example.com"})
    assert started.status_code == 200
    body = started.json()

    verified = client.post(
        "/v1/register/verify",
        json={
            "verification_token": body["verification_token"],
            "verification_code": body["verification_code"],
            "workspace_name": "Browser Callback",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert verified.status_code == 200
    google_start = dict(verified.json()["google_start"])
    parsed = urllib.parse.urlparse(str(google_start["auth_url"]))
    query = urllib.parse.parse_qs(parsed.query)
    assert parsed.path == "/sign-in/google"
    assert query["return_to"] == ["/register?ready=1&google_identity=confirmed"]
    assert google_start["identity_only"] is True
    assert google_start["ready"] is True


def test_register_verify_never_invokes_generic_google_connector_or_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    _configure_google_sign_in(monkeypatch)
    client = _client(monkeypatch)
    monkeypatch.setattr(
        client.app.state.container.onboarding,
        "start_google",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic Google connector must not run")
        ),
    )

    started = client.post("/v1/register/start", json={"email": "callback-signal@example.com"})
    assert started.status_code == 200
    body = started.json()

    verified = client.post(
        "/v1/register/verify",
        json={
            "verification_token": body["verification_token"],
            "verification_code": body["verification_code"],
            "workspace_name": "Callback Signal",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert verified.status_code == 200
    google_start = dict(verified.json()["google_start"])
    assert google_start["identity_only"] is True
    assert google_start["start_url"].startswith("/sign-in/google?")


def test_register_verify_reports_google_oauth_configuration_hint_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EMAILIT_API_KEY", raising=False)
    _clear_google_sign_in(monkeypatch)
    client = _client(monkeypatch)

    started = client.post("/v1/register/start", json={"email": "nodev@example.com"})
    assert started.status_code == 200
    body = started.json()

    verified = client.post(
        "/v1/register/verify",
        json={
            "verification_token": body["verification_token"],
            "verification_code": body["verification_code"],
            "workspace_name": "No Dev",
            "timezone": "Europe/Vienna",
            "language": "en",
        },
    )
    assert verified.status_code == 200
    google_start = dict(verified.json()["google_start"])
    assert google_start["ready"] is False
    assert google_start["error"].startswith("google_oauth_propertyquarry_")
    assert google_start["start_url"] == ""
    assert google_start["auth_url"] == ""
    assert google_start["detail"] == "Google identity is not configured for PropertyQuarry on this host."


def test_registration_email_payload_stays_english_and_uses_propertyquarry_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "property@propertyquarry.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "PropertyQuarry")

    from app.services import registration_email as service

    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-live-1"}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_registration_email(
        recipient_email="tibor.girschele@gmail.com",
        verification_code="654321",
        magic_link_url="https://propertyquarry.com/register?token=test&code=654321",
        expires_at=2_000_000_000,
    )

    payload = dict(captured["payload"])
    assert payload["from"] == "PropertyQuarry <property@propertyquarry.com>"
    assert payload["subject"] == "Verify your email for PropertyQuarry"
    assert "Use this verification code to create your PropertyQuarry account" in payload["text"]
    assert "Google can optionally confirm your PropertyQuarry identity after sign-up." in payload["text"]
    assert "It does not connect Gmail, Calendar, MyExternalBrain, or EA." in payload["text"]
    assert "workspace data source" not in payload["text"]
    assert "titled secure-access button" in payload["text"]
    assert "http://" not in payload["text"]
    assert "https://" not in payload["text"]
    assert "Verify email and continue" in payload["html"]
    assert "654321" in payload["html"]
    assert (
        'href="https://propertyquarry.com/register?token=test&amp;code=654321"'
        in payload["html"]
    )
    assert "It does not connect Gmail, Calendar, MyExternalBrain, or EA." in payload[
        "html"
    ]
    assert payload["meta"]["kind"] == "propertyquarry_registration_verification_v2"
    assert payload["meta"]["verification_ref"] != "654321"
    assert "verification_code" not in payload["meta"]
    assert "654321" not in json.dumps(payload["meta"], sort_keys=True)
    assert receipt.message_id == "emailit-live-1"


def test_registration_email_falls_back_to_verified_sender_when_domain_is_not_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "property@propertyquarry.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "PropertyQuarry")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM_FALLBACK", "concierge@chummer.run")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME_FALLBACK", "PropertyQuarry")

    from app.services import registration_email as service

    observed_payloads: list[dict[str, object]] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-live-fallback-1"}).encode("utf-8")

    class _DomainNotVerified(service.urllib.error.HTTPError):
        def __init__(self):
            super().__init__(
                url=service.EMAILIT_API_BASE,
                code=422,
                msg="Unprocessable Entity",
                hdrs=None,
                fp=None,
            )

        def read(self) -> bytes:
            return b'{"error":"Domain not verified"}'

    call_count = {"value": 0}

    def _fake_urlopen(request, timeout=0):
        observed_payloads.append(json.loads(request.data.decode("utf-8")))
        call_count["value"] += 1
        if call_count["value"] == 1:
            raise _DomainNotVerified()
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_registration_email(
        recipient_email="tibor.girschele@gmail.com",
        verification_code="654321",
        magic_link_url="https://propertyquarry.com/register?token=test&code=654321",
        expires_at=2_000_000_000,
    )

    assert call_count["value"] == 2
    assert observed_payloads[0]["from"] == "PropertyQuarry <property@propertyquarry.com>"
    assert observed_payloads[1]["from"] == "PropertyQuarry <concierge@chummer.run>"
    assert observed_payloads[1]["meta"]["sender_fallback_used"] == "true"
    assert observed_payloads[1]["meta"]["preferred_sender_email"] == "property@propertyquarry.com"
    assert observed_payloads[1]["meta"]["fallback_sender_email"] == "concierge@chummer.run"
    assert receipt.message_id == "emailit-live-fallback-1"


def test_registration_email_can_force_verified_sender_without_primary_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FORCE_FALLBACK", "1")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "property@propertyquarry.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "PropertyQuarry")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM_FALLBACK", "concierge@chummer.run")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME_FALLBACK", "PropertyQuarry")

    from app.services import registration_email as service

    observed_payloads: list[dict[str, object]] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-live-forced-fallback-1"}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        observed_payloads.append(json.loads(request.data.decode("utf-8")))
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_registration_email(
        recipient_email="tibor.girschele@gmail.com",
        verification_code="654321",
        magic_link_url="https://propertyquarry.com/register?token=test&code=654321",
        expires_at=2_000_000_000,
    )

    assert len(observed_payloads) == 1
    assert observed_payloads[0]["from"] == "PropertyQuarry <concierge@chummer.run>"
    assert receipt.message_id == "emailit-live-forced-fallback-1"


def test_property_search_results_email_serializes_emailit_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "property@propertyquarry.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "PropertyQuarry")

    from app.services import registration_email as service

    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-property-results-1"}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_property_search_results_ready_email(
        recipient_email="tibor.girschele@gmail.com",
        results_url="https://propertyquarry.com/app/properties?run_id=run-1",
        result_total=2,
        hosted_tour_total=1,
        top_properties=[
            {
                "title": "BG Leopoldstadt, 082 25 E 89/25g",
                "source_label": "Justiz Edikte Auctions | Austria | Buy | 1020 Vienna",
                "fit_summary": "Sparse but relevant auction with floorplan and enough area.",
                "price_label": "EUR 310,000",
                "area_label": "82 m2",
                "rooms_label": "3 rooms",
                "location_label": "1020 Vienna",
                "review_url": "https://propertyquarry.com/app/research/run-1/prop-1",
                "tour_status": "queued",
            },
            {
                "title": "Genossenschaft 70 m2",
                "property_url": "https://propertyquarry.com/source/property-2",
            },
        ],
    )

    payload = dict(captured["payload"])
    assert payload["from"] == "PropertyQuarry <property@propertyquarry.com>"
    assert payload["subject"] == "PropertyQuarry results ready"
    assert "Research summary:" in payload["text"]
    assert "Best current match: BG Leopoldstadt, 082 25 E 89/25g" in payload["text"]
    assert "Key facts: EUR 310,000 | 82 m2 | 3 rooms | 1020 Vienna" in payload["text"]
    assert "Best matches:" in payload["text"]
    assert "http://" not in payload["text"]
    assert "https://" not in payload["text"]
    assert "titled" in payload["text"]
    assert "BG Leopoldstadt" in payload["text"]
    html = str(payload["html"])
    assert "PropertyQuarry research brief" in html
    assert "Quick take" in html
    assert "<table" in html
    assert 'href="https://propertyquarry.com/app/research/prop-1?run_id=run-1"' in html
    assert "BG Leopoldstadt, 082 25 E 89/25g" in html
    assert "EUR 310,000" in html
    assert "82 m2" in html
    assert 'href="https://propertyquarry.com/app/properties?run_id=run-1"' in html
    assert ">Open full " in html
    assert ">https://propertyquarry.com/app/research/prop-1?run_id=run-1</a>" not in html
    assert ">https://propertyquarry.com/app/properties?run_id=run-1</a>" not in html
    assert isinstance(payload["meta"]["top_property_refs"], str)
    assert isinstance(json.loads(payload["meta"]["top_property_refs"]), list)
    assert payload["meta"]["results_ref"]
    assert receipt.message_id == "emailit-property-results-1"


def test_property_tour_email_uses_propertyquarry_branding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "property@propertyquarry.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "PropertyQuarry")

    from app.services import registration_email as service

    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-property-tour-1"}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_property_tour_email(
        recipient_email="tibor.girschele@gmail.com",
        property_title="Family flat near Augarten",
        property_url="https://propertyquarry.com/source/property-1",
        tour_url="https://propertyquarry.com/tours/family-flat-near-augarten",
        variant_key="layout_first",
        listing_id="listing-123",
        area_label="84 m2",
        rooms_label="3 rooms",
        price_label="EUR 420,000",
        decision_summary_json={"recommendation": "shortlist", "good_fit_reasons": ["Floorplan and family fit."]},
    )

    payload = dict(captured["payload"])
    assert payload["from"] == "PropertyQuarry <property@propertyquarry.com>"
    assert payload["subject"] == "Apartment tour ready: Family flat near Augarten · layout first"
    assert "PropertyQuarry prepared a 360 review for Family flat near Augarten." in payload["text"]
    assert "Open the titled review button" in payload["text"]
    assert "Open the 360 review first" in payload["text"]
    assert "http://" not in payload["text"]
    assert "https://" not in payload["text"]
    assert "EA prepared" not in payload["text"]
    assert receipt.message_id == "emailit-property-tour-1"


def test_property_match_email_uses_propertyquarry_branding(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "property@propertyquarry.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "PropertyQuarry")

    from app.services import registration_email as service

    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-property-match-1"}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_property_match_email(
        recipient_email="tibor.girschele@gmail.com",
        property_title="Altbau near U6",
        property_url="https://www.immobilienscout24.de/expose/altbau-u6",
        review_url="https://propertyquarry.com/app/research/run-42/altbau-u6",
        tour_url="https://propertyquarry.com/tours/altbau-u6",
        provider_label="ImmoScout24 Germany",
        fit_summary="Personal fit 92/100 · shortlist · Lift and transit fit.",
        decision_summary_json={
            "good_fit_reasons": ["Lift and transit fit."],
            "bad_fit_reasons": ["Street noise still unknown."],
            "unknowns": ["Heating source still needs confirmation."],
        },
    )

    payload = dict(captured["payload"])
    assert payload["from"] == "PropertyQuarry <property@propertyquarry.com>"
    assert payload["subject"] == "Property match: Altbau near U6"
    assert "PropertyQuarry shortlisted a property match: Altbau near U6" in payload["text"]
    assert "EA shortlisted" not in payload["text"]
    assert "PropertyQuarry" in str(payload["html"])
    assert "EA shortlisted" not in str(payload["html"])
    assert 'href="https://propertyquarry.com/app/research/altbau-u6?run_id=run-42"' in str(payload["html"])
    assert receipt.message_id == "emailit-property-match-1"


def test_property_notification_previews_emit_canonical_research_links() -> None:
    from app.services.registration_email import property_notification_preview

    search_results_html = str(property_notification_preview("search_results_ready").get("html") or "")
    property_match_html = str(property_notification_preview("property_match").get("html") or "")
    tour_ready_html = str(property_notification_preview("tour_ready").get("html") or "")
    investment_ready_html = str(property_notification_preview("investment_research_ready").get("html") or "")

    assert "/app/research/altbau-near-u6?run_id=run-42" in search_results_html
    assert "/app/research/family-flat-near-augarten?run_id=run-42" in search_results_html
    assert "/app/research/altbau-u6?run_id=run-42" in property_match_html
    assert "/app/research/family-flat-near-augarten?run_id=run-42" in tour_ready_html
    assert "/app/research/altbau-u6?run_id=run-42&amp;investment=1" in investment_ready_html
    assert "/app/research/run-42/" not in search_results_html
    assert "/app/research/run-42/" not in property_match_html
    assert "/app/research/run-42/" not in tour_ready_html
    assert "/app/research/run-42/" not in investment_ready_html


def test_channel_digest_email_payload_uses_compact_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "kleinhirn@girschele.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "Kleinhirn")

    from app.services import registration_email as service

    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-digest-1"}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_channel_digest_email(
        recipient_email="tibor@myexternalbrain.com",
        digest_key="memo",
        headline="Morning memo digest",
        preview_text="0 memo items, 0 commitments at risk, 0 open decisions.",
        delivery_url="https://myexternalbrain.com/channel-loop/deliveries/token-123",
        plain_text=(
            "Open digest: https://myexternalbrain.com/channel-loop/deliveries/token-very-long\n"
            "Morning memo digest\n"
            "0 memo items, 0 commitments at risk, 0 open decisions.\n"
            "\n"
            "1. [Memo] Fix memo delivery blocker\n"
            "   Domain not verified. Verify the sending domain in the email provider before the next memo cycle.\n"
            "   Open support: https://myexternalbrain.com/app/settings/support\n"
        ),
        expires_at="2026-04-01T17:27:54+00:00",
    )

    payload = dict(captured["payload"])
    assert payload["from"] == "Kleinhirn <kleinhirn@girschele.com>"
    assert payload["subject"] == "Morning memo digest"
    assert "Open this secure workspace view with the titled button in this email." in payload["text"]
    assert "http://" not in payload["text"]
    assert "https://" not in payload["text"]
    assert "Digest preview" in payload["text"]
    assert "Open digest:" not in payload["text"]
    assert "Fix memo delivery blocker" in payload["text"]
    assert payload["meta"]["digest_key"] == "memo"
    assert payload["meta"]["delivery_ref"]
    assert "delivery_url" not in payload["meta"]
    assert receipt.message_id == "emailit-digest-1"


def test_google_connect_email_uses_workspace_delivery_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "kleinhirn@girschele.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "Kleinhirn")
    monkeypatch.setenv("EA_EMAIL_DEFAULT_FROM", "sprachenzentrum@girschele.com")
    monkeypatch.setenv("EA_EMAIL_DEFAULT_NAME", "Sprachenzentrum")

    from app.services import registration_email as service

    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-google-connect-1"}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_google_connect_email(
        recipient_email="tibor.girschele@gmail.com",
        workspace_name="PropertyQuarry account",
        connect_url="https://propertyquarry.com/workspace-access/token?return_to=%2Fapp%2Factions%2Fgoogle%2Fconnect",
        scope_label="Google Full Workspace",
        scope_summary="Broader assistant context: inbox actions plus richer calendar and Drive index context.",
        primary_google_email="",
        connected_account_total=0,
        expires_at="2026-05-05T16:57:54+00:00",
    )

    payload = dict(captured["payload"])
    assert payload["from"] == "Sprachenzentrum <sprachenzentrum@girschele.com>"
    assert payload["subject"] == "Connect Google to PropertyQuarry account"
    assert "No Google inbox is connected to this account yet" in payload["text"]
    assert "titled Google-connect button" in payload["text"]
    assert "http://" not in payload["text"]
    assert "https://" not in payload["text"]
    assert "Google connection" in str(payload["html"])
    assert "Connect Google" in str(payload["html"])
    assert receipt.message_id == "emailit-google-connect-1"


def test_plaintext_digest_email_payload_uses_full_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "kleinhirn@girschele.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "Kleinhirn")

    from app.services import registration_email as service

    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-plaintext-1"}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_plaintext_digest_email(
        recipient_email="tibor.girschele@gmail.com",
        digest_key="codexea-ia-2026-05-01",
        headline="CodexEA internal affairs summary",
        preview_text="4 cycles, 2 fixes, 0 unresolved blockers.",
        plain_text="Important things fixed today.\n- lane selection no longer loops the same failure.\n",
    )

    payload = dict(captured["payload"])
    assert payload["from"] == "Kleinhirn <kleinhirn@girschele.com>"
    assert payload["subject"] == "CodexEA internal affairs summary"
    assert "4 cycles, 2 fixes, 0 unresolved blockers." in payload["text"]
    assert "Important things fixed today." in payload["text"]
    assert payload["meta"]["digest_key"] == "codexea-ia-2026-05-01"
    assert receipt.message_id == "emailit-plaintext-1"


def test_plaintext_digest_email_supports_custom_sender(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMAILIT_API_KEY", "test-emailit-key")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_FROM", "kleinhirn@girschele.com")
    monkeypatch.setenv("EA_REGISTRATION_EMAIL_NAME", "Kleinhirn")

    from app.services import registration_email as service

    captured: dict[str, object] = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

        def read(self) -> bytes:
            return json.dumps({"id": "emailit-plaintext-custom-1"}).encode("utf-8")

    def _fake_urlopen(request, timeout=0):
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    monkeypatch.setattr(service.urllib.request, "urlopen", _fake_urlopen)

    receipt = service.send_plaintext_digest_email(
        recipient_email="tibor.girschele@gmail.com",
        digest_key="codexea-ia-custom-sender",
        headline="Internal affairs summary",
        preview_text="Sender override smoke test.",
        plain_text="Plain body",
        sender_email="ia@chummer.run",
        sender_name="Internal Affairs",
    )

    payload = dict(captured["payload"])
    assert payload["from"] == "Internal Affairs <ia@chummer.run>"
    assert payload["reply_to"] == "ia@chummer.run"
    assert receipt.message_id == "emailit-plaintext-custom-1"
