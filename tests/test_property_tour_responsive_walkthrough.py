from pathlib import Path

from app.api.routes.public_tour_payloads import (
    public_tour_asset_metadata,
    public_tour_collect_asset_refs,
    redacted_public_tour_payload,
)
from app.api.routes.public_tours import (
    _PUBLIC_TOUR_RUNTIME_ACCEPTANCE_TOKEN,
    _public_tour_walkthrough_source_markup,
    _tour_html,
)


def _payload() -> dict[str, object]:
    return {
        "slug": "danube-flats",
        "title": "Danube Flats",
        "video_relpath": "walkthrough-desktop-1080p60.mp4",
        "video_mobile_relpath": "walkthrough-mobile-720p60.mp4",
        "scenes": [],
    }


def _accepted_payload() -> dict[str, object]:
    return {
        **_payload(),
        "_walkthrough_runtime_acceptance": {
            "allowed": True,
            "declared": True,
            "status": "accepted",
        },
        "_walkthrough_runtime_acceptance_token": (
            _PUBLIC_TOUR_RUNTIME_ACCEPTANCE_TOKEN
        ),
    }


def test_responsive_walkthrough_sources_put_mobile_60fps_first() -> None:
    markup = _public_tour_walkthrough_source_markup(
        _accepted_payload(),
        video_url="/tours/danube-flats/walkthrough",
        video_mime_type="video/mp4",
    )

    mobile_source = (
        '<source src="/tours/files/danube-flats/walkthrough-mobile-720p60.mp4" '
        'type="video/mp4" media="(max-width: 760px)">'
    )
    desktop_source = '<source src="/tours/danube-flats/walkthrough" type="video/mp4">'
    assert mobile_source in markup
    assert desktop_source in markup
    assert markup.index(mobile_source) < markup.index(desktop_source)


def test_mobile_walkthrough_is_a_manifest_safe_public_video_asset() -> None:
    payload = _payload()

    assert public_tour_collect_asset_refs(payload) == {
        "walkthrough-desktop-1080p60.mp4",
        "walkthrough-mobile-720p60.mp4",
    }
    metadata = public_tour_asset_metadata(payload)
    assert metadata["walkthrough-desktop-1080p60.mp4"]["role"] == "video"
    assert metadata["walkthrough-mobile-720p60.mp4"]["role"] == "video_mobile"


def test_public_payload_exposes_responsive_walkthrough_relpaths_without_private_data(tmp_path: Path) -> None:
    rendered = redacted_public_tour_payload(
        _payload(),
        expose_asset_relpaths=True,
        url_allowed=lambda _url: False,
        bundle_dir_resolver=lambda _slug: tmp_path,
    )

    assert rendered["video_relpath"] == "walkthrough-desktop-1080p60.mp4"
    assert rendered["video_mobile_relpath"] == "walkthrough-mobile-720p60.mp4"


def test_provider_neutral_rendered_tour_page_embeds_mobile_then_desktop_walkthrough_sources() -> None:
    payload = {
        **_payload(),
        "display_title": "Danube Flats",
        "facts": {},
        "scenes": [
            {
                "name": "Living room",
                "role": "photo",
                "asset_relpath": "living-room.jpg",
                "mime_type": "image/jpeg",
            }
        ],
    }

    rendered = _tour_html(
        payload,
        hostname="propertyquarry.com",
        path="/tours/danube-flats",
        walkthrough_acceptance={
            "allowed": True,
            "declared": True,
            "status": "accepted",
        },
    )

    mobile_url = "/tours/files/danube-flats/walkthrough-mobile-720p60.mp4"
    desktop_url = "/tours/danube-flats/walkthrough"
    mobile_source = f'<source src="{mobile_url}"'
    desktop_source = f'<source src="{desktop_url}"'
    assert rendered.count("<source") == 2
    assert rendered.index(mobile_source) < rendered.index(desktop_source)
    assert 'media="(max-width: 760px)"' in rendered
