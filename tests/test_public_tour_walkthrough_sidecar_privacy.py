from pathlib import Path

from app.api.routes.public_tour_payloads import (
    build_public_tour_manifest,
    canonical_public_tour_payload,
)


def _payload(sidecar_key: str, sidecar_relpath: str) -> dict[str, object]:
    return {
        "slug": "walkthrough-sidecar-fixture",
        "title": "Walkthrough sidecar fixture",
        "publication_status": "published",
        "tour_privacy_mode": "anonymous_public",
        sidecar_key: sidecar_relpath,
    }


def test_canonical_manifest_retains_safe_walkthrough_sidecar_reference(
    tmp_path: Path,
) -> None:
    payload = _payload("video_sidecar_relpath", "tour.walkthrough.json")

    canonical = canonical_public_tour_payload(payload, bundle_dir=tmp_path)

    assert canonical["video_sidecar_relpath"] == "tour.walkthrough.json"
    assert canonical_public_tour_payload(canonical, bundle_dir=tmp_path) == canonical


def test_public_payload_hides_walkthrough_sidecar_reference(tmp_path: Path) -> None:
    payload = _payload("walkthrough_sidecar_relpath", "review/walkthrough.json")

    public = build_public_tour_manifest(
        payload,
        expose_asset_relpaths=False,
        url_allowed=lambda _url: False,
        bundle_dir_resolver=lambda _slug: tmp_path,
    ).as_dict()

    assert "walkthrough_sidecar_relpath" not in public


def test_canonical_manifest_drops_unsafe_walkthrough_sidecar_reference(
    tmp_path: Path,
) -> None:
    canonical = canonical_public_tour_payload(
        _payload("video_sidecar_relpath", "../tour.private.json"),
        bundle_dir=tmp_path,
    )

    assert "video_sidecar_relpath" not in canonical
