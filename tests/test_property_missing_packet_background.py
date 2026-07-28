from __future__ import annotations

import asyncio
import json

from starlette.background import BackgroundTask
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


def test_missing_packet_html_defers_repair_until_after_redirect(
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

    assert response.status_code == 307
    assert response.headers["location"].startswith("/app/shortlist?")
    assert observed == []
    assert isinstance(response.background, BackgroundTask)

    asyncio.run(response.background())

    assert len(observed) == 1
    assert observed[0]["principal_id"] == "principal-1"
    assert observed[0]["candidate_ref"] == "missing-ref"


def test_missing_packet_json_keeps_immediate_queue_receipt(
    monkeypatch,
) -> None:
    observed: list[dict[str, object]] = []

    def _queue(**kwargs: object) -> str:
        observed.append(dict(kwargs))
        return "queue:repair-2"

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
