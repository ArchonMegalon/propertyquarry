from __future__ import annotations

from types import SimpleNamespace

from app import runner


def _container_with_principals(*principal_ids: str) -> SimpleNamespace:
    return SimpleNamespace(
        onboarding=SimpleNamespace(
            list_property_search_agent_principals=lambda *, limit: list(principal_ids)[:limit]
        )
    )


def test_property_scout_discovery_excludes_synthetic_probe_principals(monkeypatch) -> None:
    monkeypatch.delenv("EA_PROPERTY_SCOUT_PRINCIPAL_IDS", raising=False)
    monkeypatch.delenv("EA_PROPERTY_SCOUT_EXCLUDED_PRINCIPAL_IDS", raising=False)
    container = _container_with_principals(
        "cf-email:thumbnail-audit@propertyquarry.local",
        "pq-auth-performance-smoke",
        "pq-live-mobile-smoke",
        "cf-email:real-user@example.com",
    )

    assert runner._scheduler_property_scout_principal_ids(container) == (
        "cf-email:real-user@example.com",
    )


def test_property_scout_explicit_allowlist_cannot_reenable_excluded_principal(monkeypatch) -> None:
    monkeypatch.setenv(
        "EA_PROPERTY_SCOUT_PRINCIPAL_IDS",
        "pq-live-mobile-smoke,cf-email:real-user@example.com",
    )
    monkeypatch.setenv(
        "EA_PROPERTY_SCOUT_EXCLUDED_PRINCIPAL_IDS",
        "cf-email:paused-user@example.com",
    )

    assert runner._scheduler_property_scout_principal_ids(SimpleNamespace()) == (
        "cf-email:real-user@example.com",
    )


def test_property_scout_custom_exclusion_applies_to_discovery(monkeypatch) -> None:
    monkeypatch.delenv("EA_PROPERTY_SCOUT_PRINCIPAL_IDS", raising=False)
    monkeypatch.setenv(
        "EA_PROPERTY_SCOUT_EXCLUDED_PRINCIPAL_IDS",
        "cf-email:paused-user@example.com",
    )
    container = _container_with_principals(
        "cf-email:paused-user@example.com",
        "cf-email:real-user@example.com",
    )

    assert runner._scheduler_property_scout_principal_ids(container) == (
        "cf-email:real-user@example.com",
    )
