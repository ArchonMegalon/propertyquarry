from ea.app.api.routes import public_tours
from ea.app.api.routes.public_tours import (
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
