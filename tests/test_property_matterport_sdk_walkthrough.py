from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import urllib.parse

from scripts.propertyquarry_playwright_runtime import playwright_chromium_launch_kwargs
from scripts.materialize_propertyquarry_matterport_sdk_walkthrough import (
    build_sdk_walkthrough_contract,
)
from scripts.materialize_propertyquarry_matterport_model_publication import (
    build_publication_contract,
)

from app.api.routes import public_tours
from app.api.routes.public_tour_payloads import _PUBLIC_TOUR_TOP_LEVEL_KEYS
from app.api.routes.public_tours import (
    _MATTERPORT_SDK_BOOTSTRAP_URL,
    _matterport_sdk_walkthrough_context,
    _matterport_sdk_walkthrough_contract,
    _matterport_public_control_context,
    _public_tour_security_headers,
    _public_tour_primary_control_path,
    _tour_control_public_matterport_html,
    _tour_control_matterport_html,
)


def _walkthrough_contract() -> dict[str, object]:
    return {
        "status": "pass",
        "model_sid": "MODEL123",
        "cut_count": 0,
        "dissolve_count": 0,
        "teleport_count": 0,
        "start_ss": 1,
        "walkable_room_ids": ["room-aaa", "room-bbb"],
        "route": [
            {
                "sweep_id": "sweep-001",
                "room_id": "room-aaa",
                "rotation": {"x": 0, "y": 10},
                "transition_time_ms": 1600,
            },
            {
                "sweep_id": "sweep-002",
                "room_id": "room-bbb",
                "rotation": {"x": 0, "y": -30},
                "transition_time_ms": 1900,
            },
        ],
    }


def _publication_contract(*, model_sid: str = "MODEL123") -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "contract_name": "propertyquarry.matterport_model_publication.v1",
        "status": "pass",
        "model_sid": model_sid,
        "model_available": True,
        "checked_at": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "asset_valid_until": (now + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
        "proof_valid_until": (now + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
        "enabled_sweep_count": 23,
        "available_sweep_count": 23,
        "connected_component_count": 1,
        "room_count": 6,
        "navigation_edge_count": 49,
        "source_sha256": "a" * 64,
    }


def _payload(*, requested: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "slug": "sdk-loft",
        "title": "SDK Loft",
        "matterport_url": "https://my.matterport.com/show/?m=MODEL123",
        "matterport_walkthrough": _walkthrough_contract(),
        "matterport_model_publication": _publication_contract(),
    }
    if requested:
        payload["_tour_control_matterport_walkthrough"] = True
    return payload


def test_sdk_contract_requires_zero_edits_and_every_walkable_room() -> None:
    assert _matterport_sdk_walkthrough_contract(_payload())

    cut_payload = _payload()
    cut_payload["matterport_walkthrough"] = {
        **_walkthrough_contract(),
        "cut_count": 1,
    }
    assert _matterport_sdk_walkthrough_contract(cut_payload) == {}

    missing_room_payload = _payload()
    missing_room_payload["matterport_walkthrough"] = {
        **_walkthrough_contract(),
        "walkable_room_ids": ["room-aaa", "room-bbb", "room-ccc"],
    }
    assert _matterport_sdk_walkthrough_contract(missing_room_payload) == {}


def test_sdk_context_fails_closed_without_key_or_explicit_request(monkeypatch) -> None:
    monkeypatch.delenv("MATTERPORT_SDK_KEY", raising=False)
    monkeypatch.delenv("MATTERPORT_APPLICATION_KEY", raising=False)
    assert (
        _matterport_sdk_walkthrough_context(
            _payload(),
            external_url="https://my.matterport.com/show/?m=MODEL123",
        )
        == {}
    )

    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")
    assert (
        _matterport_sdk_walkthrough_context(
            _payload(requested=False),
            external_url="https://my.matterport.com/show/?m=MODEL123",
        )
        == {}
    )


def test_sdk_context_rejects_model_mismatch(monkeypatch) -> None:
    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")

    context = _matterport_sdk_walkthrough_context(
        _payload(),
        external_url="https://my.matterport.com/show/?m=OTHER999",
    )

    assert context == {}


def test_sdk_context_requires_fresh_matching_model_publication(monkeypatch) -> None:
    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")
    missing = _payload()
    missing.pop("matterport_model_publication")
    assert (
        _matterport_sdk_walkthrough_context(
            missing,
            external_url="https://my.matterport.com/show/?m=MODEL123",
        )
        == {}
    )

    mismatched = _payload()
    mismatched["matterport_model_publication"] = _publication_contract(model_sid="OTHER999")
    assert (
        _matterport_sdk_walkthrough_context(
            mismatched,
            external_url="https://my.matterport.com/show/?m=MODEL123",
        )
        == {}
    )

    current = _payload()
    current_publication = _publication_contract()
    current_publication["checked_at"] = (
        datetime.now(timezone.utc) - timedelta(days=29)
    ).isoformat().replace("+00:00", "Z")
    current_publication["proof_valid_until"] = (
        datetime.now(timezone.utc) + timedelta(hours=12)
    ).isoformat().replace("+00:00", "Z")
    current["matterport_model_publication"] = current_publication
    assert _matterport_sdk_walkthrough_context(
        current,
        external_url="https://my.matterport.com/show/?m=MODEL123",
    )

    stale = _payload()
    stale_publication = _publication_contract()
    stale_publication["checked_at"] = (
        datetime.now(timezone.utc) - timedelta(days=31)
    ).isoformat().replace("+00:00", "Z")
    stale["matterport_model_publication"] = stale_publication
    assert (
        _matterport_sdk_walkthrough_context(
            stale,
            external_url="https://my.matterport.com/show/?m=MODEL123",
        )
        == {}
    )

    expired = _payload()
    expired_publication = _publication_contract()
    expired_publication["asset_valid_until"] = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).isoformat().replace("+00:00", "Z")
    expired_publication["proof_valid_until"] = expired_publication[
        "asset_valid_until"
    ]
    expired["matterport_model_publication"] = expired_publication
    assert (
        _matterport_sdk_walkthrough_context(
            expired,
            external_url="https://my.matterport.com/show/?m=MODEL123",
        )
        == {}
    )


