from __future__ import annotations

import json

from starlette.requests import Request

from ea.app.api.routes import landing


def _request(*, accept: str) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/app/research/missing-ref",
            "raw_path": b"/app/research/missing-ref",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"accept", accept.encode("ascii"))],
            "client": ("127.0.0.1", 1),
            "server": ("propertyquarry.com", 443),
        }
    )


def test_missing_packet_html_stays_on_property_page_and_queues_recovery(
    monkeypatch,
) -> None:
    observed: list[dict[str, object]] = []

    def _queue(**kwargs: object) -> str:
        observed.append(dict(kwargs))
        return "queue:repair-1"

    monkeypatch.setattr(landing, "_property_queue_missing_research_packet_repair", _queue)

    response = landing._property_missing_packet_response(
        _request(accept="text/html"),
        container=object(),
        principal_id="principal-1",
        run_id="",
        candidate_ref="missing-ref",
    )

    assert response.status_code == 202
    assert response.background is None
    assert len(observed) == 1
    assert observed[0]["principal_id"] == "principal-1"
    assert observed[0]["candidate_ref"] == "missing-ref"
    assert observed[0]["include_receipt"] is True
    body = response.body.decode("utf-8")
    assert "Property page is being rebuilt" in body
    assert 'role="status"' in body
    assert 'aria-live="polite"' in body
    assert "data-property-packet-recovery" in body
    assert "/app/shortlist?" in body


def test_missing_packet_json_keeps_immediate_queue_receipt(
    monkeypatch,
) -> None:
    observed: list[dict[str, object]] = []

    def _queue(**kwargs: object) -> dict[str, object]:
        observed.append(dict(kwargs))
        return {
            "queue_item_ref": "queue:repair-2",
            "repair_status": "returned",
            "replacement_run_id": "replacement-run-2",
        }

    monkeypatch.setattr(landing, "_property_queue_missing_research_packet_repair", _queue)

    response = landing._property_missing_packet_response(
        _request(accept="application/json"),
        container=object(),
        principal_id="principal-2",
        run_id="run-2",
        candidate_ref="missing-ref",
    )

    assert response.status_code == 202
    assert response.background is None
    assert len(observed) == 1
    payload = json.loads(response.body)
    assert payload["queue_item_ref"] == "queue:repair-2"
    assert payload["run_id"] == "run-2"
    assert payload["poll_url"] == "/app/research/missing-ref"
    assert payload["poll_after_ms"] >= 1000
    assert payload["fallback_url"].startswith("/app/shortlist?")
    assert payload["status"] == "recovery_running"
    assert payload["replacement_run_id"] == "replacement-run-2"
    assert payload["replacement_status_url"].endswith("/replacement-run-2")
