from __future__ import annotations

import base64
import hashlib
import inspect
import json
import os
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _configure_propertyquarry_google(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID", "propertyquarry-client-id")
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET", "propertyquarry-client-secret")
    monkeypatch.setenv(
        "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI",
        "https://propertyquarry.com/google/callback",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
        "propertyquarry-state-secret-with-enough-entropy",
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
        "propertyquarry-session-secret-with-enough-entropy",
    )


def _clear_propertyquarry_google(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID",
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET",
        "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI",
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET",
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET",
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_MAX_AGE_SECONDS",
        "PROPERTYQUARRY_IDENTITY_SESSION_TTL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("EA_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("EA_API_TOKEN", "")
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_DATABASE_URL", "")
    from app.api.app import create_app

    return TestClient(create_app(), base_url="https://propertyquarry.com")


def _mobile_pkce_pair() -> tuple[str, str]:
    verifier = "PropertyQuarryAndroidVerifier_2026_secure-bridge"
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    return verifier, challenge


@pytest.fixture(autouse=True)
def _reset_identity_memory() -> None:
    from app.services import propertyquarry_google_identity as identity

    identity.reset_propertyquarry_google_identity_memory_for_tests()


def test_propertyquarry_google_config_never_falls_back_to_legacy_runtime_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _clear_propertyquarry_google(monkeypatch)
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_ID", "poisoned-client")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_CLIENT_SECRET", "poisoned-secret")
    monkeypatch.setenv("EA_GOOGLE_OAUTH_STATE_SECRET", "poisoned-state")

    assert identity.propertyquarry_google_identity_configured() is False
    with pytest.raises(RuntimeError, match="google_oauth_propertyquarry_state_secret_missing"):
        identity.load_propertyquarry_google_identity_config()


def test_identity_requires_independent_high_entropy_state_and_session_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET", "short")
    with pytest.raises(RuntimeError, match="google_oauth_propertyquarry_state_secret_weak"):
        identity.load_propertyquarry_google_identity_config()

    _configure_propertyquarry_google(monkeypatch)
    monkeypatch.delenv("PROPERTYQUARRY_IDENTITY_SESSION_SECRET")
    with pytest.raises(RuntimeError, match="google_oauth_propertyquarry_session_secret_missing"):
        identity.load_propertyquarry_google_identity_config()

    monkeypatch.setenv("PROPERTYQUARRY_IDENTITY_SESSION_SECRET", "x" * 64)
    with pytest.raises(RuntimeError, match="google_oauth_propertyquarry_session_secret_weak"):
        identity.load_propertyquarry_google_identity_config()

    shared_secret = "propertyquarry-shared-secret-with-enough-entropy"
    monkeypatch.setenv("PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET", shared_secret)
    monkeypatch.setenv("PROPERTYQUARRY_IDENTITY_SESSION_SECRET", shared_secret)
    with pytest.raises(RuntimeError, match="google_oauth_propertyquarry_secrets_must_differ"):
        identity.load_propertyquarry_google_identity_config()


def test_identity_start_is_prefixed_narrow_and_keeps_only_a_safe_local_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    packet = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to="/app/properties?run_id=run-42#shortlist",
    )
    parsed = urllib.parse.urlparse(packet.auth_url)
    query = urllib.parse.parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == identity.GOOGLE_AUTHORIZE_ENDPOINT
    assert query["scope"] == ["openid email profile"]
    assert query["include_granted_scopes"] == ["false"]
    assert "access_type" not in query
    assert query["state"][0].startswith("pqg1.")
    assert packet.return_to == "/app/properties?run_id=run-42#shortlist"
    state_payload = identity.read_propertyquarry_google_identity_state(packet.state)
    assert state_payload["return_to"] == packet.return_to
    assert state_payload["flow_nonce_hash"] == hashlib.sha256(packet.flow_nonce.encode("utf-8")).hexdigest()
    assert packet.flow_nonce not in packet.state

    unsafe = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to="https://example.invalid/escape",
    )
    assert unsafe.return_to == "/app/search"