def test_sdk_context_builds_interactive_keyed_iframe(monkeypatch) -> None:
    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")

    context = _matterport_sdk_walkthrough_context(
        _payload(),
        external_url="https://my.matterport.com/show/?m=MODEL123&play=1",
    )

    query = urllib.parse.parse_qs(urllib.parse.urlparse(str(context["iframe_url"])).query)
    assert query["m"] == ["MODEL123"]
    assert query["applicationKey"] == ["domain-key-123"]
    assert query["play"] == ["0"]
    assert query["qs"] == ["1"]
    assert query["ss"] == ["1"]
    assert context["contract"]["transition"] == "fly"
    assert context["publication"]["model_available"] is True


def test_matterport_control_emits_only_native_fly_walkthrough_when_ready(monkeypatch) -> None:
    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")

    body = _tour_control_matterport_html(_payload())

    assert _MATTERPORT_SDK_BOOTSTRAP_URL in body
    assert "domain-key-123" in body
    assert "window.MP_SDK.connect" in body
    assert "mpSdk.Sweep.moveTo" in body
    assert "mpSdk.Camera.TransitionType.FLY" in body
    assert "data-matterport-walkthrough-toggle" in body
    assert 'aria-label="Pause walkthrough"' in body
    assert 'aria-live="polite"' in body
    assert "propertyquarry:matterport-walkthrough" in body
    assert "waitForMatterportWalkthroughResume" in body
    assert 'data.matterportWalkthroughState' not in body
    assert "FADEOUT" not in body
    assert "INSTANT" not in body
    assert "walkable_room_coverage_missing" in body


def test_matterport_control_does_not_expose_sdk_key_during_manual_view(monkeypatch) -> None:
    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")

    body = _tour_control_matterport_html(_payload(requested=False))

    assert _MATTERPORT_SDK_BOOTSTRAP_URL not in body
    assert "domain-key-123" not in body
    assert "applicationKey" not in body
    assert "mpSdk.Sweep.moveTo" not in body


def test_primary_tour_entry_selects_topology_verified_matterport_control(monkeypatch) -> None:
    monkeypatch.delenv("MATTERPORT_SDK_KEY", raising=False)
    monkeypatch.delenv("MATTERPORT_APPLICATION_KEY", raising=False)
    assert _public_tour_primary_control_path(_payload()) == "/tours/sdk-loft/control/matterport"

    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")
    assert _public_tour_primary_control_path(_payload()) == "/tours/sdk-loft/control/matterport"


