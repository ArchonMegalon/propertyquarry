from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.api.routes import landing_property_workspace_payload as workspace_payload
from app.api.routes import product_api_delivery
from app.product import property_tour_hosting


def _matterport_payload() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "matterport_url": "https://my.matterport.com/show/?m=MODEL123",
        "matterport_model_publication": {
            "contract_name": "propertyquarry.matterport_model_publication.v1",
            "status": "pass",
            "model_sid": "MODEL123",
            "model_available": True,
            "checked_at": (now - timedelta(minutes=5)).isoformat(),
            "asset_valid_until": (now + timedelta(hours=12)).isoformat(),
            "proof_valid_until": (now + timedelta(hours=12)).isoformat(),
            "enabled_sweep_count": 23,
            "available_sweep_count": 23,
            "connected_component_count": 1,
            "room_count": 6,
            "navigation_edge_count": 49,
            "source_sha256": "a" * 64,
        },
    }


def test_verified_matterport_uses_first_party_control_route(monkeypatch) -> None:
    payload = _matterport_payload()
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_payload_for_url",
        lambda _url, *, principal_id="": payload,
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_has_3dvista_export",
        lambda _url, *, principal_id="": False,
    )

    assert (
        property_tour_hosting._hosted_property_tour_verified_provider(
            "/tours/sdk-loft"
        )
        == "matterport"
    )
    assert (
        property_tour_hosting._hosted_property_tour_verified_open_url(
            "/tours/sdk-loft"
        )
        == "/tours/sdk-loft/control/matterport"
    )


def test_verified_3dvista_uses_first_party_control_route(monkeypatch) -> None:
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_payload_for_url",
        lambda _url, *, principal_id="": {"slug": "vista-loft"},
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_has_3dvista_export",
        lambda _url, *, principal_id="": True,
    )

    assert (
        property_tour_hosting._hosted_property_tour_verified_provider(
            "/tours/vista-loft"
        )
        == "3dvista"
    )
    assert (
        property_tour_hosting._hosted_property_tour_verified_open_url(
            "/tours/vista-loft"
        )
        == "/tours/vista-loft/control/3dvista"
    )


def test_matterport_fails_closed_without_current_connected_multi_room_proof(
    monkeypatch,
) -> None:
    payload = _matterport_payload()
    publication = dict(payload["matterport_model_publication"])
    publication["checked_at"] = (
        datetime.now(timezone.utc) - timedelta(days=31)
    ).isoformat()
    payload["matterport_model_publication"] = publication
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_payload_for_url",
        lambda _url, *, principal_id="": payload,
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_has_3dvista_export",
        lambda _url, *, principal_id="": False,
    )

    assert property_tour_hosting._hosted_property_tour_verified_provider(
        "/tours/sdk-loft"
    ) == ""

    publication["checked_at"] = datetime.now(timezone.utc).isoformat()
    publication["room_count"] = 1
    assert property_tour_hosting._hosted_property_tour_verified_provider(
        "/tours/sdk-loft"
    ) == ""


def test_workspace_rejects_raw_provider_url_and_projects_verified_hosted_tour(
    monkeypatch,
) -> None:
    raw_provider = "https://my.matterport.com/show/?m=MODEL123"
    assert workspace_payload._property_workbench_candidate_ready_tour_url(
        {"tour_url": raw_provider}
    ) == ""

    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_verified_provider",
        lambda _url, *, principal_id="": "matterport",
    )
    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_verified_open_url",
        lambda _url, *, principal_id="": "/tours/sdk-loft/control/matterport",
    )
    assert workspace_payload._property_workbench_candidate_ready_tour_url(
        {"tour_url": "/tours/sdk-loft", "source_virtual_tour_url": raw_provider}
    ) == "/tours/sdk-loft/control/matterport"


