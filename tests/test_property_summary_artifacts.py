from __future__ import annotations

from pathlib import Path

import pytest

from app.services.fliplink.service import build_fliplink_packet_service
from tests.propertyquarry_phase_helpers import property_client_with_workspace, reset_packet_repo, seed_packet


@pytest.fixture(autouse=True)
def _reset_repo() -> None:
    reset_packet_repo()


def test_summary_artifact_generation_and_attachment_contract(tmp_path: Path) -> None:
    client = property_client_with_workspace(principal_id="pq-phase3-contract", tmp_path=tmp_path)
    publication_id = seed_packet(client, property_ref="listing-phase3")
    artifact = client.post(
        "/app/api/property-summaries/generate",
        json={"subject_type": "property", "subject_id": "listing-phase3", "artifact_type": "why_shortlisted", "audience_type": "family"},
    )
    assert artifact.status_code == 200, artifact.text
    artifact_id = artifact.json()["artifact"]["artifact_id"]

    fetched = client.get(f"/app/api/property-summaries/{artifact_id}")
    assert fetched.status_code == 200
    assert fetched.json()["artifact"]["artifact_type"] == "why_shortlisted"

    attached = client.post(
        f"/app/api/properties/packets/{publication_id}/attach-summary",
        json={"artifact_id": artifact_id},
    )
    assert attached.status_code == 200, attached.text

    packet = client.get(f"/app/api/properties/packets/{publication_id}")
    assert packet.status_code == 200
    assert packet.json()["publication"]["attached_summaries"][0]["artifact_id"] == artifact_id


def test_summary_artifact_copy_uses_customer_facing_share_language(tmp_path: Path) -> None:
    client = property_client_with_workspace(principal_id="pq-phase3-copy", tmp_path=tmp_path)
    publication_id = seed_packet(client, property_ref="listing-phase3-copy")
    assert publication_id

    service = build_fliplink_packet_service(client.app.state.container)
    artifact = service.generate_summary_artifact(
        principal_id="pq-phase3-copy",
        subject_type="property",
        subject_id="listing-phase3-copy",
        artifact_type="why_shortlisted",
    )

    body = str(artifact.get("body_markdown") or artifact.get("body") or "")
    assert "review-ready" not in body
    assert "shareable packet" not in body
    assert "This home is worth sharing now." in body


def test_opportunity_brief_is_local_and_carries_decision_evidence(tmp_path: Path) -> None:
    client = property_client_with_workspace(principal_id="pq-opportunity-copy", tmp_path=tmp_path)
    service = build_fliplink_packet_service(client.app.state.container)

    artifact = service.generate_summary_artifact(
        principal_id="pq-opportunity-copy",
        subject_type="property",
        subject_id="listing-opportunity-copy",
        artifact_type="why_shortlisted",
        context_json={
            "title": "Quiet Vienna flat",
            "property_url": "https://example.com/listings/quiet-vienna-flat?source=propertyquarry",
            "opportunity": {
                "opportunity_id": "property_opportunity:test",
                "fit_score": 88,
                "confidence": 0.82,
                "recommendation": "shortlist",
                "predicted_reaction": "Strong initial fit, pending the cost check",
                "match_reasons": ["The layout matches the three-room preference."],
                "mismatch_reasons": ["The heating type is not confirmed."],
                "unknowns": ["monthly_operating_costs"],
                "blocking_constraints": ["Confirm financing before making an offer."],
            },
        },
    )

    assert artifact["generation_provider"] == "PropertyQuarry"
    assert artifact["generation_mode"] == "local_opportunity_brief"
    assert artifact["generation_basis"] == "durable_preference_assessment"
    assert artifact["publication_mode"] == "local_only"
    assert artifact["external_publication_status"] == "not_published"
    assert artifact["external_publication_verified"] is False
    body = str(artifact["body_markdown"])
    assert body.startswith("# Quiet Vienna flat\n")
    assert "**Recommendation:** shortlist" in body
    assert "**Preference fit:** 88/100" in body
    assert "**Confidence:** 82%" in body
    assert (
        "**Predicted reaction:** Strong initial fit, pending the cost check."
        in body
    )
    assert "\n## Why it fits\n- " in body
    assert "The layout matches the three-room preference" in body
    assert "\n## Trade-offs\n- The heating type is not confirmed" in body
    assert "\n## Blocking constraints\n- Confirm financing before making an offer" in body
    assert ".." not in body
    assert "\n## Verify next\n- monthly operating costs" in body
    assert "[Open property](https://example.com/listings/quiet-vienna-flat?source=propertyquarry)" in body


def test_opportunity_brief_sanitizes_untrusted_markdown_and_urls(tmp_path: Path) -> None:
    client = property_client_with_workspace(principal_id="pq-opportunity-safety", tmp_path=tmp_path)
    service = build_fliplink_packet_service(client.app.state.container)

    artifact = service.generate_summary_artifact(
        principal_id="pq-opportunity-safety",
        subject_type="property",
        subject_id="listing-opportunity-safety",
        artifact_type="why_shortlisted",
        context_json={
            "title": "<script>alert(1)</script>",
            "property_url": "javascript:alert(1)",
            "opportunity": {
                "opportunity_id": "property_opportunity:safety",
                "fit_score": 0,
                "confidence": 0,
                "recommendation": "review",
                "match_reasons": ["A [label](https://malicious.example) is not a link."],
            },
        },
    )

    body = str(artifact["body_markdown"])
    assert "<script>" not in body
    assert "javascript:" not in body
    assert "[Open property]" not in body
    assert "**Preference fit:** 0/100" in body
    assert "**Confidence:** 0%" in body
    assert r"\[label\]" in body