def test_public_route_keeps_receipts_private_and_csp_scopes_provider_exactly() -> None:
    assert "matterport_walkthrough" not in _PUBLIC_TOUR_TOP_LEVEL_KEYS
    assert "matterport_model_publication" not in _PUBLIC_TOUR_TOP_LEVEL_KEYS
    csp = _public_tour_security_headers()["Content-Security-Policy"]
    assert "https://static.matterport.com" not in csp
    assert "https://*.matterport.com" not in csp
    assert "https://my.matterport.com" not in csp
    matterport_csp = _public_tour_security_headers(
        allow_matterport=True
    )["Content-Security-Policy"]
    assert "https://my.matterport.com" in matterport_csp


def test_public_matterport_control_requires_multi_room_connected_capture() -> None:
    payload = _payload(requested=False)
    assert _matterport_public_control_context(payload)
    body = _tour_control_public_matterport_html(payload, nonce="a" * 24)
    assert "Captured 3D Tour" in body
    assert "https://my.matterport.com/show/?m=MODEL123" in body
    assert "MATTERPORT_SDK_KEY" not in body

    single_room = _payload(requested=False)
    single_room["matterport_model_publication"] = {
        **_publication_contract(),
        "room_count": 1,
    }
    assert _matterport_public_control_context(single_room) == {}


def test_public_matterport_control_hides_empty_media_filters_and_exposes_direct_fallback(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        public_tours,
        "_tour_control_media_context",
        lambda _payload: (
            [
                {
                    "name": "Living room",
                    "role": "photo",
                    "image_url": "/tours/files/sdk-loft/living-room.jpg",
                    "mime_type": "image/jpeg",
                }
            ],
            "",
            "",
        ),
    )

    body = public_tours._tour_control_external_iframe_html(
        title="SDK Loft",
        iframe_src="https://my.matterport.com/show/?m=MODEL123",
        badge="Captured 3D Tour",
        payload=_payload(requested=False),
        fullscreen_href="/tours/sdk-loft/control/matterport?fullscreen=1",
    )

    assert 'id="role-filter"' not in body
    assert ">Floor plans</button>" not in body
    assert "Open 3D tour in new tab" in body
    assert 'target="_blank"' in body


