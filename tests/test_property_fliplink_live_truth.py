from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.repositories.property_packet_publications import (
    InMemoryPropertyPacketPublicationRepository,
)
from app.services.fliplink.browser_adapter import (
    browseract_fliplink_publish_requested,
)
from app.services.fliplink.service import FlipLinkPacketService
from tests.propertyquarry_phase_helpers import (
    property_client_with_workspace,
    reset_packet_repo,
    seed_packet,
)


FLIPLINK_ENV = (
    "FLIPLINK_LOGIN_EMAIL",
    "EA_FLIPLINK_LOGIN_EMAIL",
    "FLIPLINK_LOGIN_PASSWORD",
    "EA_FLIPLINK_LOGIN_PASSWORD",
    "FLIPLINK_BROWSERACT_ENABLED",
    "FLIPLINK_ACCOUNT_TIER",
    "FLIPLINK_ACTIVE_PUBLICATION_CAP",
    "FLIPLINK_CUSTOM_DOMAIN",
)


@pytest.fixture(autouse=True)
def _reset_publications() -> Iterator[None]:
    reset_packet_repo()
    yield
    reset_packet_repo()


def _clear_fliplink_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in FLIPLINK_ENV:
        monkeypatch.delenv(name, raising=False)


def _service(tmp_path: Path) -> FlipLinkPacketService:
    return FlipLinkPacketService(
        repo=InMemoryPropertyPacketPublicationRepository(),
        artifact_root=tmp_path,
    )


def _publish_request() -> dict[str, object]:
    return {
        "publication_id": "pub_test",
        "pdf_artifact_ref": "/private/pub_test.pdf",
        "source_pdf_sha256": "a" * 64,
        "redaction_receipt_present": True,
        "completion_endpoint": "/app/api/properties/packets/pub_test/complete",
        "privacy_mode": "family_review",
    }


def test_unconfigured_fliplink_reports_local_capacity_without_plan_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fliplink_env(monkeypatch)

    status = _service(tmp_path).capacity_status(principal_id="principal-one")

    assert status["capacity_scope"] == "local_packet_repository"
    assert status["external_provider"] == "FlipLink.me"
    assert status["external_account_configured"] is False
    assert status["external_account_verified"] is False
    assert status["external_publish_request_ready"] is False
    assert status["account_tier"] == 0
    assert status["account_tier_verified"] is False
    assert status["custom_domain"] == ""
    assert status["custom_domain_verified"] is False


def test_configured_values_still_do_not_become_verified_provider_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fliplink_env(monkeypatch)
    monkeypatch.setenv("FLIPLINK_LOGIN_EMAIL", "operator@example.test")
    monkeypatch.setenv("FLIPLINK_LOGIN_PASSWORD", "test-only-password")
    monkeypatch.setenv("FLIPLINK_BROWSERACT_ENABLED", "1")
    monkeypatch.setenv("FLIPLINK_ACCOUNT_TIER", "10")
    monkeypatch.setenv("FLIPLINK_CUSTOM_DOMAIN", "packets.example.test")

    status = _service(tmp_path).capacity_status(principal_id="principal-one")

    assert status["external_account_configured"] is True
    assert status["external_publish_request_ready"] is True
    assert status["external_account_verified"] is False
    assert status["account_tier"] == 0
    assert status["custom_domain"] == ""


def test_browseract_publish_requires_credentials_not_only_feature_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fliplink_env(monkeypatch)
    monkeypatch.setenv("FLIPLINK_BROWSERACT_ENABLED", "1")

    with pytest.raises(RuntimeError, match="fliplink_credentials_unconfigured"):
        browseract_fliplink_publish_requested(_publish_request())

    monkeypatch.setenv("FLIPLINK_LOGIN_EMAIL", "operator@example.test")
    monkeypatch.setenv("FLIPLINK_LOGIN_PASSWORD", "test-only-password")
    result = browseract_fliplink_publish_requested(_publish_request())
    assert result["status"] == "queued_operator_assist"
    assert result["provider"] == "fliplink"


def test_packet_dashboard_labels_local_and_external_truth() -> None:
    template = Path(
        "ea/app/templates/app/property_packets.html"
    ).read_text(encoding="utf-8")

    assert "External FlipLink publishing is not connected or verified." in template
    assert "Local packet storage is ready." in template
    assert "current sharing plan" not in template
    assert "data-external-publish-unavailable" in template
    assert "fliplink_capacity.external_publish_request_ready" in template


def test_unconfigured_packet_dashboard_renders_local_only_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_fliplink_env(monkeypatch)
    client = property_client_with_workspace(
        principal_id="fliplink-live-truth",
        tmp_path=tmp_path,
    )
    seed_packet(client, property_ref="vienna-local-packet")

    response = client.get("/app/properties/packets")

    assert response.status_code == 200, response.text
    assert "Local packet storage is ready." in response.text
    assert "External FlipLink publishing is not connected or verified." in response.text
    assert "data-external-publish-unavailable" in response.text
    assert "<button class=\"pq-pack-button small\" type=\"button\" data-browseract-publish" not in response.text
