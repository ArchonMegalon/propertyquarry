from __future__ import annotations

from ea.app.product import service


def test_private_showcase_is_disabled_without_an_explicit_allowlist(monkeypatch) -> None:
    monkeypatch.delenv("PROPERTYQUARRY_PRIVATE_SHOWCASE_ALLOWED_EMAILS", raising=False)

    assert service._property_private_showcase_allowed_emails() == frozenset()
    assert (
        service._property_private_showcase_allowed(
            principal_id="cf-email:owner@example.com",
            account_email="owner@example.com",
        )
        is False
    )


def test_private_showcase_accepts_only_normalized_configured_identities(monkeypatch) -> None:
    monkeypatch.setenv(
        "PROPERTYQUARRY_PRIVATE_SHOWCASE_ALLOWED_EMAILS",
        " Owner@Example.com, reviewer@example.com,not-an-email ",
    )

    assert service._property_private_showcase_allowed_emails() == frozenset(
        {"owner@example.com", "reviewer@example.com"}
    )
    assert (
        service._property_private_showcase_allowed(
            principal_id="cf-email:owner@example.com",
        )
        is True
    )
    assert (
        service._property_private_showcase_allowed(
            principal_id="cf-email:unlisted@example.com",
        )
        is False
    )
