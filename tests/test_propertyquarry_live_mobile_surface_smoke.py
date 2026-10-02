from __future__ import annotations

from scripts import propertyquarry_live_mobile_surface_smoke as mobile_smoke


def _cloudflare_1033_response() -> dict[str, object]:
    return {
        "status_code": 530,
        "headers": {"Server": "cloudflare", "CF-Ray": "redacted"},
        "text": "<span class='inline-block'>Error code: 1033</span>",
    }


def test_cloudflare_1033_is_reduced_to_a_sanitized_tunnel_failure() -> None:
    assert mobile_smoke.cloudflare_tunnel_failure_from_response(
        _cloudflare_1033_response()
    ) == {
        "provider": "cloudflare",
        "code": "1033",
        "reason": "cloudflare_tunnel_unavailable",
        "http_status": 530,
    }


def test_cloudflare_tunnel_failure_rejects_spoofed_or_ambiguous_responses() -> None:
    for response in (
        {**_cloudflare_1033_response(), "headers": {"Server": "example"}},
        {**_cloudflare_1033_response(), "status_code": 200},
        {**_cloudflare_1033_response(), "text": "Error code: 1016"},
        {**_cloudflare_1033_response(), "text": "upstream unavailable"},
    ):
        assert mobile_smoke.cloudflare_tunnel_failure_from_response(response) == {}


def test_live_probe_marks_uniform_cloudflare_1033_as_pre_route_blocked(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mobile_smoke,
        "_http_get_for_smoke",
        lambda *args, **kwargs: _cloudflare_1033_response(),
    )

    receipt = mobile_smoke.build_live_mobile_surface_receipt(
        base_url="https://propertyquarry.com",
        api_token="",
        principal_id="test-public-origin",
        routes=("/",),
        timeout_ms=1_000,
    )

    assert receipt["status"] == "blocked"
    assert receipt["error"] == "cloudflare_error_1033_tunnel_unavailable"
    assert receipt["edge_failure"] == {
        "provider": "cloudflare",
        "code": "1033",
        "reason": "cloudflare_tunnel_unavailable",
        "http_status": 530,
    }
    assert receipt["routes"][0]["metrics"]["edge_failure"] == receipt["edge_failure"]
    assert "CF-Ray" not in str(receipt)


def test_uniform_tunnel_failure_requires_every_attempted_route() -> None:
    failure = {
        "provider": "cloudflare",
        "code": "1033",
        "reason": "cloudflare_tunnel_unavailable",
        "http_status": 530,
    }
    rows = [
        {"ok": False, "metrics": {"edge_failure": failure}},
        {"ok": False, "metrics": {}},
    ]
    assert mobile_smoke.uniform_cloudflare_tunnel_failure(rows) == {}
