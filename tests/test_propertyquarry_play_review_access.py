from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.services import propertyquarry_play_review_access as play_review


TEST_USERNAME = "play-reviewer@propertyquarry.com"
TEST_PASSWORD = "Test-Play-Review-Password-42!"
TEST_DIGEST = play_review.build_password_digest(
    TEST_PASSWORD,
    salt=b"0123456789abcdef",
)


@pytest.fixture(autouse=True)
def _reset_attempt_limiter() -> None:
    play_review.PLAY_REVIEW_ATTEMPT_LIMITER.reset()
    yield
    play_review.PLAY_REVIEW_ATTEMPT_LIMITER.reset()


def _client(monkeypatch: pytest.MonkeyPatch, *, configured: bool = True) -> TestClient:
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("EA_LEDGER_BACKEND", raising=False)
    monkeypatch.setenv("EA_API_TOKEN", "")
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_PUBLIC_APP_BASE_URL", "https://propertyquarry.com")
    monkeypatch.setenv("PROPERTYQUARRY_RUNTIME_PROFILE", "propertyquarry")
    if configured:
        monkeypatch.setenv(play_review.PLAY_REVIEW_USERNAME_ENV, TEST_USERNAME)
        monkeypatch.setenv(play_review.PLAY_REVIEW_PASSWORD_DIGEST_ENV, TEST_DIGEST)
    else:
        monkeypatch.delenv(play_review.PLAY_REVIEW_USERNAME_ENV, raising=False)
        monkeypatch.delenv(play_review.PLAY_REVIEW_PASSWORD_DIGEST_ENV, raising=False)
    from app.api.app import create_app

    return TestClient(create_app(), base_url="https://propertyquarry.com")


def _credentials(*, password: str = TEST_PASSWORD, return_to: str = "/app/search") -> dict[str, str]:
    return {
        "username": TEST_USERNAME,
        "password": password,
        "return_to": return_to,
    }


def test_play_review_password_digest_is_strong_and_verifiable() -> None:
    assert TEST_DIGEST.startswith("pbkdf2_sha256$600000$")
    assert TEST_PASSWORD not in TEST_DIGEST
    assert play_review.password_matches(TEST_PASSWORD, TEST_DIGEST) is True
    assert play_review.password_matches("wrong-password", TEST_DIGEST) is False


def test_play_review_route_is_unlinked_and_fails_closed_without_complete_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, configured=False)

    unavailable = client.get("/sign-in/play-review")
    ordinary_sign_in = client.get("/sign-in")

    assert unavailable.status_code == 404
    assert ordinary_sign_in.status_code == 200
    assert "Secure review access" not in ordinary_sign_in.text

    monkeypatch.setenv(play_review.PLAY_REVIEW_USERNAME_ENV, TEST_USERNAME)
    monkeypatch.setenv(play_review.PLAY_REVIEW_PASSWORD_DIGEST_ENV, "not-a-valid-digest")
    malformed = client.get("/sign-in/play-review")
    assert malformed.status_code == 404


def test_play_review_page_never_renders_password_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    response = client.get("/sign-in/play-review?return_to=%2Fapp%2Fsearch")

    assert response.status_code == 200
    assert "Secure review access" in response.text
    assert 'action="/sign-in/play-review"' in response.text
    assert TEST_DIGEST not in response.text
    assert TEST_PASSWORD not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive, nosnippet"


def test_play_review_post_requires_same_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    response = client.post(
        "/sign-in/play-review",
        data=_credentials(),
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert "ea_workspace_session=" not in str(response.headers.get("set-cookie") or "")


def test_play_review_rejects_invalid_credentials_with_generic_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    response = client.post(
        "/sign-in/play-review",
        data=_credentials(password="wrong-password"),
        headers={"origin": "https://propertyquarry.com"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    assert "Those reviewer credentials could not be confirmed" in response.text
    assert TEST_USERNAME not in response.text
    assert TEST_DIGEST not in response.text
    assert "ea_workspace_session=" not in str(response.headers.get("set-cookie") or "")


def test_play_review_issues_revocable_workspace_cookie_and_opens_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    response = client.post(
        "/sign-in/play-review",
        data=_credentials(),
        headers={"origin": "https://propertyquarry.com"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/app/search"
    set_cookie = str(response.headers.get("set-cookie") or "")
    assert "ea_workspace_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert TEST_DIGEST not in set_cookie

    search = client.get("/app/search", follow_redirects=False)
    assert search.status_code == 200
    assert "Search" in search.text


def test_play_review_rejects_external_return_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)

    response = client.post(
        "/sign-in/play-review",
        data=_credentials(return_to="https://example.invalid/phish"),
        headers={"origin": "https://propertyquarry.com"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/app/search"


def test_play_review_rate_limits_repeated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    headers = {"origin": "https://propertyquarry.com"}

    for _ in range(5):
        rejected = client.post(
            "/sign-in/play-review",
            data=_credentials(password="wrong-password"),
            headers=headers,
            follow_redirects=False,
        )
        assert rejected.status_code == 401

    blocked = client.post(
        "/sign-in/play-review",
        data=_credentials(),
        headers=headers,
        follow_redirects=False,
    )
    assert blocked.status_code == 429
    assert "ea_workspace_session=" not in str(blocked.headers.get("set-cookie") or "")


def test_play_review_secrets_are_passed_only_to_api_runtime() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    compose = (repository_root / "docker-compose.property.yml").read_text(encoding="utf-8")
    env_example = (repository_root / ".env.example").read_text(encoding="utf-8")
    api_block, other_services = compose.split("  propertyquarry-migrate:", 1)

    assert "PROPERTYQUARRY_PLAY_REVIEW_USERNAME" in api_block
    assert "PROPERTYQUARRY_PLAY_REVIEW_PASSWORD_DIGEST" in api_block
    assert "PROPERTYQUARRY_PLAY_REVIEW_USERNAME" not in other_services
    assert "PROPERTYQUARRY_PLAY_REVIEW_PASSWORD_DIGEST" not in other_services
    assert "PROPERTYQUARRY_PLAY_REVIEW_USERNAME=" in env_example
    assert "PROPERTYQUARRY_PLAY_REVIEW_PASSWORD_DIGEST=" in env_example
