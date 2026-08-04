from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timedelta, timezone

import app.product.service as product_service
import pytest
from app.api.routes.product_api_contracts import PropertyOneminEvaluationOut
from app.domain.models import ToolInvocationRequest, ToolInvocationResult
from app.product.property_fact_enrichment import (
    property_fact_distance_evidence_is_valid,
    property_fact_distance_specs,
)
from app.product.property_onemin_evaluation import (
    _google_maps_url_identity,
    property_onemin_safe_public_packet,
    run_property_google_maps_ooda,
    run_property_onemin_evaluation,
)
from app.product.property_search_storage import _compact_property_search_run_record
from app.product.service import ProductService
from tests.product_test_helpers import build_property_operator_client
from tests.test_property_fact_enrichment import (
    _candidate,
    _clear_run,
    _run_record,
    _seed_run,
    _valid_osm_research,
)


@pytest.fixture(autouse=True)
def _enable_onemin_property_evaluation(monkeypatch) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_ONEMIN_EVALUATION_ENABLED", "1")
    monkeypatch.setenv("PROPERTYQUARRY_ONEMIN_GOOGLE_MAPS_OODA_ENABLED", "1")


class _FakeToolExecution:
    def __init__(self) -> None:
        self.requests: list[ToolInvocationRequest] = []

    def execute_invocation(self, request: ToolInvocationRequest) -> ToolInvocationResult:
        self.requests.append(request)
        if request.tool_name == "provider.onemin.code_generate":
            return ToolInvocationResult(
                tool_name=request.tool_name,
                action_kind=request.action_kind,
                target_ref="property-evaluation",
                output_json={
                    "structured_output_json": {
                        "recommendation": "shortlist",
                        "confidence": 0.84,
                        "summary": "Strong daily-life fit with one nearby amenity still to verify.",
                        "strengths": ["Verified living area matches the preference."],
                        "risks": ["Supermarket distance was initially unknown."],
                        "evidence_keys": ["area_m2", "invented_fact"],
                        "missing_fact_keys": ["nearest_supermarket_m", "invented_fact"],
                        "research_actions": [
                            {
                                "fact_key": "nearest_supermarket_m",
                                "reason": "Resolve the selected soft filter.",
                                "travel_mode": "walking",
                                "priority": 1,
                            },
                            {
                                "fact_key": "area_m2",
                                "reason": "Resolved facts must never be researched again.",
                            },
                        ],
                    },
                    "provider_backend": "1min",
                },
                receipt_json={
                    "provider_backend": "1min",
                    "provider_account_name": "managed-primary",
                    "provider_key_slot": "slot-a",
                },
                model_name="deepseek-chat",
                tokens_in=420,
                tokens_out=180,
            )
        if request.tool_name == "browseract.extract_account_facts":
            fact_key = str(request.payload_json["account_hints_json"]["fact_key"])
            return ToolInvocationResult(
                tool_name=request.tool_name,
                action_kind=request.action_kind,
                target_ref="google-maps-research",
                output_json={
                    "facts_json": {
                        "fact_key": fact_key,
                        "place_name": "BILLA",
                        "place_category": "Supermarket",
                        "place_id": "ChIJtest12345",
                        "destination_latitude": 48.2105,
                        "destination_longitude": 16.3715,
                        "final_surface_url": (
                            "https://www.google.com/maps/place/BILLA/"
                            "data=!4m2!3m1!1sChIJtest12345"
                        ),
                        "visible_text": "BILLA · Supermarket · open today",
                    }
                },
                receipt_json={"binding_id": "maps-test"},
            )
        raise AssertionError(f"unexpected tool: {request.tool_name}")


