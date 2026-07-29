from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.api.routes import public_tours


def _browser_proof() -> dict[str, object]:
    return {
        "provider": "3dvista",
        "status": "pass",
        "rendered_viewer": True,
    }


def _matterport_publication() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "status": "pass",
        "model_sid": "CAPTURED123",
        "model_available": True,
        "checked_at": (now - timedelta(minutes=1)).isoformat(),
        "asset_valid_until": (now + timedelta(hours=6)).isoformat(),
        "proof_valid_until": (now + timedelta(hours=6)).isoformat(),
        "enabled_sweep_count": 23,
        "available_sweep_count": 23,
        "connected_component_count": 1,
        "room_count": 6,
        "navigation_edge_count": 49,
        "source_sha256": "a" * 64,
    }


def test_public_tour_csp_scopes_matterport_to_explicit_control_documents() -> None:
    csp = public_tours._public_tour_security_headers()["Content-Security-Policy"]

    assert "frame-src 'self' https://3dvista.com https://*.3dvista.com;" in csp
    assert "https://my.matterport.com" not in csp
    assert "https://*.matterport.com" not in csp
    assert "frame-src 'self' https:;" not in csp

    matterport_csp = public_tours._public_tour_security_headers(
        allow_matterport=True
    )["Content-Security-Policy"]
    assert (
        "frame-src 'self' https://3dvista.com https://*.3dvista.com "
        "https://my.matterport.com;"
    ) in matterport_csp


def test_public_live_360_cannot_reenable_matterport_via_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "PROPERTYQUARRY_PUBLIC_360_ALLOWED_HOSTS",
        "my.matterport.com,*.matterport.com,3dvista.com,*.3dvista.com",
    )

    assert public_tours._safe_live_360_url("https://my.matterport.com/show/?m=HISTORICAL") == ""
    assert public_tours._safe_live_360_url("https://demo.3dvista.com/tour/index.htm") == (
        "https://demo.3dvista.com/tour/index.htm"
    )


def test_provider_layers_drop_historical_matterport_but_keep_verified_3dvista() -> None:
    payload = {
        "slug": "verified-home",
        "three_d_vista_browser_render_proof": _browser_proof(),
        "tour_layers": [
            {
                "id": "historical-matterport",
                "provider": "matterport",
                "url": "https://my.matterport.com/show/?m=HISTORICAL",
            },
            {
                "id": "verified-3dvista",
                "provider": "3dvista",
                "url": "https://demo.3dvista.com/tour/index.htm",
            },
        ],
    }

    layers = public_tours._tour_control_provider_layers(
        payload=payload,
        default_src="/tours/3dvista/verified-home/3dvista/index.htm",
        default_label="3DVista Control",
    )
    serialized = str(layers).lower()

    assert "matterport" not in serialized
    assert "my.matterport.com" not in serialized
    assert any(layer["src"] == "https://demo.3dvista.com/tour/index.htm" for layer in layers)


def test_provider_layers_fail_closed_when_default_frame_is_matterport() -> None:
    assert public_tours._tour_control_provider_layers(
        payload={"slug": "historical-home"},
        default_src="https://my.matterport.com/show/?m=HISTORICAL",
        default_label="3D tour",
    ) == []


def test_provider_layers_allow_topology_verified_matterport_default() -> None:
    layers = public_tours._tour_control_provider_layers(
        payload={
            "slug": "captured-home",
            "matterport_url": "https://my.matterport.com/show/?m=CAPTURED123",
            "matterport_model_publication": _matterport_publication(),
        },
        default_src="https://my.matterport.com/show/?m=CAPTURED123",
        default_label="Captured 3D Tour",
    )

    assert [layer["src"] for layer in layers] == [
        "https://my.matterport.com/show/?m=CAPTURED123"
    ]


def test_walkable_3dvista_claim_requires_multiple_panorama_nodes(
    monkeypatch,
    tmp_path,
) -> None:
    bundle = tmp_path / "claimed-walkable"
    media = bundle / "3dvista" / "media"
    (media / "panorama_entry").mkdir(parents=True)
    payload = {
        "scene_strategy": "walkable_panorama",
        "creation_mode": "hosted_walkable_360",
    }
    monkeypatch.setattr(
        public_tours,
        "_tour_bundle_dir",
        lambda _slug: bundle,
    )

    assert (
        public_tours._3dvista_walkable_spatial_node_count(
            "claimed-walkable",
            payload,
            "3dvista/index.htm",
        )
        == 1
    )
    (media / "panorama_living").mkdir()
    assert (
        public_tours._3dvista_walkable_spatial_node_count(
            "claimed-walkable",
            payload,
            "3dvista/index.htm",
        )
        == 2
    )
