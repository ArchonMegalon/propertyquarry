from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.product import service as product_service


def _delivery_summary(*, updated_at: datetime) -> dict[str, object]:
    return {
        "updated_at": updated_at.isoformat(),
        "eligible_tour_total": 1,
        "ready_tour_total": 0,
        "pending_tour_total": 1,
        "blocked_tour_total": 0,
        "_delivery_candidates": [
            {
                "candidate_ref": "candidate-1",
                "source_ref": "source-1",
                "property_url": "https://example.test/listing/1",
                "tour_url": "https://propertyquarry.com/tours/unverified-tour",
                "tour_status": "created",
                "blocked_reason": "",
            }
        ],
    }


def _refresh(
    monkeypatch,
    *,
    summary: dict[str, object],
    events: dict[str, list[dict[str, object]]] | None = None,
) -> dict[str, object]:
    monkeypatch.setenv(
        "EA_PROPERTY_SEARCH_RESULTS_TOUR_VERIFICATION_MAX_PENDING_SECONDS",
        "3600",
    )
    monkeypatch.setattr(
        product_service,
        "_hosted_property_tour_verified_open_url",
        lambda _tour_url, *, principal_id: "",
    )
    service = object.__new__(product_service.ProductService)
    return service._refresh_property_search_results_delivery_state(
        principal_id="principal-1",
        result=summary,
        tour_events_by_source=events or {},
    )


def test_expired_unverified_tour_becomes_terminal_blocked_result(monkeypatch) -> None:
    summary = _delivery_summary(
        updated_at=datetime.now(timezone.utc) - timedelta(days=2)
    )

    refreshed = _refresh(monkeypatch, summary=summary)

    candidate = refreshed["_delivery_candidates"][0]
    assert candidate["tour_status"] == "blocked"
    assert candidate["blocked_reason"] == "hosted_tour_verification_expired"
    assert candidate["tour_url"] == (
        "https://propertyquarry.com/tours/unverified-tour"
    )
    assert refreshed["ready_tour_total"] == 0
    assert refreshed["pending_tour_total"] == 0
    assert refreshed["blocked_tour_total"] == 1
    assert refreshed["eligible_tour_total"] == 1
    service = object.__new__(product_service.ProductService)
    assert service._property_search_results_delivery_pending(result=refreshed) is False


def test_recent_unverified_tour_remains_pending_during_grace_period(monkeypatch) -> None:
    summary = _delivery_summary(updated_at=datetime.now(timezone.utc))

    refreshed = _refresh(monkeypatch, summary=summary)

    candidate = refreshed["_delivery_candidates"][0]
    assert candidate["tour_status"] == "created"
    assert candidate["blocked_reason"] == ""
    assert refreshed["ready_tour_total"] == 0
    assert refreshed["pending_tour_total"] == 0
    assert refreshed["blocked_tour_total"] == 0
    service = object.__new__(product_service.ProductService)
    assert service._property_search_results_delivery_pending(result=refreshed) is True


def test_recent_tour_event_renews_verification_grace_for_old_run(monkeypatch) -> None:
    summary = _delivery_summary(
        updated_at=datetime.now(timezone.utc) - timedelta(days=2)
    )
    event_url = "https://propertyquarry.com/tours/recent-unverified-tour"
    events = {
        "source-1": [
            {
                "event_type": "generic_property_tour_created",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "payload": {
                    "property_url": "https://example.test/listing/1",
                    "tour_url": event_url,
                },
            }
        ]
    }

    refreshed = _refresh(monkeypatch, summary=summary, events=events)

    candidate = refreshed["_delivery_candidates"][0]
    assert candidate["tour_url"] == event_url
    assert candidate["tour_status"] == "created"
    assert candidate["blocked_reason"] == ""
    assert refreshed["blocked_tour_total"] == 0
    service = object.__new__(product_service.ProductService)
    assert service._property_search_results_delivery_pending(result=refreshed) is True


def test_existing_blocked_tour_with_url_is_counted_as_terminal(monkeypatch) -> None:
    summary = _delivery_summary(updated_at=datetime.now(timezone.utc))
    candidate = summary["_delivery_candidates"][0]
    candidate["tour_status"] = "blocked"
    candidate["blocked_reason"] = "provider_acceptance_failed"

    refreshed = _refresh(monkeypatch, summary=summary)

    assert refreshed["pending_tour_total"] == 0
    assert refreshed["blocked_tour_total"] == 1
    service = object.__new__(product_service.ProductService)
    assert service._property_search_results_delivery_pending(result=refreshed) is False