def test_sdk_walkthrough_browser_executes_only_fly_moves(monkeypatch) -> None:
    from playwright.sync_api import sync_playwright

    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")
    body = _tour_control_matterport_html(_payload())
    sdk_stub = """
      window.__sdkMoves = [];
      window.MP_SDK = {
        connect: async function (_frame, key) {
          if (key !== 'domain-key-123') throw new Error('wrong_key');
          let observer = null;
          return {
            Camera: { TransitionType: { FLY: 'FLY' } },
            Sweep: {
              current: { subscribe: function (callback) { observer = callback; } },
              moveTo: async function (id, options) {
                window.__sdkMoves.push({ id, transition: options.transition, transitionTime: options.transitionTime });
                if (options.transition !== 'FLY') throw new Error('non_fly_transition');
                if (observer) observer({ id });
                return id;
              }
            }
          };
        }
      };
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**playwright_chromium_launch_kwargs(playwright))
        context = browser.new_context()
        page = context.new_page()

        def handle_route(route) -> None:  # noqa: ANN001
            if route.request.url == _MATTERPORT_SDK_BOOTSTRAP_URL:
                route.fulfill(status=200, content_type="application/javascript", body=sdk_stub)
            elif route.request.url.startswith("https://my.matterport.com/show/"):
                route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>Viewer</title>")
            else:
                route.abort()

        page.route("**/*", handle_route)
        page.set_content(body, wait_until="domcontentloaded")
        page.wait_for_function(
            "window.__PROPERTYQUARRY_MATTERPORT_WALKTHROUGH__?.status === 'pass'",
            timeout=10000,
        )
        result = page.evaluate(
            "() => ({proof: window.__PROPERTYQUARRY_MATTERPORT_WALKTHROUGH__, moves: window.__sdkMoves})"
        )
        context.close()
        browser.close()

    assert result["proof"]["status"] == "pass"
    assert result["proof"]["transition"] == "fly"
    assert result["proof"]["walkable_room_count"] == 2
    assert result["proof"]["missing_room_count"] == 0
    assert [move["id"] for move in result["moves"]] == ["sweep-001", "sweep-002"]
    assert {move["transition"] for move in result["moves"]} == {"FLY"}


def test_sdk_walkthrough_browser_pauses_between_sweeps(monkeypatch) -> None:
    from playwright.sync_api import sync_playwright

    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")
    payload = _payload()
    contract = _walkthrough_contract()
    contract["route"] = [
        *list(contract["route"]),
        {
            "sweep_id": "sweep-003",
            "room_id": "room-aaa",
            "transition_time_ms": 1700,
        },
    ]
    payload["matterport_walkthrough"] = contract
    body = _tour_control_matterport_html(payload)
    sdk_stub = """
      window.__sdkMoves = [];
      window.MP_SDK = {
        connect: async function () {
          let observer = null;
          return {
            Camera: { TransitionType: { FLY: 'FLY' } },
            Sweep: {
              current: { subscribe: function (callback) { observer = callback; } },
              moveTo: async function (id, options) {
                window.__sdkMoves.push({ id, transition: options.transition });
                if (window.__sdkMoves.length === 1) {
                  await new Promise((resolve) => { window.__releaseFirstSdkMove = resolve; });
                }
                if (observer) observer({ id });
                return id;
              }
            }
          };
        }
      };
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**playwright_chromium_launch_kwargs(playwright))
        context = browser.new_context()
        page = context.new_page()

        def handle_route(route) -> None:  # noqa: ANN001
            if route.request.url == _MATTERPORT_SDK_BOOTSTRAP_URL:
                route.fulfill(status=200, content_type="application/javascript", body=sdk_stub)
            elif route.request.url.startswith("https://my.matterport.com/show/"):
                route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>Viewer</title>")
            else:
                route.abort()

        page.route("**/*", handle_route)
        page.set_content(body, wait_until="domcontentloaded")
        page.wait_for_function("window.__sdkMoves?.length === 1", timeout=10000)
        toggle = page.locator("[data-matterport-walkthrough-toggle]")
        toggle.click()
        page.wait_for_function(
            "document.documentElement.dataset.matterportWalkthroughState === 'paused'",
            timeout=10000,
        )
        page.evaluate("() => window.__releaseFirstSdkMove()")
        page.wait_for_timeout(200)
        paused = page.evaluate(
            "() => ({moves: window.__sdkMoves.length, label: document.querySelector('[data-matterport-walkthrough-toggle]')?.getAttribute('aria-label')})"
        )
        toggle.click()
        page.wait_for_function(
            "window.__PROPERTYQUARRY_MATTERPORT_WALKTHROUGH__?.status === 'pass'",
            timeout=10000,
        )
        result = page.evaluate(
            "() => ({proof: window.__PROPERTYQUARRY_MATTERPORT_WALKTHROUGH__, moves: window.__sdkMoves})"
        )
        context.close()
        browser.close()

    assert paused == {"moves": 1, "label": "Resume walkthrough"}
    assert [move["id"] for move in result["moves"]] == ["sweep-001", "sweep-002", "sweep-003"]
    assert result["proof"]["status"] == "pass"
    assert result["proof"]["route_node_count"] == 3


def test_sdk_walkthrough_reduced_motion_requires_explicit_play(monkeypatch) -> None:
    from playwright.sync_api import sync_playwright

    monkeypatch.setenv("MATTERPORT_SDK_KEY", "domain-key-123")
    body = _tour_control_matterport_html(_payload())
    sdk_stub = """
      window.__sdkMoves = [];
      window.MP_SDK = {
        connect: async function () {
          return {
            Camera: { TransitionType: { FLY: 'FLY' } },
            Sweep: {
              current: { subscribe: function () {} },
              moveTo: async function (id, options) {
                window.__sdkMoves.push({ id, transition: options.transition });
                return id;
              }
            }
          };
        }
      };
    """

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**playwright_chromium_launch_kwargs(playwright))
        context = browser.new_context(reduced_motion="reduce")
        page = context.new_page()

        def handle_route(route) -> None:  # noqa: ANN001
            if route.request.url == _MATTERPORT_SDK_BOOTSTRAP_URL:
                route.fulfill(status=200, content_type="application/javascript", body=sdk_stub)
            elif route.request.url.startswith("https://my.matterport.com/show/"):
                route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>Viewer</title>")
            else:
                route.abort()

        page.route("**/*", handle_route)
        page.set_content(body, wait_until="domcontentloaded")
        page.wait_for_function(
            "window.__PROPERTYQUARRY_MATTERPORT_WALKTHROUGH__?.status === 'manual'",
            timeout=10000,
        )
        before = page.evaluate(
            "() => ({moves: window.__sdkMoves.length, label: document.querySelector('[data-matterport-walkthrough-toggle]')?.getAttribute('aria-label')})"
        )
        page.locator("[data-matterport-walkthrough-toggle]").click()
        page.wait_for_function(
            "window.__PROPERTYQUARRY_MATTERPORT_WALKTHROUGH__?.status === 'pass'",
            timeout=10000,
        )
        after = page.evaluate("() => window.__sdkMoves")
        context.close()
        browser.close()

    assert before == {"moves": 0, "label": "Play walkthrough"}
    assert [move["id"] for move in after] == ["sweep-001", "sweep-002"]
    assert {move["transition"] for move in after} == {"FLY"}