def _fixtures() -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
    key = "nearest_supermarket_m"
    spec = next(row for row in property_fact_distance_specs() if row["key"] == key)
    facts: dict[str, object] = {
        "address": "Karlsplatz 1, 1010 Wien",
        "area_m2": 82,
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "map_location_precision": "address",
        "map_location_source": "listing",
    }
    plan = [
        {
            "key": "area_m2",
            "label": "Living area",
            "priority": "required",
            "state": "resolved",
            "value": 82,
            "evidence": {"provider": "listing"},
        },
        {
            "key": key,
            "label": str(spec["label"]),
            "priority": "lazy",
            "state": "unknown",
            "value": None,
            "evidence": {},
        },
    ]
    return facts, plan, spec


def test_google_maps_url_identity_uses_only_exact_google_url_material() -> None:
    final_url = (
        "https://www.google.com/maps/place/Lidl/@48.23359,16.36534,17z/"
        "data=!4m6!3m5!1s0x476d079da7c03951:0x629cc3d9b227236f!8m2"
        "!3d48.23359!4d16.36534"
    )

    place_id, latitude, longitude = _google_maps_url_identity(final_url)

    assert place_id == "0x476d079da7c03951:0x629cc3d9b227236f"
    assert latitude == pytest.approx(48.23359)
    assert longitude == pytest.approx(16.36534)
    assert _google_maps_url_identity(
        "https://example.test/maps/place/Lidl/@48.2,16.3"
    ) == ("", None, None)


def test_maps_ooda_accepts_canary_shape_only_when_url_binds_identity_and_coordinates(
    monkeypatch,
) -> None:
    facts, plan, spec = _fixtures()

    class _CanaryShapeToolExecution:
        def execute_invocation(
            self, request: ToolInvocationRequest
        ) -> ToolInvocationResult:
            return ToolInvocationResult(
                tool_name=request.tool_name,
                action_kind=request.action_kind,
                target_ref="google-maps-canary",
                output_json={
                    "facts_json": {
                        "place_name": "Lidl Österreich",
                        "place_category": "Discounter",
                        "place_id": None,
                        "destination_latitude": None,
                        "destination_longitude": None,
                        "final_surface_url": (
                            "https://www.google.com/maps/place/Lidl/"
                            "@48.23359,16.36534,17z/data=!4m6!3m5!1s"
                            "0x476d079da7c03951:0x629cc3d9b227236f!8m2"
                            "!3d48.23359!4d16.36534"
                        ),
                        "visible_text": (
                            "Lidl Österreich · Discounter · "
                            "Klosterneuburger Str. 79, 1200 Wien"
                        ),
                    }
                },
                receipt_json={"requested_workflow_id": "workflow-google-maps"},
            )

    monkeypatch.delenv(
        "PROPERTYQUARRY_GOOGLE_MAPS_BROWSERACT_BINDING_ID", raising=False
    )
    research, completed = run_property_google_maps_ooda(
        tool_execution=_CanaryShapeToolExecution(),
        principal_id="property-user-42",
        run_id="run-canary",
        candidate_ref="candidate-canary",
        property_url="https://example.test/listing/42",
        facts=facts,
        plan=plan,
        specs=[spec],
        evaluation={
            "status": "succeeded",
            "ooda": {
                "actions": [
                    {
                        "fact_key": "nearest_supermarket_m",
                        "label": "Nearest supermarket",
                        "travel_mode": "walking",
                        "priority": 1,
                    }
                ]
            },
        },
    )

    action = completed["ooda"]["actions"][0]
    assert action["status"] == "verified"
    assert action["provider_receipt"]["provider_object_id"] == (
        "0x476d079da7c03951:0x629cc3d9b227236f"
    )
    assert research["nearest_supermarket_m"] > 0


