from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.routes.public_tours import (
    _public_tour_walkthrough_acceptance,
    _public_tour_walkthrough_media_context,
    _public_tour_walkthrough_source_markup,
    _without_disqualified_walkthrough,
    public_tour_file,
    public_tour_walkthrough,
)


def _write_bundle(
    root: Path,
    *,
    acceptance_status: str | None,
    launch_eligible: bool | None,
    write_sidecar: bool = True,
) -> dict[str, object]:
    slug = "disqualification-tour"
    bundle = root / slug
    bundle.mkdir(parents=True)
    (bundle / "walkthrough-desktop.mp4").write_bytes(b"desktop-video")
    (bundle / "walkthrough-mobile.mp4").write_bytes(b"mobile-video")
    payload: dict[str, object] = {
        "slug": slug,
        "title": "Disqualification Tour",
        "video_relpath": "walkthrough-desktop.mp4",
        "video_mobile_relpath": "walkthrough-mobile.mp4",
        "flythrough_video_relpath": "walkthrough-desktop.mp4",
        "video_sidecar_relpath": "tour.magicfit.json",
        "video_provider": "magicfit",
        "video_provider_key": "magicfit",
        "generated_reconstruction": {
            "walkthrough_video_relpath": "generated-reconstruction/generated-walkthrough.mp4",
            "walkthrough_sidecar_relpath": "generated-reconstruction/generated-walkthrough.quality.json",
        },
        "scenes": [],
    }
    (bundle / "tour.json").write_text(json.dumps(payload), encoding="utf-8")
    if write_sidecar:
        sidecar: dict[str, object] = {
            "provider_key": "magicfit",
            "video_relpath": "walkthrough-desktop.mp4",
        }
        if acceptance_status is not None:
            sidecar["acceptance_status"] = acceptance_status
        if launch_eligible is not None:
            sidecar["launch_eligible"] = launch_eligible
        (bundle / "tour.magicfit.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return payload


def _write_generated_reconstruction_bundle(
    root: Path,
    *,
    acceptance_status: str | None,
    launch_eligible: bool | None,
    write_sidecar: bool = True,
) -> dict[str, object]:
    slug = "generated-reconstruction-tour"
    bundle = root / slug
    generated = bundle / "generated-reconstruction"
    generated.mkdir(parents=True)
    (generated / "generated-walkthrough.mp4").write_bytes(b"generated-video")
    payload: dict[str, object] = {
        "slug": slug,
        "title": "Generated Reconstruction Tour",
        "generated_reconstruction": {
            "walkthrough_video_relpath": (
                "generated-reconstruction/generated-walkthrough.mp4"
            ),
            "walkthrough_sidecar_relpath": (
                "generated-reconstruction/generated-walkthrough.quality.json"
            ),
        },
        "scenes": [],
    }
    (bundle / "tour.json").write_text(json.dumps(payload), encoding="utf-8")
    if write_sidecar:
        sidecar: dict[str, object] = {
            "provider_key": "propertyquarry_generated_reconstruction",
        }
        if acceptance_status is not None:
            sidecar["acceptance_status"] = acceptance_status
        if launch_eligible is not None:
            sidecar["launch_eligible"] = launch_eligible
        (generated / "generated-walkthrough.quality.json").write_text(
            json.dumps(sidecar),
            encoding="utf-8",
        )
    return payload


def test_disqualified_walkthrough_is_removed_from_every_public_media_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    payload = _write_bundle(
        tmp_path,
        acceptance_status="disqualified",
        launch_eligible=False,
    )

    acceptance = _public_tour_walkthrough_acceptance(payload)
    sanitized = _without_disqualified_walkthrough(payload)

    assert acceptance == {
        "allowed": False,
        "declared": True,
        "scope": "top_level",
        "asset_relpaths": ["walkthrough-desktop.mp4", "walkthrough-mobile.mp4"],
        "status": "magicfit_acceptance_invalid",
        "verified_video_relpath": "",
    }
    assert _public_tour_walkthrough_media_context(payload) == ("", "video/mp4")
    assert (
        _public_tour_walkthrough_source_markup(
            payload,
            video_url="",
            video_mime_type="video/mp4",
        )
        == ""
    )
    assert sanitized["_walkthrough_media_suppressed"] is True
    assert "video_relpath" not in sanitized
    assert "video_mobile_relpath" not in sanitized
    assert "video_provider" not in sanitized


@pytest.mark.parametrize(
    ("acceptance_status", "launch_eligible", "write_sidecar", "expected_status"),
    (
        (None, None, True, "unreviewed"),
        ("pending", True, True, "pending"),
        ("accepted", None, True, "accepted"),
        (None, None, False, "sidecar_unavailable"),
    ),
)
def test_unaccepted_generated_walkthrough_is_suppressed_everywhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    acceptance_status: str | None,
    launch_eligible: bool | None,
    write_sidecar: bool,
    expected_status: str,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    payload = _write_generated_reconstruction_bundle(
        tmp_path,
        acceptance_status=acceptance_status,
        launch_eligible=launch_eligible,
        write_sidecar=write_sidecar,
    )

    acceptance = _public_tour_walkthrough_acceptance(payload)

    assert acceptance["allowed"] is False
    assert acceptance["status"] == expected_status
    assert _public_tour_walkthrough_media_context(payload) == ("", "video/mp4")
    with pytest.raises(HTTPException) as error:
        public_tour_walkthrough(str(payload["slug"]))
    assert error.value.status_code == 404
    assert error.value.detail == "tour_walkthrough_unavailable"

    response = public_tour_file(
        str(payload["slug"]),
        "generated-reconstruction/generated-walkthrough.mp4",
        None,  # type: ignore[arg-type]
    )
    assert response.status_code == 410
    assert response.headers["cache-control"] == "no-store"


def test_explicitly_accepted_generated_walkthrough_remains_public(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    payload = _write_generated_reconstruction_bundle(
        tmp_path,
        acceptance_status="accepted",
        launch_eligible=True,
    )

    acceptance = _public_tour_walkthrough_acceptance(payload)

    assert acceptance["allowed"] is True
    assert acceptance["status"] == "accepted"
    assert _public_tour_walkthrough_media_context(payload) == (
        f"/tours/{payload['slug']}/walkthrough",
        "video/mp4",
    )
    response = public_tour_walkthrough(str(payload["slug"]))
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_disqualified_walkthrough_routes_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    payload = _write_bundle(
        tmp_path,
        acceptance_status="disqualified",
        launch_eligible=False,
    )
    slug = str(payload["slug"])

    with pytest.raises(HTTPException) as error:
        public_tour_walkthrough(slug)
    assert error.value.status_code == 404
    assert error.value.detail == "tour_walkthrough_unavailable"

    response = public_tour_file(slug, "walkthrough-desktop.mp4", None)  # type: ignore[arg-type]
    assert response.status_code == 410
    assert response.headers["cache-control"] == "no-store"

    mobile_response = public_tour_file(slug, "walkthrough-mobile.mp4", None)  # type: ignore[arg-type]
    assert mobile_response.status_code == 410
    assert mobile_response.headers["cache-control"] == "no-store"


def test_declared_magicfit_requires_exact_v4_not_status_only_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(tmp_path))
    missing_payload = _write_bundle(
        tmp_path,
        acceptance_status=None,
        launch_eligible=None,
        write_sidecar=False,
    )

    assert _public_tour_walkthrough_acceptance(missing_payload)["status"] == (
        "magicfit_acceptance_invalid"
    )
    assert _public_tour_walkthrough_media_context(missing_payload) == ("", "video/mp4")

    sidecar_path = tmp_path / str(missing_payload["slug"]) / "tour.magicfit.json"
    sidecar_path.write_text(
        json.dumps(
            {
                "provider_key": "magicfit",
                "video_relpath": "walkthrough-desktop.mp4",
                "acceptance_status": "accepted",
                "launch_eligible": True,
            }
        ),
        encoding="utf-8",
    )

    shallow = _public_tour_walkthrough_acceptance(missing_payload)
    assert shallow["allowed"] is False
    assert shallow["status"] == "magicfit_acceptance_invalid"
    assert _public_tour_walkthrough_media_context(missing_payload) == (
        "",
        "video/mp4",
    )
