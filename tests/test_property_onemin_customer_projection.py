from __future__ import annotations

from app.api.routes.landing_property_workspace_payload import (
    _property_workbench_client_candidate_payload,
)
from app.api.routes.product_api_delivery import (
    _property_search_lightweight_candidate_payload,
)


def _candidate_with_onemin_assessment() -> dict[str, object]:
    input_digest = "sha256:" + ("a" * 64)
    evaluated_at = "2026-08-12T13:23:46+00:00"
    return {
        "candidate_ref": "property-scout:42",
        "title": "Vienna home",
        "onemin_evaluation": {
            "schema_version": "propertyquarry.onemin-evaluation.v2",
            "status": "succeeded",
            "input_digest": input_digest,
            "manager_routed": True,
            "evaluated_at": evaluated_at,
            "judgment": {
                "recommendation": "shortlist",
                "confidence": 0.82,
                "summary": "Strong preference fit with one distance left to verify.",
                "strengths": ["Verified living area fits."],
                "risks": ["Supermarket distance is still unknown."],
                "evidence_keys": ["area_m2"],
                "missing_fact_keys": ["nearest_supermarket_m"],
            },
            "receipt": {
                "provider_backend": "1min",
                "provider_account_name": "must-not-reach-customer",
                "provider_key_slot": "must-not-reach-customer",
                "model": "deepseek-chat",
                "manager_routed": True,
                "input_digest": input_digest,
                "evaluated_at": evaluated_at,
            },
        },
    }


def test_initial_and_live_candidate_payloads_expose_same_safe_ai_assessment() -> None:
    candidate = _candidate_with_onemin_assessment()

    initial = _property_workbench_client_candidate_payload(candidate)
    live = _property_search_lightweight_candidate_payload(
        candidate,
        run_id="run-42",
        index=1,
    )

    assert initial["ai_assessment"] == live["ai_assessment"]
    assessment = dict(initial["ai_assessment"])
    assert assessment["provider"] == "1minAI"
    assert assessment["recommendation"] == "shortlist"
    assert assessment["receipt"]["manager_routed"] is True
    assert "provider_account_name" not in str(assessment)
    assert "provider_key_slot" not in str(assessment)


def test_detached_onemin_receipt_is_absent_from_both_customer_payloads() -> None:
    candidate = _candidate_with_onemin_assessment()
    candidate["onemin_evaluation"]["receipt"]["input_digest"] = (
        "sha256:" + ("b" * 64)
    )

    initial = _property_workbench_client_candidate_payload(candidate)
    live = _property_search_lightweight_candidate_payload(
        candidate,
        run_id="run-42",
        index=1,
    )

    assert "ai_assessment" not in initial
    assert "ai_assessment" not in live