def test_manager_backed_evaluation_drives_governed_maps_ooda(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.onemin_manager.active_onemin_manager",
        lambda: object(),
    )
    tool_execution = _FakeToolExecution()
    facts, plan, spec = _fixtures()
    property_url = "https://example.test/listing/42"

    evaluation = run_property_onemin_evaluation(
        tool_execution=tool_execution,
        principal_id="principal-test",
        run_id="run-test",
        candidate_ref="candidate-test",
        candidate={"candidate_ref": "candidate-test", "title": "Vienna home"},
        facts=facts,
        preferences={"max_distance_to_supermarket_m": 900},
        plan=plan,
        score={
            "state": "final",
            "current": 87.0,
            "ranking_eligible": True,
            "algorithm_version": "propertyquarry.fact-score-state.v1",
        },
    )

    assert evaluation["status"] == "succeeded"
    assert evaluation["manager_routed"] is True
    assert evaluation["receipt"]["manager_routed"] is True
    assert evaluation["judgment"]["evidence_keys"] == ["area_m2"]
    assert evaluation["judgment"]["missing_fact_keys"] == [
        "nearest_supermarket_m"
    ]
    assert [row["fact_key"] for row in evaluation["ooda"]["actions"]] == [
        "nearest_supermarket_m"
    ]

    research, completed = run_property_google_maps_ooda(
        tool_execution=tool_execution,
        principal_id="principal-test",
        run_id="run-test",
        candidate_ref="candidate-test",
        property_url=property_url,
        facts=facts,
        plan=plan,
        specs=[spec],
        evaluation=evaluation,
    )

    assert [request.tool_name for request in tool_execution.requests] == [
        "provider.onemin.code_generate",
        "browseract.extract_account_facts",
    ]
    assert tool_execution.requests[1].payload_json["workflow_inputs_json"][
        "KeyWords"
    ] == "supermarket near Karlsplatz 1, 1010 Wien"
    assert all(request.context_json["suppress_telegram_delivery"] for request in tool_execution.requests)
    action = completed["ooda"]["actions"][0]
    assert action["status"] == "verified"
    assert action["browser_receipt"]["irreversible_actions_attempted"] == []
    assert action["browser_receipt"]["quality_gate"].startswith("pass:")

    value = research["nearest_supermarket_m"]
    evidence = research["property_fact_geo_evidence"]["nearest_supermarket_m"]
    assert evidence["provider"] == "google_maps_browseract"
    assert evidence["method"] == "straight_line_google_maps"
    assert property_fact_distance_evidence_is_valid(
        facts=facts,
        evidence=evidence,
        spec=spec,
        observed_source_key="nearest_supermarket_m",
        observed_value=value,
        property_url=property_url,
    )

    public_packet = property_onemin_safe_public_packet(completed)
    validated = PropertyOneminEvaluationOut.model_validate(public_packet)
    assert validated.receipt.manager_routed is True
    assert validated.ooda.actions[0].browser_receipt.completed_actions


def test_maps_ooda_keeps_worker_binding_authority_off_the_user_principal(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "PROPERTYQUARRY_GOOGLE_MAPS_BROWSERACT_BINDING_ID", "maps-binding-test"
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_GOOGLE_MAPS_BROWSERACT_PRINCIPAL_ID",
        "propertyquarry-operator",
    )
    facts, plan, spec = _fixtures()
    evaluation = {
        "status": "succeeded",
        "ooda": {
            "phase": "decide",
            "actions": [
                {
                    "action_id": "maps-research-1",
                    "fact_key": "nearest_supermarket_m",
                    "label": "Nearest supermarket",
                    "reason": "Resolve the selected soft filter.",
                    "travel_mode": "walking",
                    "priority": 1,
                    "status": "planned",
                }
            ],
        },
    }
    tool_execution = _FakeToolExecution()

    research, completed = run_property_google_maps_ooda(
        tool_execution=tool_execution,
        principal_id="property-user-42",
        run_id="run-test",
        candidate_ref="candidate-test",
        property_url="https://example.test/listing/42",
        facts=facts,
        plan=plan,
        specs=[spec],
        evaluation=evaluation,
    )

    request = tool_execution.requests[0]
    assert request.payload_json["binding_id"] == "maps-binding-test"
    assert request.context_json["principal_id"] == "propertyquarry-operator"
    assert request.context_json["requesting_principal_id"] == "property-user-42"
    assert request.context_json["provider_binding_principal_id"] == (
        "propertyquarry-operator"
    )
    assert research["nearest_supermarket_m"] > 0
    assert completed["ooda"]["actions"][0]["status"] == "verified"