def test_camera_walkthrough_does_not_depend_on_optional_spatial_tour(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def _resolve(
        source_url: object,
        _walkthrough_url: object = "",
        *,
        principal_id: object = "",
    ) -> str:
        del principal_id
        source = str(source_url or "")
        calls.append(source)
        if source == "/tours/camera-only":
            return "/tours/camera-only/walkthrough"
        return ""

    monkeypatch.setattr(
        property_tour_hosting,
        "_hosted_property_tour_walkthrough_open_url",
        _resolve,
    )

    result = workspace_payload._property_workbench_candidate_flythrough_url(
        {
            "tour_url": "/tours/camera-only",
            "flythrough_url": "https://cdn.example.test/loose-video.mp4",
        },
        ready_tour_url="",
    )

    assert result == "/tours/camera-only/walkthrough"
    assert "/tours/camera-only" in calls


def test_client_payload_separates_default_camera_video_from_optional_3d_tour(
    monkeypatch,
) -> None:
    raw_provider = "https://my.matterport.com/show/?m=MODEL123"
    monkeypatch.setattr(
        workspace_payload,
        "_property_workbench_candidate_ready_tour_url",
        lambda _candidate, *, principal_id="": "/tours/sdk-loft/control/matterport",
    )
    monkeypatch.setattr(
        workspace_payload,
        "_property_workbench_candidate_generated_layout_url",
        lambda _candidate: "",
    )
    monkeypatch.setattr(
        workspace_payload,
        "_property_workbench_candidate_flythrough_url",
        lambda _candidate, *, ready_tour_url, principal_id="": "/tours/sdk-loft/walkthrough",
    )

    result = workspace_payload._property_workbench_client_candidate_payload(
        {
            "tour": {
                "status": "ready",
                "provider_url": raw_provider,
            },
            "source_virtual_tour_url": raw_provider,
        }
    )

    assert result["flythrough"]["url"] == "/tours/sdk-loft/walkthrough"
    assert result["flythrough"]["media_kind"] == "camera_walkthrough"
    assert result["flythrough"]["label"] == "Camera walkthrough available"
    assert result["tour"]["url"] == "/tours/sdk-loft/control/matterport"
    assert result["tour"]["label"] == "3D tour available"
    assert "provider_url" not in result["tour"]
    assert "source_virtual_tour_url" not in result


def test_client_payload_promotes_safe_remote_listing_thumbnail() -> None:
    thumbnail_url = "https://cache.willhaben.at/mmo/8/1234567898.jpg"

    result = workspace_payload._property_workbench_client_candidate_payload(
        {"thumbnail_url": thumbnail_url}
    )

    assert result["preview_image_url"] == thumbnail_url


def test_client_payload_keeps_transformed_cdn_listing_thumbnail() -> None:
    thumbnail_url = (
        "https://i.prod.mp-dst.onyx60.com/plain/immoimporte/justimmo2/"
        "storage.justimmo.at/thumb/abc123/fm_h1080_w1920/photo-1.jpg/~/token/"
        "format:jpg/background:ffffff/rs:fill:920:613:1"
    )

    result = workspace_payload._property_workbench_client_candidate_payload(
        {"thumbnail_url": thumbnail_url}
    )

    assert result["preview_image_url"] == thumbnail_url


def test_client_payload_keeps_query_formatted_cdn_listing_thumbnail() -> None:
    thumbnail_url = "https://images.example.test/render/asset?width=640&format=webp"

    result = workspace_payload._property_workbench_client_candidate_payload(
        {"thumbnail_url": thumbnail_url}
    )

    assert result["preview_image_url"] == thumbnail_url


def test_client_payload_rejects_provider_chrome_and_promotes_real_media() -> None:
    real_media_url = "https://cache.willhaben.at/mmo/8/1234567899.jpg"

    for provider_chrome_url in (
        "https://cache.willhaben.at/img/upselling/icon-bump.png",
        "https://www.immobilienscout24.at/expose/assets/plus-insider-locked.77b21addeee8c430a19b.webp",
    ):
        result = workspace_payload._property_workbench_client_candidate_payload(
            {
                "thumbnail_url": provider_chrome_url,
                "property_facts": {
                    "media_urls_json": [provider_chrome_url, real_media_url],
                },
            }
        )

        assert result["preview_image_url"] == real_media_url
        assert provider_chrome_url not in result.get(
            "preview_image_fallback_urls", []
        )


def test_client_payload_uses_honest_empty_state_for_provider_chrome_only() -> None:
    result = workspace_payload._property_workbench_client_candidate_payload(
        {
            "thumbnail_url": (
                "https://www.immobilienscout24.at/expose/assets/"
                "plus-insider-locked.77b21addeee8c430a19b.webp"
            )
        }
    )

    assert "preview_image_url" not in result


def test_client_payload_keeps_a_bounded_safe_thumbnail_fallback() -> None:
    primary_url = "https://cache.willhaben.at/mmo/8/1234567898.jpg"
    tracking_url = "https://api.willhaben.at/restapi/v2/listings/123.jpg"
    fallback_urls = [
        f"https://cache.willhaben.at/mmo/8/123456789{index}.jpg"
        for index in range(9, 14)
    ]

    result = workspace_payload._property_workbench_client_candidate_payload(
        {
            "thumbnail_url": primary_url,
            "property_facts": {
                "media_urls_json": [primary_url, tracking_url, *fallback_urls],
            },
        }
    )

    assert result["preview_image_url"] == primary_url
    assert result["preview_image_fallback_url"] == fallback_urls[0]
    assert result["preview_image_fallback_urls"] == fallback_urls[:4]


def test_client_payload_rejects_tracking_thumbnail_fallback() -> None:
    result = workspace_payload._property_workbench_client_candidate_payload(
        {"thumbnail_url": "https://api.willhaben.at/restapi/v2/listings/123.jpg"}
    )

    assert "preview_image_url" not in result


def test_client_payload_promotes_safe_media_when_primary_thumbnail_is_tracking() -> None:
    tracking_url = "https://api.willhaben.at/restapi/v2/listings/123.jpg"
    safe_media_url = "https://cache.willhaben.at/mmo/8/1234567899.jpg"

    result = workspace_payload._property_workbench_client_candidate_payload(
        {
            "preview_image_url": tracking_url,
            "property_facts": {
                "media_urls_json": [tracking_url, safe_media_url],
            },
        }
    )

    assert result["preview_image_url"] == safe_media_url
    assert "preview_image_fallback_url" not in result
    assert "preview_image_fallback_urls" not in result


def test_client_payload_keeps_first_party_dynamic_thumbnail() -> None:
    primary_url = "/app/api/property/map-preview/candidate-thumbnail"

    result = workspace_payload._property_workbench_client_candidate_payload(
        {"preview_image_url": primary_url}
    )

    assert result["preview_image_url"] == primary_url


def test_lightweight_status_payload_keeps_safe_thumbnail_fallback_chain() -> None:
    primary_url = "https://cache.willhaben.at/mmo/8/1234567898.jpg"
    fallback_urls = [
        "/app/api/property/map-previews/local.png",
        "https://cache.willhaben.at/mmo/8/1234567899.jpg",
        "https://cache.willhaben.at/mmo/8/1234567900.webp",
    ]

    result = product_api_delivery._property_search_lightweight_candidate_payload(
        {
            "candidate_ref": "candidate-thumbnail-chain",
            "preview_image_url": primary_url,
            "preview_image_fallback_urls": [
                primary_url,
                *fallback_urls,
                "https://api.willhaben.at/restapi/v2/listings/123.jpg",
            ],
        },
        run_id="run-thumbnail-chain",
        index=1,
    )

    assert result["preview_image_url"] == primary_url
    assert result["preview_image_fallback_url"] == fallback_urls[0]
    assert result["preview_image_fallback_urls"] == fallback_urls


def test_lightweight_status_payload_keeps_first_party_dynamic_thumbnail() -> None:
    primary_url = "/app/api/property/map-preview/candidate-thumbnail"

    result = product_api_delivery._property_search_lightweight_candidate_payload(
        {
            "candidate_ref": "candidate-dynamic-thumbnail",
            "preview_image_url": primary_url,
        },
        run_id="run-dynamic-thumbnail",
        index=1,
    )

    assert result["preview_image_url"] == primary_url


def test_shortlist_uses_area_preview_when_spatial_diorama_is_unavailable() -> None:
    results_template = (
        Path(__file__).resolve().parents[1]
        / "ea/app/templates/app/_property_results_list.html"
    ).read_text(encoding="utf-8")
    workbench_script = (
        Path(__file__).resolve().parents[1]
        / "ea/app/templates/app/_property_workbench_script.html"
    ).read_text(encoding="utf-8")

    assert "const shortlistPreviewUrl = dioramaPreviewUrl || previewUrl;" in workbench_script
    assert "const shortlistPreviewIsDiorama = Boolean(dioramaPreviewUrl);" in workbench_script
    assert "? 'Spatial diorama'" in workbench_script
    assert ": 'Area preview';" in workbench_script
    assert "Preview not available" in workbench_script
    assert "Diorama not ready" not in workbench_script
    assert "{% set shortlist_preview_url = diorama_preview_url or primary_preview_url %}" in results_template
    assert "{{ 'Spatial diorama' if diorama_preview_url else 'Area preview' }}" in results_template
    assert 'src="{{ shortlist_preview_url }}"' in results_template
    assert "Diorama not ready" not in results_template


def test_generated_reconstruction_is_not_projected_as_provider_3d_tour(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        workspace_payload,
        "_property_workbench_candidate_ready_tour_url",
        lambda _candidate, *, principal_id="": "",
    )
    monkeypatch.setattr(
        workspace_payload,
        "_property_workbench_candidate_generated_layout_url",
        lambda _candidate: "/tours/ai-layout/control/generated-reconstruction",
    )
    monkeypatch.setattr(
        workspace_payload,
        "_property_workbench_candidate_flythrough_url",
        lambda _candidate, *, ready_tour_url, principal_id="": "",
    )

    result = workspace_payload._property_workbench_client_candidate_payload(
        {
            "tour_status": "ready",
            "tour": {
                "status": "ready",
                "tour_media_mode": "generated_reconstruction",
                "label": "Open AI-generated 3D tour",
            },
        }
    )

    assert result["tour_url"] == ""
    assert result["generated_reconstruction_url"].endswith(
        "/control/generated-reconstruction"
    )
    assert "url" not in result["tour"]


def test_results_template_presents_camera_walkthrough_before_optional_3d_tour() -> None:
    template_root = Path(__file__).resolve().parents[1] / "ea/app/templates/app"
    template = (template_root / "_property_results_list.html").read_text(
        encoding="utf-8"
    )
    selected_review = (
        template_root / "_property_selected_review_panel.html"
    ).read_text(encoding="utf-8")
    workbench = (template_root / "property_decision_workbench.html").read_text(
        encoding="utf-8"
    )
    feedback_script = (
        template_root / "_property_workbench_feedback_script.html"
    ).read_text(encoding="utf-8")
    workbench_script = (
        template_root / "_property_workbench_script.html"
    ).read_text(encoding="utf-8")
    research_detail = (template_root / "property_research_detail.html").read_text(
        encoding="utf-8"
    )

    camera_link = template.index('aria-label="Camera walkthrough"')
    provider_link = template.index('aria-label="3D tour"')
    assert camera_link < provider_link
    assert "Open the normal camera walkthrough." in template
    assert "Open the verified 3D tour." in template
    assert selected_review.index("Open camera walkthrough") < selected_review.index(
        "Open 3D tour"
    )
    assert "selected.get('open_tour_url')" not in selected_review
    assert "selected.get('open_tour_url')" not in workbench
    assert "candidate?.open_tour_url" not in feedback_script
    assert feedback_script.index("Open camera walkthrough") < feedback_script.index(
        "Open 3D tour"
    )
    assert "return 'Camera walkthrough';" in workbench_script
    assert 'data-pqx-thumbnail-fallback=' in template
    assert 'data-pqx-thumbnail-fallbacks=' in template
    assert 'data-pqx-shortlist-diorama\n            data-pqx-thumbnail' in template
    assert 'data-pqx-thumbnail-image' in template
    assert 'pqx-result-diorama-empty-label" aria-hidden="true" hidden' in template
    assert 'referrerpolicy="no-referrer"' in template
    assert "pqxThumbnailFallbackAttempted" in workbench_script
    assert "pqxThumbnailFallbackIndex" in workbench_script
    assert "preview_image_fallback_urls" in workbench_script
    assert 'referrerpolicy="no-referrer" data-pqx-thumbnail-image' in workbench_script
    assert "thumbnail.classList.add('is-recovering')" in workbench_script
    assert "thumbnail.classList.add('is-unavailable')" in workbench_script
    assert "placeholder.hidden = false" in workbench_script
    assert "eyebrow': 'AI layout preview'" in research_detail
    assert "visual_rail_label_display = 'AI-generated 3D tour'" not in research_detail
    assert research_detail.index("{% if visual_ready_walkthrough %}") < research_detail.index(
        "{% elif visual_ready_tour %}"
    )
    assert (
        "Cinematic walkthroughs and immersive 3D tours, crafted for this home."
        in research_detail
    )
