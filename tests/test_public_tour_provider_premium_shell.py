from ea.app.api.routes import public_tours
from ea.app.api.routes.public_tours import (
    _tour_control_3dvista_vr_href,
    _tour_control_3dvista_html,
    _tour_control_external_iframe_html,
    _tour_control_media_context,
)


def _layout_payload() -> dict[str, object]:
    return {
        "slug": "karl-czerny-gasse-2-urban-jungle",
        "three_d_vista_entry_relpath": "3dvista/index.htm",
    }


def test_provider_control_does_not_release_unaccepted_layout_assets() -> None:
    scenes, video_url, video_mime_type = _tour_control_media_context(_layout_payload())

    assert video_url == ""
    assert video_mime_type == "video/mp4"
    assert scenes == []


def test_provider_control_shell_is_fuller_height_and_truthfully_labels_layout_media(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        public_tours,
        "_tour_control_media_context",
        lambda payload: ([], "/tours/karl-czerny-gasse-2-urban-jungle/walkthrough", "video/mp4"),
    )
    rendered = _tour_control_external_iframe_html(
        title="Karl-Czerny-Gasse 2",
        iframe_src="/tours/3dvista/karl-czerny-gasse-2-urban-jungle/3dvista/index.htm",
        badge="3D Tour",
        payload=_layout_payload(),
        nonce="test-nonce",
    )

    assert "Floorplan available inside the 3D tour." in rendered
    assert "Use <strong>Floorplan</strong> inside the 3D tour" in rendered
    assert "Photos and floorplans are not attached yet." not in rendered
    assert "height: min(82dvh, 920px); min-height: 620px" in rendered
    assert "View with 3D glasses" not in rendered


def test_3dvista_vr_is_optional_and_normal_camera_walkthrough_stays_default(
    monkeypatch,
) -> None:
    iframe_src = "/tours/3dvista/karl-czerny-gasse-2-urban-jungle/3dvista/index.htm"
    monkeypatch.setattr(
        public_tours,
        "_tour_control_media_context",
        lambda payload: ([], "/tours/karl-czerny-gasse-2-urban-jungle/walkthrough", "video/mp4"),
    )

    rendered = _tour_control_external_iframe_html(
        title="Karl-Czerny-Gasse 2",
        iframe_src=iframe_src,
        badge="3D Tour",
        payload=_layout_payload(),
        nonce="test-nonce",
        vr_href=_tour_control_3dvista_vr_href(iframe_src),
    )

    assert 'href="/tours/karl-czerny-gasse-2-urban-jungle/walkthrough"' in rendered
    assert 'data-tour-mode="vr"' in rendered
    assert f'href="{iframe_src}?vr"' in rendered
    assert "View with 3D glasses" in rendered


def test_3dvista_vr_href_preserves_provider_query_and_rejects_other_providers() -> None:
    assert (
        _tour_control_3dvista_vr_href(
            "/tours/3dvista/karl/3dvista/index.htm?language=en-US#hall"
        )
        == "/tours/3dvista/karl/3dvista/index.htm?language=en-US&vr#hall"
    )
    assert _tour_control_3dvista_vr_href("https://example.crezlo.com/tour/123") == ""


def test_verified_3dvista_control_exposes_optional_glasses_mode(monkeypatch) -> None:
    monkeypatch.setattr(
        public_tours,
        "_3dvista_private_viewer_proof_ready",
        lambda payload, *, slug: True,
    )
    monkeypatch.setattr(
        public_tours,
        "_3dvista_entry_export_ready",
        lambda slug, payload, entry_relpath: True,
    )
    monkeypatch.setattr(
        public_tours,
        "_tour_control_media_context",
        lambda payload: ([], "/tours/karl-czerny-gasse-2-urban-jungle/walkthrough", "video/mp4"),
    )

    rendered = _tour_control_3dvista_html(_layout_payload(), nonce="test-nonce")

    assert "Open walkthrough" in rendered
    assert "View with 3D glasses" in rendered
    assert (
        'href="/tours/3dvista/karl-czerny-gasse-2-urban-jungle/3dvista/index.htm?vr"'
        in rendered
    )