def test_maps_ooda_uses_exact_coordinates_when_listing_address_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.delenv(
        "PROPERTYQUARRY_GOOGLE_MAPS_BROWSERACT_BINDING_ID", raising=False
    )
    facts, plan, spec = _fixtures()
    facts.pop("address")
    tool_execution = _FakeToolExecution()

    run_property_google_maps_ooda(
        tool_execution=tool_execution,
        principal_id="property-user-42",
        run_id="run-test",
        candidate_ref="candidate-test",
        property_url="https://example.test/listing/42",
        facts=facts,
        plan=plan,
        specs=[spec],
        evaluation={
            "status": "succeeded",
            "ooda": {
                "actions": [
                    {
                        "fact_key": "nearest_supermarket_m",
                        "label": "Nearest supermarket",
                        "travel_mode": "walking",
                        "priority": 1,
                    }
                ]
            },
        },
    )

    assert tool_execution.requests[0].payload_json["workflow_inputs_json"][
        "KeyWords"
    ] == "supermarket near 48.20820000,16.37380000"


def test_maps_ooda_fails_closed_when_binding_principal_is_missing(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "PROPERTYQUARRY_GOOGLE_MAPS_BROWSERACT_BINDING_ID", "maps-binding-test"
    )
    monkeypatch.delenv(
        "PROPERTYQUARRY_GOOGLE_MAPS_BROWSERACT_PRINCIPAL_ID", raising=False
    )
    facts, plan, spec = _fixtures()
    evaluation = {
        "status": "succeeded",
        "ooda": {
            "actions": [
                {
                    "action_id": "maps-research-1",
                    "fact_key": "nearest_supermarket_m",
                    "label": "Nearest supermarket",
                    "travel_mode": "walking",
                    "priority": 1,
                    "status": "planned",
                }
            ]
        },
    }
    tool_execution = _FakeToolExecution()

    research, completed = run_property_google_maps_ooda(
        tool_execution=tool_execution,
        principal_id="property-user-42",
        run_id="run-test",
        candidate_ref="candidate-test",
        property_url="https://example.test/listing/42",
        facts=facts,
        plan=plan,
        specs=[spec],
        evaluation=evaluation,
    )

    assert research == {}
    assert tool_execution.requests == []
    action = completed["ooda"]["actions"][0]
    assert action["status"] == "unavailable"
    assert action["blockers"] == ["browser_binding_principal_required"]


def test_evaluation_fails_closed_without_active_manager(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.onemin_manager.active_onemin_manager",
        lambda: None,
    )
    facts, plan, _spec = _fixtures()

    class _MustNotRun:
        def execute_invocation(self, request: ToolInvocationRequest) -> ToolInvocationResult:
            raise AssertionError(f"manager bypass attempted: {request.tool_name}")

    evaluation = run_property_onemin_evaluation(
        tool_execution=_MustNotRun(),
        principal_id="principal-test",
        run_id="run-test",
        candidate_ref="candidate-test",
        candidate={"candidate_ref": "candidate-test"},
        facts=facts,
        preferences={},
        plan=plan,
        score={"state": "final", "current": 87.0, "ranking_eligible": True},
    )

    assert evaluation["status"] == "unavailable"
    assert evaluation["manager_routed"] is False
    assert evaluation["error"]["code"] == "onemin_manager_unavailable"
    assert evaluation["ooda"]["actions"] == []