def test_callback_requires_verified_email_discards_tokens_and_issues_local_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    packet = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to="/app/support",
    )
    monkeypatch.setattr(
        identity,
        "_exchange_google_code_for_tokens",
        lambda **_kwargs: {
            "access_token": "one-use-access-token",
            "refresh_token": "must-never-be-persisted",
            "id_token": "must-never-be-persisted-either",
        },
    )
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda token: {
            "sub": "google-subject-1",
            "email": "Owner@Example.com",
            "email_verified": True,
            "name": "Property Owner",
            "observed_token": token,
        },
    )

    session = identity.complete_propertyquarry_google_identity_callback(
        code="one-use-code",
        state=packet.state,
        flow_nonce=packet.flow_nonce,
    )
    resolved = identity.resolve_propertyquarry_identity_session(token=session.token)
    durable_snapshot = json.dumps(
        {
            "accounts": identity._MEMORY_ACCOUNTS,  # noqa: SLF001
            "sessions": identity._MEMORY_SESSIONS,  # noqa: SLF001
            "audit": identity._MEMORY_AUDIT,  # noqa: SLF001
        },
        sort_keys=True,
    )

    assert session.return_to == "/app/support"
    assert session.email == "owner@example.com"
    assert session.principal_id == f"user-{hashlib.sha256(b'owner@example.com').hexdigest()[:16]}"
    assert session.token.startswith("pqis1.")
    assert resolved is not None
    assert resolved["principal_id"] == session.principal_id
    assert resolved["source_kind"] == "propertyquarry_google_identity"
    assert "one-use-access-token" not in durable_snapshot
    assert "must-never-be-persisted" not in durable_snapshot
    assert "must-never-be-persisted-either" not in durable_snapshot


def test_unverified_google_email_fails_closed_without_account_or_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    packet = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to="/app/search",
    )
    monkeypatch.setattr(identity, "_exchange_google_code_for_tokens", lambda **_kwargs: {"access_token": "short-lived"})
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda _token: {
            "sub": "unverified-subject",
            "email": "unverified@example.com",
            "email_verified": False,
        },
    )

    with pytest.raises(RuntimeError, match="google_oauth_propertyquarry_email_unverified"):
        identity.complete_propertyquarry_google_identity_callback(
            code="code",
            state=packet.state,
            flow_nonce=packet.flow_nonce,
        )

    assert identity._MEMORY_ACCOUNTS == {}  # noqa: SLF001
    assert identity._MEMORY_SESSIONS == {}  # noqa: SLF001


def test_identity_state_replay_is_rejected_before_second_token_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    packet = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to="/app/search",
    )
    exchanges = {"count": 0}

    def exchange(**_kwargs):  # noqa: ANN003
        exchanges["count"] += 1
        return {"access_token": "short-lived"}

    monkeypatch.setattr(identity, "_exchange_google_code_for_tokens", exchange)
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda _token: {
            "sub": "replay-subject",
            "email": "replay@example.com",
            "email_verified": True,
        },
    )

    identity.complete_propertyquarry_google_identity_callback(
        code="code-1",
        state=packet.state,
        flow_nonce=packet.flow_nonce,
    )
    with pytest.raises(RuntimeError, match="google_oauth_propertyquarry_state_replayed"):
        identity.complete_propertyquarry_google_identity_callback(
            code="code-2",
            state=packet.state,
            flow_nonce=packet.flow_nonce,
        )
    assert exchanges["count"] == 1


def test_repeated_subject_maps_to_one_account_and_new_sessions_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    monkeypatch.setattr(identity, "_exchange_google_code_for_tokens", lambda **_kwargs: {"access_token": "transient"})
    observed = {"email": "first@example.com"}
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda _token: {
            "sub": "stable-google-subject",
            "email": observed["email"],
            "email_verified": True,
        },
    )
    first_start = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to="/app/search",
    )
    first = identity.complete_propertyquarry_google_identity_callback(
        code="first",
        state=first_start.state,
        flow_nonce=first_start.flow_nonce,
    )
    observed["email"] = "renamed@example.com"
    second_start = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to="/app/properties",
    )
    second = identity.complete_propertyquarry_google_identity_callback(
        code="second",
        state=second_start.state,
        flow_nonce=second_start.flow_nonce,
    )

    assert first.principal_id == second.principal_id
    assert first.session_id != second.session_id
    assert len(identity._MEMORY_ACCOUNTS) == 1  # noqa: SLF001
    assert len(identity._MEMORY_SESSIONS) == 2  # noqa: SLF001
    assert identity._MEMORY_ACCOUNTS[first.principal_id]["email"] == "renamed@example.com"  # noqa: SLF001


