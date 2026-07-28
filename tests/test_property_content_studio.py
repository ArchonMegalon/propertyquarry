from __future__ import annotations

from pathlib import Path

from app.services.property_content_job_ledger import PropertyContentJobLedger
from app.services.property_content_packet_builder import build_product_tutorial_source_packet
from app.services.property_content_studio import PropertyContentStudio
from app.services.property_content_validation import validate_property_content_source_packet
from tests.product_test_helpers import build_property_operator_client


_SYSTEM_OWNERSHIP = {
    "principal_id": "propertyquarry:system:content-studio",
    "ownership_scope": "system",
    "search_run_id": "",
}


def _studio(tmp_path: Path) -> PropertyContentStudio:
    return PropertyContentStudio(ledger=PropertyContentJobLedger(path=tmp_path / "content-ledger.json"))


def test_content_studio_prepares_packet_and_does_not_call_disabled_provider(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SUBSCRIBR_ENABLED", "0")
    packet = build_product_tutorial_source_packet(title="How to Read a PropertyQuarry Dossier")
    studio = _studio(tmp_path)

    prepared = studio.prepare_source_packet(packet, **_SYSTEM_OWNERSHIP)
    requested = studio.request_subscribr_script(packet, **_SYSTEM_OWNERSHIP)

    assert prepared["status"] == "SOURCE_PACKET_APPROVED"
    assert requested["status"] == "SOURCE_PACKET_APPROVED"
    assert requested["provider_status"] == "disabled"
    assert requested["publication_allowed"] is False
    assert studio.ledger.get_job(packet["packet_id"], **_SYSTEM_OWNERSHIP)["source_packet_json"]["packet_id"] == packet["packet_id"]


def test_content_studio_validation_route_and_admin_surface(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_CONTENT_JOB_LEDGER", str(tmp_path / "ledger.json"))
    client = build_property_operator_client(principal_id="content-ops")
    packet = build_product_tutorial_source_packet(title="How to Read a PropertyQuarry Dossier")

    validation = client.post("/app/api/property/content/source-packets/validate", json={"packet": packet})
    create = client.post(
        "/app/api/property/content/source-packets/product-tutorial",
        json={"title": "How to Read a PropertyQuarry Dossier"},
    )
    page = client.get("/admin/property/content-studio")

    assert validation.status_code == 200
    assert validation.json()["status"] == "pass"
    assert create.status_code == 200
    assert create.json()["job"]["status"] == "SOURCE_PACKET_APPROVED"
    assert page.status_code == 200
    assert "Property Content Studio" in page.text
    assert "Direct publication" in page.text


def test_content_studio_templates_support_dark_surfaces() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    templates = (
        repo_root / "ea/app/templates/admin/property_content_studio.html",
        repo_root / "ea/app/templates/admin/property_content_job.html",
    )
    for template in templates:
        body = template.read_text(encoding="utf-8")
        assert "color-scheme: light dark;" in body
        assert "@media (prefers-color-scheme: dark)" in body
        assert "background: #fff;" not in body
        assert "background: #ffffff;" not in body
        assert "color: #18211f;" not in body


def test_invalid_packet_is_rejected_before_subscribr_job(tmp_path: Path) -> None:
    packet = build_product_tutorial_source_packet(title="Bad private packet")
    packet["user_email"] = "buyer@example.com"
    studio = _studio(tmp_path)

    job = studio.request_subscribr_script(packet, **_SYSTEM_OWNERSHIP)

    assert validate_property_content_source_packet(packet)["status"] == "fail"
    assert job["status"] == "SOURCE_REJECTED"
    assert job["provider_status"] == "blocked"