def test_compact_storage_preserves_bounded_onemin_receipts(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.onemin_manager.active_onemin_manager",
        lambda: object(),
    )
    tool_execution = _FakeToolExecution()
    facts, plan, spec = _fixtures()
    evaluation = run_property_onemin_evaluation(
        tool_execution=tool_execution,
        principal_id="principal-test",
        run_id="run-test",
        candidate_ref="candidate-test",
        candidate={"candidate_ref": "candidate-test"},
        facts=facts,
        preferences={},
        plan=plan,
        score={"state": "final", "current": 87.0, "ranking_eligible": True},
    )
    _research, evaluation = run_property_google_maps_ooda(
        tool_execution=tool_execution,
        principal_id="principal-test",
        run_id="run-test",
        candidate_ref="candidate-test",
        property_url="https://example.test/listing/42",
        facts=facts,
        plan=plan,
        specs=[spec],
        evaluation=evaluation,
    )
    record = {
        "run_id": "run-test",
        "principal_id": "principal-test",
        "status": "completed",
        "summary": {
            "status": "completed",
            "ranked_candidates": [
                {
                    "candidate_ref": "candidate-test",
                    "title": "Vienna home",
                    "ranking_eligible": True,
                    "onemin_evaluation": evaluation,
                }
            ],
            "fact_enrichment_jobs": {
                "pfe_0123456789abcdef01234567": {
                    "job_id": "pfe_0123456789abcdef01234567",
                    "status": "succeeded",
                    "onemin_evaluation": evaluation,
                }
            },
        },
    }

    compact = _compact_property_search_run_record(record)
    candidate_packet = compact["summary"]["ranked_candidates"][0][
        "onemin_evaluation"
    ]
    job_packet = compact["summary"]["fact_enrichment_jobs"][
        "pfe_0123456789abcdef01234567"
    ]["onemin_evaluation"]
    assert candidate_packet["receipt"]["manager_routed"] is True
    assert candidate_packet["ooda"]["actions"][0]["browser_receipt"][
        "quality_gate"
    ].startswith("pass:")
    assert job_packet["ooda"]["actions"][0]["provider_receipt"][
        "provider_object_id"
    ] == "ChIJtest12345"


