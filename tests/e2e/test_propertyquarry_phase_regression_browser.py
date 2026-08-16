from __future__ import annotations

from pathlib import Path

import pytest

from tests.propertyquarry_phase_helpers import property_client_with_workspace, reset_packet_repo, seed_packet


@pytest.fixture(autouse=True)
def _reset_repo() -> None:
    reset_packet_repo()


def test_core_propertyquarry_pages_still_render(monkeypatch, tmp_path: Path) -> None:
    client = property_client_with_workspace(principal_id="pq-regression-browser", tmp_path=tmp_path)
    seed_packet(client, property_ref="listing-regression")

    properties = client.get("/app/properties")
    assert properties.status_code == 200
    assert "PropertyQuarry" in properties.text

    packets = client.get("/app/properties/packets")
    assert packets.status_code == 200
    assert "Save local review packets and keep the replies together." in packets.text

    settings = client.get("/app/settings")
    assert settings.status_code == 200
    assert "Account" in settings.text or "Settings" in settings.text or "Rules" in settings.text
