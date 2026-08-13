from __future__ import annotations

from app.product.service import ProductService
from tests.product_test_helpers import build_product_client


def test_property_opportunity_generation_is_principal_scoped_and_truthfully_provenanced(monkeypatch) -> None:
    client = build_product_client(principal_id="opportunity-owner")
    candidate = {
        "candidate_ref": "candidate-1",
        "title": "Quiet Vienna flat",
        "source_platform": "willhaben",
        "property_facts": {"rooms": 3, "has_floorplan": True},
    }

    monkeypatch.setattr(
        ProductService,
        "get_property_search_run_status",
        lambda self, *, principal_id, run_id: {
            "run_id": run_id,
            "principal_id": principal_id,
            "summary": {
                "opportunity_person_id": "elisabeth",
                "ranked_candidates": [candidate],
            },
        },
    )

    response = client.post(
        "/app/api/property/opportunities/candidate-1/generate",
        json={"run_id": "run-1", "artifact_type": "why_shortlisted"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["opportunity"]["opportunity_id"]
    assert body["opportunity"]["person_id"] == "elisabeth"
    assert body["artifact"]["opportunity_id"] == body["opportunity"]["opportunity_id"]
    assert body["artifact"]["generation_provider"] == "PropertyQuarry"
    assert body["artifact"]["generation_mode"] == "local_opportunity_brief"
    assert body["artifact"]["generation_basis"] == "durable_preference_assessment"
    assert body["generation"] == {
        "provider": "PropertyQuarry",
        "mode": "local_opportunity_brief",
        "basis": "durable_preference_assessment",
        "artifact_status": "ready",
    }
    assert body["publication"] == {
        "mode": "local_only",
        "scope": "private_artifact",
        "status": "not_published",
        "external_provider": "",
        "external_publication_verified": False,
        "external_receipt_ref": "",
    }


def test_property_opportunity_generation_rejects_unknown_candidate(monkeypatch) -> None:
    client = build_product_client(principal_id="opportunity-owner")
    monkeypatch.setattr(
        ProductService,
        "get_property_search_run_status",
        lambda self, *, principal_id, run_id: {
            "run_id": run_id,
            "principal_id": principal_id,
            "summary": {"ranked_candidates": []},
        },
    )

    response = client.post(
        "/app/api/property/opportunities/not-owned/generate",
        json={"run_id": "run-1"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "property_opportunity_not_found"


def test_property_opportunity_generation_refreshes_durable_assessment_from_run_brief(monkeypatch) -> None:
    client = build_product_client(principal_id="opportunity-owner")
    candidate = {
        "candidate_ref": "candidate-existing",
        "title": "Existing opportunity",
        "opportunity": {
            "opportunity_id": "assessment:existing",
            "status": "ready",
            "person_id": "elisabeth",
            "run_id": "run-existing",
            "match_reasons": ["The layout matches."],
            "private_prompt": "must not leave the server",
        },
    }
    monkeypatch.setattr(
        ProductService,
        "get_property_search_run_status",
        lambda self, *, principal_id, run_id: {
            "run_id": run_id,
            "principal_id": principal_id,
            "property_search_preferences": {"max_price_eur": 3500},
            "summary": {"ranked_candidates": [candidate]},
        },
    )
    captured: dict[str, object] = {}

    def refresh(self, **kwargs):  # noqa: ANN001, ANN003
        del self
        captured.update(kwargs)
        refreshed = kwargs["sources"][0]["top_candidates"][0]
        refreshed["opportunity"] = {
            **candidate["opportunity"],
            "fit_score": 74.0,
            "confidence": 0.68,
            "recommendation": "shortlist",
            "match_reasons": ["The rent is within the active search ceiling."],
        }
        return {
            "opportunity_total": 1,
            "opportunity_persistence_failed_total": 0,
            "opportunity_person_id": "elisabeth",
            "opportunity_generation_status": "ready",
        }

    monkeypatch.setattr(
        ProductService,
        "_materialize_property_search_opportunities",
        refresh,
    )

    response = client.post(
        "/app/api/property/opportunities/candidate-existing/generate",
        json={"run_id": "run-existing"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["opportunity"]["opportunity_id"] == "assessment:existing"
    assert response.json()["opportunity"]["fit_score"] == 74.0
    assert captured["search_preferences"] == {"max_price_eur": 3500}
    assert "private_prompt" not in response.json()["opportunity"]