def test_sdk_materializer_requires_declared_edges_and_scales_fly_timing() -> None:
    route_payload = {
        "contract_name": "propertyquarry.matterport_continuous_route.v1",
        "status": "pass",
        "model_sid": "MODEL123",
        "cut_count": 0,
        "dissolve_count": 0,
        "teleport_count": 0,
        "walkable_room_ids": ["room-aaa", "room-bbb"],
        "route": [
            {
                "id": "sweep-001",
                "index": 0,
                "room_id": "room-aaa",
                "neighbors": ["sweep-002"],
                "position": {"x": 0, "y": 0, "z": 0},
            },
            {
                "id": "sweep-002",
                "index": 1,
                "room_id": "room-bbb",
                "neighbors": ["sweep-001"],
                "position": {"x": 4, "y": 0, "z": 0},
            },
        ],
    }

    contract = build_sdk_walkthrough_contract(route_payload)

    assert contract["status"] == "pass"
    assert contract["transition"] == "fly"
    assert contract["start_ss"] == 1
    assert contract["route_node_count"] == 2
    assert contract["route_edge_count"] == 1
    assert contract["route_distance_m"] == 4.0
    assert contract["missing_room_ids"] == []
    assert contract["route"][0]["transition_time_ms"] == 1200
    assert contract["route"][1]["transition_time_ms"] == 1800

    route_payload["route"][0]["neighbors"] = []
    try:
        build_sdk_walkthrough_contract(route_payload)
    except RuntimeError as error:
        assert str(error) == "matterport_route_edge_not_declared"
    else:
        raise AssertionError("undeclared route edge was accepted")


def test_model_publication_materializer_requires_connected_available_sweeps(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "topology.json"
    valid_until = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat().replace(
        "+00:00", "Z"
    )
    topology = {
        "model_sid": "MODEL123",
        "locations": [
            {
                "id": "sweep-001",
                "model": {"id": "MODEL123"},
                "neighbors": ["sweep-002"],
                "room": {"id": "room-aaa"},
                "pano": {
                    "skyboxes": [
                        {"status": "available", "validUntil": valid_until}
                    ]
                },
            },
            {
                "id": "sweep-002",
                "model": {"id": "MODEL123"},
                "neighbors": ["sweep-001", "sweep-003"],
                "room": {"id": "room-bbb"},
                "pano": {
                    "skyboxes": [
                        {"status": "available", "validUntil": valid_until}
                    ]
                },
            },
            {
                "id": "sweep-003",
                "model": {"id": "MODEL123"},
                "neighbors": ["sweep-002"],
                "room": {"id": "room-bbb"},
                "pano": {
                    "skyboxes": [
                        {"status": "available", "validUntil": valid_until}
                    ]
                },
            },
        ],
    }
    source_path.write_text(json.dumps(topology), encoding="utf-8")

    observed_at = datetime.now(timezone.utc).replace(microsecond=0)
    contract = build_publication_contract(
        topology,
        source_path=source_path,
        checked_at=observed_at.isoformat().replace("+00:00", "Z"),
    )

    assert contract["status"] == "pass"
    assert contract["model_available"] is True
    assert contract["enabled_sweep_count"] == 3
    assert contract["connected_component_count"] == 1
    assert contract["room_count"] == 2
    assert contract["navigation_edge_count"] == 2
    assert contract["source_sha256"]
    assert datetime.fromisoformat(
        str(contract["proof_valid_until"]).replace("Z", "+00:00")
    ) == observed_at + timedelta(days=30)

    topology["locations"][0]["neighbors"] = []
    topology["locations"][1]["neighbors"] = []
    topology["locations"][2]["neighbors"] = []
    try:
        build_publication_contract(
            topology,
            source_path=source_path,
            checked_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
    except RuntimeError as error:
        assert str(error) == "matterport_publication_topology_disconnected"
    else:
        raise AssertionError("disconnected publication topology was accepted")