def test_concurrent_different_subjects_for_one_email_never_overwrite_an_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    starts = [
        identity.build_propertyquarry_google_identity_start(
            redirect_uri="https://propertyquarry.com/google/callback",
            return_to="/app/search",
        )
        for _ in range(2)
    ]
    monkeypatch.setattr(
        identity,
        "_exchange_google_code_for_tokens",
        lambda **kwargs: {"access_token": kwargs["code"]},
    )
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda token: {
            "sub": f"subject-{token}",
            "email": "conflict@example.com",
            "email_verified": True,
        },
    )

    def complete(index: int):  # noqa: ANN202
        return identity.complete_propertyquarry_google_identity_callback(
            code=f"code-{index}",
            state=starts[index].state,
            flow_nonce=starts[index].flow_nonce,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        sessions = list(executor.map(complete, range(2)))

    assert len({session.principal_id for session in sessions}) == 2
    assert len(identity._MEMORY_ACCOUNTS) == 2  # noqa: SLF001
    subject_hashes = {
        str(account["subject_hash"])
        for account in identity._MEMORY_ACCOUNTS.values()  # noqa: SLF001
    }
    assert subject_hashes == {
        hashlib.sha256(b"subject-code-0").hexdigest(),
        hashlib.sha256(b"subject-code-1").hexdigest(),
    }


def test_schema_preflight_receipt_names_only_the_four_identity_tables() -> None:
    from app.services import propertyquarry_google_identity as identity

    receipt = identity.propertyquarry_google_identity_schema_preflight()

    assert receipt == {
        "backend": "memory",
        "contract_name": "propertyquarry.google_identity_schema_preflight.v1",
        "generic_product_records_written": False,
        "provider_tokens_persisted": False,
        "ready": True,
        "schema_digest": receipt["schema_digest"],
        "tables": sorted(identity.GOOGLE_IDENTITY_TABLES),
    }
    assert str(receipt["schema_digest"]).startswith("sha256:")


def test_sign_in_page_and_route_have_actionable_configured_and_unavailable_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_propertyquarry_google(monkeypatch)
    client = _client(monkeypatch)

    configured = client.get(
        "/sign-in?return_to=%2Fapp%2Fproperties%3Frun_id%3Drun-42&session=expired",
    )
    assert configured.status_code == 200
    assert 'aria-label="Continue with Google"' in configured.text
    assert 'href="/sign-in/google?return_to=%2Fapp%2Fproperties%3Frun_id%3Drun-42"' in configured.text
    started = client.get(
        "/sign-in/google?return_to=%2Fapp%2Fproperties%3Frun_id%3Drun-42",
        follow_redirects=False,
    )
    assert started.status_code == 303
    assert started.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")

    _clear_propertyquarry_google(monkeypatch)
    unavailable = client.get("/sign-in?session=expired")
    assert unavailable.status_code == 200
    assert "Google sign-in is temporarily unavailable." in unavailable.text
    assert "data-propertyquarry-google-unavailable" in unavailable.text
    assert 'href="/sign-in?return_to=%2Fapp%2Fsearch#sign-in-options"' in unavailable.text
    if 'data-sign-in-unavailable' in unavailable.text:
        assert (
            '<button class="btn primary" type="button" data-pq-next-action data-focus-sign-in-options>'
            not in unavailable.text
        )


def test_prefixed_callback_bypasses_generic_google_and_sets_only_local_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import landing_setup
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    client = _client(monkeypatch)
    started = client.get("/sign-in/google?return_to=%2Fapp%2Fsupport", follow_redirects=False)
    start_cookies = started.headers.get_list("set-cookie")
    assert len(start_cookies) == 1
    assert start_cookies[0].startswith("propertyquarry_google_identity_flow=")
    assert "Path=/google/callback" in start_cookies[0]
    assert "HttpOnly" in start_cookies[0]
    assert "Secure" in start_cookies[0]
    assert all("ea_" not in header.lower() and "myexternalbrain" not in header.lower() for header in start_cookies)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(started.headers["location"]).query)["state"][0]
    monkeypatch.setattr(identity, "_exchange_google_code_for_tokens", lambda **_kwargs: {"access_token": "transient"})
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda _token: {
            "sub": "isolated-subject",
            "email": "isolated@example.com",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(
        landing_setup,
        "complete_google_oauth_callback",
        lambda **_kwargs: pytest.fail("generic callback must not run"),
    )
    monkeypatch.setattr(
        landing_setup,
        "build_product_service",
        lambda *_args, **_kwargs: pytest.fail("product service must not run"),
    )

    callback = client.get(
        "/google/callback",
        params={"code": "one-use-code", "state": state},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == "/app/support"
    callback_cookies = callback.headers.get_list("set-cookie")
    assert any(header.startswith("propertyquarry_identity_session=pqis1.") for header in callback_cookies)
    assert any(header.startswith("propertyquarry_google_identity_flow=") and "Max-Age=0" in header for header in callback_cookies)
    assert all("HttpOnly" in header and "Secure" in header and "SameSite=lax" in header for header in callback_cookies)
    workspace_cookie_deletions = [
        header for header in callback_cookies if header.startswith("ea_workspace_session=")
    ]
    assert workspace_cookie_deletions
    assert all("Max-Age=0" in header for header in workspace_cookie_deletions)
    assert all("myexternalbrain" not in header.lower() for header in callback_cookies)
    current = client.get("/sign-in/current-session", follow_redirects=False)
    assert current.status_code == 303
    assert current.headers["location"] == "/app/search"
    assert all(
        "ea_" not in header.lower() and "myexternalbrain" not in header.lower()
        for header in current.headers.get_list("set-cookie")
    )

    signed_out = client.post(
        "/app/actions/sign-out",
        data={"return_to": "/sign-in"},
        headers={"origin": "https://propertyquarry.com"},
        follow_redirects=False,
    )
    assert signed_out.status_code == 303
    sign_out_cookies = signed_out.headers.get_list("set-cookie")
    assert any(header.startswith("propertyquarry_identity_session=") and "Max-Age=0" in header for header in sign_out_cookies)
    assert any(header.startswith("ea_workspace_session=") and "Max-Age=0" in header for header in sign_out_cookies)
    assert any(header.startswith("ea_workspace_signed_out=1") for header in sign_out_cookies)
    assert all("myexternalbrain" not in header.lower() for header in sign_out_cookies)


def test_local_unprefixed_google_state_stays_on_generic_callback_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.app import create_app
    from app.api.routes import landing_setup

    _configure_propertyquarry_google(monkeypatch)
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("EA_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("EA_API_TOKEN", "")
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_DATABASE_URL", "")
    generic_calls = {"count": 0}
    monkeypatch.setattr(
        landing_setup,
        "read_google_oauth_state",
        lambda _state: {
            "browser_source": "sign_in",
            "return_to": "/app/support",
        },
    )

    def _generic_callback(**_kwargs):  # noqa: ANN003
        generic_calls["count"] += 1
        raise RuntimeError("generic-callback-sentinel")

    monkeypatch.setattr(
        landing_setup,
        "complete_google_oauth_callback",
        _generic_callback,
    )
    client = TestClient(create_app(), base_url="https://testserver")

    callback = client.get(
        "/google/callback",
        params={"code": "generic-code", "state": "legacy-state"},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert generic_calls["count"] == 1
    assert "google_error=generic-callback-sentinel" in callback.headers["location"]


def test_propertyquarry_host_rejects_unprefixed_google_state_without_generic_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import landing_setup

    _configure_propertyquarry_google(monkeypatch)
    generic_calls = {"count": 0}

    def _generic_callback(**_kwargs):  # noqa: ANN003
        generic_calls["count"] += 1
        raise AssertionError("generic callback must not run on the PropertyQuarry host")

    monkeypatch.setattr(
        landing_setup,
        "complete_google_oauth_callback",
        _generic_callback,
    )
    client = _client(monkeypatch)

    callback = client.get(
        "/google/callback",
        params={"code": "attacker-code", "state": "legacy-state"},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert generic_calls["count"] == 0
    assert "google_error=google_oauth_propertyquarry_state_invalid" in callback.headers["location"]
    assert any(
        header.startswith("propertyquarry_google_identity_flow=")
        and "Max-Age=0" in header
        for header in callback.headers.get_list("set-cookie")
    )


def test_registration_google_continuation_rejects_a_different_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    expected_email = "registered@example.com"
    binding = identity.propertyquarry_google_expected_email_binding(expected_email)
    client = _client(monkeypatch)
    started = client.get(
        "/sign-in/google?"
        + urllib.parse.urlencode(
            {
                "return_to": "/register?ready=1&google_identity=confirmed",
                "identity_binding": binding,
            }
        ),
        follow_redirects=False,
    )
    assert started.status_code == 303
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(started.headers["location"]).query
    )["state"][0]
    assert expected_email not in state
    assert identity.read_propertyquarry_google_identity_state(state)[
        "expected_email_binding"
    ] == binding
    monkeypatch.setattr(
        identity,
        "_exchange_google_code_for_tokens",
        lambda **_kwargs: {"access_token": "transient"},
    )
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda _token: {
            "sub": "different-google-subject",
            "email": "different@example.com",
            "email_verified": True,
        },
    )

    callback = client.get(
        "/google/callback",
        params={"code": "one-use-code", "state": state},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert "google_error=google_oauth_propertyquarry_email_mismatch" in callback.headers[
        "location"
    ]
    assert not any(
        header.startswith("propertyquarry_identity_session=pqis1.")
        for header in callback.headers.get_list("set-cookie")
    )


def test_callback_rejects_login_csrf_before_exchange_and_clears_flow_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    packet = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to="/app/support",
    )
    exchanges = {"count": 0}

    def exchange(**_kwargs):  # noqa: ANN003
        exchanges["count"] += 1
        return {"access_token": "must-not-be-issued"}

    monkeypatch.setattr(identity, "_exchange_google_code_for_tokens", exchange)
    client = _client(monkeypatch)
    callback = client.get(
        "/google/callback",
        params={"code": "attacker-code", "state": packet.state},
        headers={"cookie": f"{identity.GOOGLE_IDENTITY_FLOW_COOKIE_NAME}=wrong-browser-flow"},
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert "google_error=google_oauth_propertyquarry_flow_mismatch" in callback.headers["location"]
    assert exchanges["count"] == 0
    callback_cookies = callback.headers.get_list("set-cookie")
    assert len(callback_cookies) == 1
    assert callback_cookies[0].startswith("propertyquarry_google_identity_flow=")
    assert "Max-Age=0" in callback_cookies[0]
    assert "Path=/google/callback" in callback_cookies[0]
    assert "ea_" not in callback_cookies[0].lower()


def test_provider_denial_consumes_bound_state_and_clears_flow_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    client = _client(monkeypatch)
    started = client.get("/sign-in/google?return_to=%2Fapp%2Fsupport", follow_redirects=False)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(started.headers["location"]).query)["state"][0]
    flow_nonce = str(client.cookies.get(identity.GOOGLE_IDENTITY_FLOW_COOKIE_NAME) or "")
    exchanges = {"count": 0}

    def exchange(**_kwargs):  # noqa: ANN003
        exchanges["count"] += 1
        return {"access_token": "must-not-be-issued"}

    monkeypatch.setattr(identity, "_exchange_google_code_for_tokens", exchange)
    denied = client.get(
        "/google/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )

    assert denied.status_code == 303
    assert "google_error=google_oauth_propertyquarry_access_denied" in denied.headers["location"]
    assert exchanges["count"] == 0
    denied_cookies = denied.headers.get_list("set-cookie")
    assert len(denied_cookies) == 1
    assert denied_cookies[0].startswith("propertyquarry_google_identity_flow=")
    assert "Max-Age=0" in denied_cookies[0]
    with pytest.raises(RuntimeError, match="google_oauth_propertyquarry_state_replayed"):
        identity.complete_propertyquarry_google_identity_callback(
            code="late-code",
            state=state,
            flow_nonce=flow_nonce,
        )
    assert exchanges["count"] == 0


def test_propertyquarry_identity_cookie_is_ignored_on_another_product_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    packet = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to="/app/search",
    )
    monkeypatch.setattr(identity, "_exchange_google_code_for_tokens", lambda **_kwargs: {"access_token": "transient"})
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda _token: {
            "sub": "host-isolated-subject",
            "email": "host-isolated@example.com",
            "email_verified": True,
        },
    )
    session = identity.complete_propertyquarry_google_identity_callback(
        code="code",
        state=packet.state,
        flow_nonce=packet.flow_nonce,
    )
    client = _client(monkeypatch)

    response = client.get(
        "/sign-in/current-session",
        headers={
            "host": "other-product.example",
            "cookie": f"{identity.GOOGLE_IDENTITY_COOKIE_NAME}={session.token}",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in?current_session=missing"


def test_sign_out_clears_invalid_propertyquarry_cookie_without_legacy_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    client = _client(monkeypatch)
    response = client.post(
        "/app/actions/sign-out",
        data={"return_to": "/sign-in"},
        headers={
            "origin": "https://propertyquarry.com",
            "cookie": f"{identity.GOOGLE_IDENTITY_COOKIE_NAME}=pqis1.invalid-expired-cookie"
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    cookies = response.headers.get_list("set-cookie")
    assert any(
        header.startswith(f"{identity.GOOGLE_IDENTITY_COOKIE_NAME}=")
        and "Max-Age=0" in header
        for header in cookies
    )
    assert any(
        header.startswith("ea_workspace_session=") and "Max-Age=0" in header
        for header in cookies
    )
    assert any(header.startswith("ea_workspace_signed_out=1") for header in cookies)


def test_production_sign_out_bypasses_generic_auth_and_workspace_resolution_for_invalid_pq_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.app import create_app
    from app.api.routes import landing
    from app.services import propertyquarry_google_identity as identity
    from app.settings import RuntimeProfile

    _configure_propertyquarry_google(monkeypatch)
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("EA_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("EA_API_TOKEN", "configured-production-api-token")
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_DATABASE_URL", "")
    app = create_app()
    object.__setattr__(
        app.state.container,
        "runtime_profile",
        RuntimeProfile(
            mode="prod",
            storage_backend="postgres",
            durability="durable",
            auth_mode="token",
            principal_source="verified_identity",
            database_required=True,
            database_configured=True,
            source_backend="postgres",
        ),
    )
    monkeypatch.setattr(
        landing,
        "get_request_context",
        lambda *_args, **_kwargs: pytest.fail(
            "PQ-cookie sign-out must not resolve generic authentication"
        ),
    )
    monkeypatch.setattr(
        landing,
        "_workspace_session_payload",
        lambda *_args, **_kwargs: pytest.fail(
            "PQ-cookie sign-out must not resolve or touch an EA workspace session"
        ),
    )
    client = TestClient(app, base_url="https://propertyquarry.com")

    response = client.post(
        "/app/actions/sign-out",
        data={"return_to": "/sign-in"},
        headers={
            "origin": "https://propertyquarry.com",
            "cookie": (
                f"{identity.GOOGLE_IDENTITY_COOKIE_NAME}=pqis1.invalid-expired-cookie; "
                "ea_workspace_session=must-not-be-resolved"
            )
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/sign-in"
    cookies = response.headers.get_list("set-cookie")
    assert any(
        header.startswith(f"{identity.GOOGLE_IDENTITY_COOKIE_NAME}=")
        and "Max-Age=0" in header
        for header in cookies
    )
    assert any(
        header.startswith("ea_workspace_session=") and "Max-Age=0" in header
        for header in cookies
    )
    assert any(header.startswith("ea_workspace_signed_out=1") for header in cookies)


def test_sign_out_is_post_only_and_rejects_cross_origin_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    client = _client(monkeypatch)
    cookie = f"{identity.GOOGLE_IDENTITY_COOKIE_NAME}=pqis1.invalid-expired-cookie"

    get_response = client.get(
        "/app/actions/sign-out?return_to=%2Fsign-in",
        headers={"cookie": cookie},
        follow_redirects=False,
    )
    assert get_response.status_code == 405
    assert not get_response.headers.get_list("set-cookie")

    cross_origin = client.post(
        "/app/actions/sign-out",
        data={"return_to": "/sign-in"},
        headers={
            "origin": "https://attacker.example",
            "cookie": cookie,
        },
        follow_redirects=False,
    )
    assert cross_origin.status_code == 403
    assert cross_origin.json()["error"]["code"] == "cross_site_browser_mutation"
    assert not cross_origin.headers.get_list("set-cookie")


def test_google_identity_lane_has_static_cross_product_isolation() -> None:
    from app.api.routes import landing, landing_setup
    from app.services import propertyquarry_google_identity as identity

    module_source = inspect.getsource(identity)
    lowered = module_source.lower()
    assert "CREATE TABLE" not in module_source
    assert "EA_" not in module_source
    assert "myexternalbrain" not in lowered
    assert "provider_binding" not in lowered
    assert "connector" not in lowered
    assert "refresh_token" not in lowered
    assert "sync_google" not in lowered

    start_source = inspect.getsource(landing.sign_in_google)
    assert "app.services.google_oauth" not in start_source
    assert "build_product_service" not in start_source
    callback_source = inspect.getsource(landing_setup.google_oauth_browser_callback)
    identity_branch = callback_source.index("is_propertyquarry_google_identity_state")
    generic_state_read = callback_source.index("read_google_oauth_state")
    generic_callback = callback_source.index("complete_google_oauth_callback")
    assert identity_branch < generic_state_read < generic_callback


def test_rendered_propertyquarry_compose_wires_identity_only_to_api_without_ea_fallback() -> None:
    root = Path(__file__).resolve().parents[1]
    identity_environment = {
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_ID": "compose-propertyquarry-client",
        "PROPERTYQUARRY_GOOGLE_OAUTH_CLIENT_SECRET": "compose-propertyquarry-client-secret",
        "PROPERTYQUARRY_GOOGLE_OAUTH_REDIRECT_URI": "https://propertyquarry.com/google/callback",
        "PROPERTYQUARRY_GOOGLE_OAUTH_STATE_SECRET": "compose-propertyquarry-state-secret-with-entropy",
        "PROPERTYQUARRY_IDENTITY_SESSION_SECRET": "compose-propertyquarry-session-secret-with-entropy",
    }
    environment = os.environ.copy()
    environment.update(
        {
            **identity_environment,
            "EA_SIGNING_SECRET": "compose-test-signing-secret",
            "POSTGRES_PASSWORD": "compose-test-postgres-password",
                "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL": "postgresql://admission:test@db/propertyquarry",
                "PROPERTYQUARRY_API_DATABASE_URL": "postgresql://api:test@db/propertyquarry",
                "PROPERTYQUARRY_API_INGRESS_DATABASE_URL": "postgresql://ingress:test@db/propertyquarry",
                "PROPERTYQUARRY_MIGRATION_DATABASE_URL": "postgresql://migrate:test@db/propertyquarry",
            "PROPERTYQUARRY_RECONSTRUCTION_RENDER_BRIDGE_TOKEN": "compose-test-render-token",
            "PROPERTYQUARRY_RENDER_DATABASE_URL": "postgresql://render:test@db/propertyquarry",
            "PROPERTYQUARRY_SCHEDULER_DATABASE_URL": "postgresql://scheduler:test@db/propertyquarry",
            "PROPERTYQUARRY_WORKER_DATABASE_URL": "postgresql://worker:test@db/propertyquarry",
            # Poisoned legacy values prove Compose does not project them.
            "EA_GOOGLE_OAUTH_CLIENT_ID": "must-not-enter-propertyquarry",
            "EA_GOOGLE_OAUTH_CLIENT_SECRET": "must-not-enter-propertyquarry",
            "EA_GOOGLE_OAUTH_REDIRECT_URI": "https://myexternalbrain.invalid/google/callback",
            "EA_GOOGLE_OAUTH_STATE_SECRET": "must-not-enter-propertyquarry",
            "MEB_GOOGLE_OAUTH_CLIENT_ID": "must-not-enter-propertyquarry",
            "MYEXTERNALBRAIN_GOOGLE_OAUTH_CLIENT_ID": "must-not-enter-propertyquarry",
        }
    )
    rendered = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.property.yml", "config", "--format", "json"],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    services = dict(json.loads(rendered.stdout)["services"])
    api_environment = dict(services["propertyquarry-api"]["environment"])

    assert {name: api_environment.get(name) for name in identity_environment} == identity_environment
    for service_name, service in services.items():
        service_environment = dict(service.get("environment") or {})
        if service_name != "propertyquarry-api":
            assert not set(identity_environment).intersection(service_environment), service_name
        for name in service_environment:
            normalized_name = str(name).upper()
            assert not (
                "GOOGLE_OAUTH" in normalized_name
                and (
                    normalized_name.startswith("EA_")
                    or normalized_name.startswith("MEB_")
                    or "MYEXTERNALBRAIN" in normalized_name
                )
            ), f"{service_name}:{name}"


def test_mobile_identity_handoff_is_pkce_bound_single_use_and_issues_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    verifier, challenge = _mobile_pkce_pair()
    packet = identity.build_propertyquarry_google_identity_start(
        redirect_uri="https://propertyquarry.com/google/callback",
        return_to=identity.MOBILE_IDENTITY_RETURN_TO,
        mobile_pkce_challenge=challenge,
    )
    monkeypatch.setattr(
        identity,
        "_exchange_google_code_for_tokens",
        lambda **_kwargs: {"access_token": "transient"},
    )
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda _token: {
            "sub": "mobile-subject",
            "email": "mobile@example.com",
            "email_verified": True,
            "name": "Mobile Owner",
        },
    )
    browser_session = identity.complete_propertyquarry_google_identity_callback(
        code="provider-code",
        state=packet.state,
        flow_nonce=packet.flow_nonce,
    )
    handoff = identity.create_propertyquarry_mobile_identity_handoff(
        identity_session=browser_session,
    )

    assert browser_session.session_id == ""
    assert browser_session.token == ""
    assert identity._MEMORY_SESSIONS == {}
    assert not handoff.code.startswith(identity.GOOGLE_IDENTITY_SESSION_PREFIX)
    with pytest.raises(RuntimeError, match="pkce_mismatch"):
        identity.redeem_propertyquarry_mobile_identity_handoff(
            code=handoff.code,
            pkce_verifier="WrongVerifier_abcdefghijklmnopqrstuvwxyz0123456789",
        )

    app_session = identity.redeem_propertyquarry_mobile_identity_handoff(
        code=handoff.code,
        pkce_verifier=verifier,
    )
    assert app_session.session_id != browser_session.session_id
    assert identity.resolve_propertyquarry_identity_session(token=app_session.token)
    with pytest.raises(RuntimeError, match="handoff_replayed"):
        identity.redeem_propertyquarry_mobile_identity_handoff(
            code=handoff.code,
            pkce_verifier=verifier,
        )


def test_mobile_external_login_callback_redeems_into_an_httponly_webview_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import propertyquarry_google_identity as identity

    _configure_propertyquarry_google(monkeypatch)
    verifier, challenge = _mobile_pkce_pair()
    client = _client(monkeypatch)
    started = client.get(
        "/sign-in/google",
        params={
            "return_to": identity.MOBILE_IDENTITY_RETURN_TO,
            "mobile_challenge": challenge,
        },
        follow_redirects=False,
    )
    assert started.status_code == 303
    state = urllib.parse.parse_qs(
        urllib.parse.urlparse(started.headers["location"]).query
    )["state"][0]
    monkeypatch.setattr(
        identity,
        "_exchange_google_code_for_tokens",
        lambda **_kwargs: {"access_token": "transient"},
    )
    monkeypatch.setattr(
        identity,
        "_fetch_google_userinfo",
        lambda _token: {
            "sub": "mobile-route-subject",
            "email": "mobile-route@example.com",
            "email_verified": True,
        },
    )

    callback = client.get(
        "/google/callback",
        params={"code": "provider-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303
    callback_uri = urllib.parse.urlparse(callback.headers["location"])
    assert (callback_uri.scheme, callback_uri.netloc, callback_uri.path) == (
        "propertyquarry",
        "auth",
        "/callback",
    )
    assert not any(
        header.startswith(f"{identity.GOOGLE_IDENTITY_COOKIE_NAME}=")
        for header in callback.headers.get_list("set-cookie")
    )
    handoff_code = urllib.parse.parse_qs(callback_uri.query)["code"][0]

    redeemed = client.post(
        "/mobile/auth/redeem",
        json={"code": handoff_code, "pkce_verifier": verifier},
    )
    assert redeemed.status_code == 200
    assert redeemed.json() == {"status": "authenticated", "return_to": "/app/search"}
    identity_cookies = [
        header
        for header in redeemed.headers.get_list("set-cookie")
        if header.startswith(f"{identity.GOOGLE_IDENTITY_COOKIE_NAME}=")
    ]
    assert len(identity_cookies) == 1
    assert "HttpOnly" in identity_cookies[0]
    assert "Secure" in identity_cookies[0]
    assert "SameSite=lax" in identity_cookies[0]