def test_authenticated_fact_enrichment_e2e_renders_manager_judgment_and_maps_fact(
    monkeypatch,
) -> None:
    principal_id = "exec-property-onemin-e2e"
    run_id = "run-property-onemin-e2e"
    candidate_ref = "candidate-onemin-e2e"
    candidate = _candidate(candidate_ref=candidate_ref)
    candidate["property_facts"] = {
        "address": "Karlsplatz 1, 1010 Wien",
        "map_lat": 48.2082,
        "map_lng": 16.3738,
        "map_location_precision": "address",
        "map_location_source": "listing",
        "house_number": "1",
    }
    record = _run_record(
        principal_id=principal_id,
        run_id=run_id,
        candidate=candidate,
    )
    client = build_property_operator_client(principal_id=principal_id)
    tool_execution = _FakeToolExecution()
    container = client.app.state.container
    source_observed_at = datetime.now(timezone.utc)
    source_fingerprint = product_service.property_fact_source_fingerprint(
        str(candidate["property_url"])
    )

    monkeypatch.setattr(
        "app.services.onemin_manager.active_onemin_manager",
        lambda: object(),
    )
    monkeypatch.setattr(
        container.tool_execution,
        "execute_invocation",
        tool_execution.execute_invocation,
    )
    monkeypatch.setattr(
        product_service,
        "_property_fact_validated_source_url",
        lambda url: str(url),
    )
    monkeypatch.setattr(
        product_service,
        "_property_fact_coordinate_snapshot",
        lambda _property_url: {
            "map_lat": 48.2082,
            "map_lng": 16.3738,
            "map_location_precision": "address",
            "map_location_source": "listing_structured_coordinates",
            "house_number": "1",
            "map_coordinate_evidence": {
                "exact": True,
                "trusted": True,
                "provider": "listing_structured_coordinates",
                "source_fingerprint": source_fingerprint,
                "latitude": 48.2082,
                "longitude": 16.3738,
                "coordinate_digest": product_service.property_fact_coordinate_digest(
                    48.2082, 16.3738
                ),
                "observed_at": source_observed_at.isoformat(),
                "expires_at": (source_observed_at + timedelta(hours=6)).isoformat(),
            },
        },
    )

    def _osm_without_supermarket(
        latitude: float,
        longitude: float,
        property_url: str,
    ) -> dict[str, object]:
        research, _meta = _valid_osm_research(
            {
                "nearest_playground_m": 410,
                "nearest_pharmacy_m": 530,
                "nearest_medical_care_m": 570,
                "nearest_subway_m": 640,
            },
            property_url=property_url,
            latitude=latitude,
            longitude=longitude,
        )
        return research

    monkeypatch.setattr(
        product_service,
        "_property_research_nearby_pois",
        _osm_without_supermarket,
    )
    monkeypatch.setattr(
        product_service,
        "_property_alert_personal_fit_from_facts",
        lambda **_kwargs: {
            "domain": "willhaben",
            "fit_score": 68.0,
            "recommendation": "shortlist",
            "match_reasons_json": ["Verified facts fit the search."],
        },
    )
    _seed_run(record)
    endpoint = (
        f"/app/api/signals/property/search/run/{run_id}/candidates/"
        f"{candidate_ref}/fact-enrichment"
    )
    try:
        started = client.post(
            endpoint,
            json={"retry_failed": False},
            headers={
                "origin": "https://propertyquarry.com",
                "sec-fetch-site": "same-origin",
            },
        )
        assert started.status_code == 202, started.text

        deadline = time.monotonic() + 6.0
        payload: dict[str, object] = {}
        while time.monotonic() < deadline:
            response = client.get(endpoint)
            assert response.status_code == 200, response.text
            payload = response.json()
            if payload["status"] not in {"queued", "running"}:
                break
            time.sleep(0.02)

        assert payload["status"] == "succeeded", payload
        validated = ProductService(container).get_property_candidate_fact_enrichment(
            principal_id=principal_id,
            run_id=run_id,
            candidate_ref=candidate_ref,
        )
        assert validated is not None
        assert validated["onemin_evaluation"]["manager_routed"] is True
        assert payload["onemin_evaluation"]["ooda"]["actions"][0]["status"] == "verified", json.dumps(
            payload["onemin_evaluation"], indent=2
        )
        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            diagnostic_record = copy.deepcopy(
                product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id]
            )
        diagnostic_candidate = product_service._property_fact_find_candidate(
            diagnostic_record,
            candidate_ref=candidate_ref,
        )
        diagnostic_facts = dict(
            dict(diagnostic_candidate or {}).get("property_facts") or {}
        )
        maps_field = next(
            row
            for row in payload["fields"]
            if row["key"] == "nearest_supermarket_m"
        )
        assert maps_field["state"] == "resolved", {
            "field": maps_field,
            "onemin": payload["onemin_evaluation"],
            "value": diagnostic_facts.get("nearest_supermarket_m"),
            "evidence": dict(
                diagnostic_facts.get("property_fact_evidence") or {}
            ).get("nearest_supermarket_m"),
        }
        assert maps_field["provenance"]["provider"] == "google_maps_browseract"
        assert maps_field["provenance"]["method"] == "straight_line_google_maps"
        assert payload["score"]["current"] == 68.0
        assert PropertyOneminEvaluationOut.model_validate(
            payload["onemin_evaluation"]
        ).receipt.manager_routed is True

        with product_service._PROPERTY_SEARCH_RUN_LOCK:
            persisted = copy.deepcopy(
                product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id]
            )
        persisted_candidate = product_service._property_fact_find_candidate(
            persisted,
            candidate_ref=candidate_ref,
        )
        assert persisted_candidate is not None
        assert persisted_candidate["onemin_evaluation"]["status"] == "succeeded"
        assert persisted_candidate["property_facts"]["nearest_supermarket_m"] > 0

        research_page = client.get(
            f"/app/research/{candidate_ref}?run_id={run_id}"
        )
        assert research_page.status_code == 200, research_page.text
        assert "1minAI via EA 1min Manager" in research_page.text
        assert "AI property judgment" in research_page.text
        assert "Google Maps verified BILLA" in research_page.text
        assert "deterministic PropertyQuarry fit score remains authoritative" in research_page.text
    finally:
        _clear_run(run_id)
