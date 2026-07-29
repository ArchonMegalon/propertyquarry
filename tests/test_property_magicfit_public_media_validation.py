from __future__ import annotations

import base64
from io import BytesIO
import json
import os
from pathlib import Path

import pytest
from PIL import Image

from app.api.routes import public_tours
from app.api.routes.public_tour_payloads import (
    public_tour_allowed_asset_paths,
    public_tour_file_url,
    public_tour_safe_asset_relpath,
)
from app.product.property_tour_hosting import _hosted_public_tour_asset_url
from scripts.property_magicfit_contact_sheet import (
    MagicFitContactSheetError,
    validate_magicfit_contact_sheet_bytes,
)
from scripts.property_magicfit_public_eligibility import (
    clear_magicfit_public_eligibility_cache,
)
from scripts.property_magicfit_reviewer_authority import (
    REVIEWER_AUTHORIZATION_ALGORITHM,
    REVIEWER_PUBLIC_KEY_CONTRACT,
    REVIEWER_TEST_OWNER_UID_ENV,
    REVIEWER_TRUST_STORE_ENV,
)
from scripts.verify_property_tour_controls import _local_video_asset_is_playable
from tests.product_test_helpers import build_product_client
from tests.test_property_tour_control_verifier import (
    _write_reproducible_magicfit_tour,
)
from tests.magicfit_test_support import provision_magicfit_reviewer_test_authority


def _image_bytes(image_format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (4, 3), color=(16, 32, 48)).save(
        output, format=image_format
    )
    return output.getvalue()


@pytest.mark.parametrize("image_format", ("PNG", "JPEG"))
def test_magicfit_contact_sheet_fully_decodes_real_bounded_image(
    image_format: str,
) -> None:
    body = _image_bytes(image_format)

    decoded = validate_magicfit_contact_sheet_bytes(body)

    assert decoded.format == image_format
    assert (decoded.width, decoded.height) == (4, 3)
    assert decoded.pixels == 12
    assert decoded.size_bytes == len(body)


@pytest.mark.parametrize(
    "body",
    (
        b"\x89PNG\r\n\x1a\nheader-only",
        b"\xff\xd8\xffheader-only",
        b"not-an-image",
        _image_bytes("PNG")[:-8],
        _image_bytes("JPEG")[:-2],
    ),
)
def test_magicfit_contact_sheet_rejects_header_only_malformed_and_truncated(
    body: bytes,
) -> None:
    with pytest.raises(MagicFitContactSheetError):
        validate_magicfit_contact_sheet_bytes(body)


def test_magicfit_contact_sheet_rejects_byte_and_pixel_bounds() -> None:
    body = _image_bytes()

    with pytest.raises(MagicFitContactSheetError):
        validate_magicfit_contact_sheet_bytes(body, maximum_bytes=len(body) - 1)
    with pytest.raises(MagicFitContactSheetError):
        validate_magicfit_contact_sheet_bytes(body, maximum_pixels=11)


def test_sparse_large_video_signature_check_reads_no_full_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "sparse-video"
    bundle.mkdir()
    video = bundle / "walkthrough.mp4"
    with video.open("wb") as handle:
        handle.write(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom")
        handle.truncate(512 * 1024 * 1024)

    def _full_read_forbidden(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("full video read is forbidden")

    monkeypatch.setattr(Path, "read_bytes", _full_read_forbidden)
    monkeypatch.setattr(
        "scripts.verify_property_tour_controls._ffprobe_video_markers",
        lambda _path: {"ffprobe_available": False},
    )

    assert _local_video_asset_is_playable(bundle, "walkthrough.mp4") is True


def _public_media_client(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    principal_id: str,
):
    reviewer_authority = provision_magicfit_reviewer_test_authority(
        root.parent / f".{root.name}-reviewer-trust",
        public_tour_root=root,
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(root))
    monkeypatch.setenv("PROPERTYQUARRY_ENABLE_PUBLIC_TOURS", "1")
    monkeypatch.setenv(
        REVIEWER_TRUST_STORE_ENV, str(reviewer_authority.trust_store_path)
    )
    monkeypatch.setenv(REVIEWER_TEST_OWNER_UID_ENV, str(os.geteuid()))
    clear_magicfit_public_eligibility_cache()
    return build_product_client(principal_id=principal_id)


def _accepted_video_bytes() -> bytes:
    return b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isomexact-v4-video"


def test_exact_accepted_v4_is_the_only_magicfit_public_video_subject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "accepted-v3-public-media"
    expected = _accepted_video_bytes()
    accepted = _write_reproducible_magicfit_tour(
        tmp_path,
        slug,
        expected,
        include_scene=True,
    )
    video_relpath = str(accepted["video_relpath"])
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="accepted-v3-public-media",
    )

    landing = client.get(f"/tours/{slug}")
    walkthrough = client.get(f"/tours/{slug}/walkthrough")
    direct = client.get(f"/tours/files/{slug}/{video_relpath}")

    assert landing.status_code == 200
    assert f"/tours/{slug}/walkthrough" in landing.text
    assert walkthrough.status_code == 200
    assert walkthrough.content == expected
    assert direct.status_code == 200
    assert direct.content == expected
    assert walkthrough.headers["cache-control"] == "no-store"
    assert direct.headers["cache-control"] == "no-store"


def test_warm_magicfit_public_cache_observes_reviewer_revocation_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "accepted-v4-revoked-after-cache"
    expected = _accepted_video_bytes()
    accepted = _write_reproducible_magicfit_tour(tmp_path, slug, expected)
    video_relpath = str(accepted["video_relpath"])
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="accepted-v4-revoked-after-cache",
    )
    route = f"/tours/files/{slug}/{video_relpath}"

    assert client.get(route).status_code == 200
    assert client.get(route).status_code == 200
    authority = provision_magicfit_reviewer_test_authority(
        tmp_path.parent / f".{tmp_path.name}-reviewer-trust",
        public_tour_root=tmp_path,
    )
    authority.revoke()

    revoked = client.get(route)
    revoked_again = client.get(route)

    assert revoked.status_code == 410
    assert revoked_again.status_code == 410
    assert revoked.content != expected
    authority.unrevoke()

    restored = client.get(route)

    assert restored.status_code == 200
    assert restored.content == expected


def test_warm_magicfit_public_cache_fails_closed_when_trust_config_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "accepted-v4-trust-config-removed"
    expected = _accepted_video_bytes()
    accepted = _write_reproducible_magicfit_tour(tmp_path, slug, expected)
    route = f"/tours/files/{slug}/{accepted['video_relpath']}"
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="accepted-v4-trust-config-removed",
    )
    assert client.get(route).status_code == 200
    assert client.get(route).status_code == 200

    monkeypatch.delenv(REVIEWER_TRUST_STORE_ENV)
    unavailable = client.get(route)

    assert unavailable.status_code == 410
    assert unavailable.content != expected


def test_warm_magicfit_public_cache_rejects_authorization_audit_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "accepted-v4-authorization-audit-mutated"
    expected = _accepted_video_bytes()
    accepted = _write_reproducible_magicfit_tour(tmp_path, slug, expected)
    route = f"/tours/files/{slug}/{accepted['video_relpath']}"
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="accepted-v4-authorization-audit-mutated",
    )
    assert client.get(route).status_code == 200
    sidecar_path = (
        tmp_path
        / slug
        / ".magicfit-deliveries"
        / f"{accepted['delivery_digest']}.json"
    )
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    authority_relpath = sidecar["audit"]["artifacts"]["reviewer_authority"][
        "relpath"
    ]
    authority_path = tmp_path / slug / str(authority_relpath)
    authority_path.write_bytes(authority_path.read_bytes() + b"\n")

    rejected = client.get(route)

    assert rejected.status_code == 410
    assert rejected.content != expected


def test_magicfit_public_eligibility_rejects_writable_accepted_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "accepted-v4-writable-media"
    expected = _accepted_video_bytes()
    accepted = _write_reproducible_magicfit_tour(tmp_path, slug, expected)
    video_path = tmp_path / slug / str(accepted["video_relpath"])
    assert video_path.stat().st_mode & 0o777 == 0o444
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="accepted-v4-writable-media",
    )
    video_path.chmod(0o644)

    rejected = client.get(
        f"/tours/files/{slug}/{accepted['video_relpath']}"
    )

    assert rejected.status_code == 410
    assert rejected.content != expected


def test_unrelated_reviewer_key_rotation_does_not_unpublish_accepted_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "accepted-v4-unrelated-key-rotation"
    expected = _accepted_video_bytes()
    accepted = _write_reproducible_magicfit_tour(tmp_path, slug, expected)
    video_relpath = str(accepted["video_relpath"])
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="accepted-v4-unrelated-key-rotation",
    )
    route = f"/tours/files/{slug}/{video_relpath}"
    assert client.get(route).status_code == 200
    authority = provision_magicfit_reviewer_test_authority(
        tmp_path.parent / f".{tmp_path.name}-reviewer-trust",
        public_tour_root=tmp_path,
    )
    unrelated_key_id = "unrelated-reviewer-key"
    unrelated_authority_id = "unrelated-reviewer-authority"
    unrelated_key_path = authority.public_key_path.with_name(
        f"{unrelated_key_id}.json"
    )
    unrelated_key_path.write_text(
        json.dumps(
            {
                "contract_name": REVIEWER_PUBLIC_KEY_CONTRACT,
                "algorithm": REVIEWER_AUTHORIZATION_ALGORITHM,
                "key_id": unrelated_key_id,
                "authority_id": unrelated_authority_id,
                "public_key_base64": base64.b64encode(
                    authority.public_key_bytes
                ).decode("ascii"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    unrelated_key_path.chmod(0o600)
    trust_store = authority.read_trust_store()
    keys = trust_store["keys"]
    assert isinstance(keys, list)
    keys.append(
        {
            "key_id": unrelated_key_id,
            "authority_id": unrelated_authority_id,
            "public_key_relpath": f"keys/{unrelated_key_path.name}",
            "valid_from": "2020-01-01T00:00:00Z",
            "valid_until": "2099-12-31T23:59:59Z",
            "revoked": False,
        }
    )
    authority.write_trust_store(trust_store)

    still_public = client.get(route)

    assert still_public.status_code == 200
    assert still_public.content == expected


def test_legacy_v3_magicfit_acceptance_is_quarantined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "legacy-v3-acceptance-quarantined"
    expected = _accepted_video_bytes()
    accepted = _write_reproducible_magicfit_tour(tmp_path, slug, expected)
    delivery_digest = str(accepted["delivery_digest"])
    sidecar_path = (
        tmp_path
        / slug
        / ".magicfit-deliveries"
        / f"{delivery_digest}.json"
    )
    legacy = json.loads(sidecar_path.read_text(encoding="utf-8"))
    legacy["contract_name"] = "propertyquarry.magicfit_delivery_acceptance.v3"
    sidecar_path.write_text(json.dumps(legacy), encoding="utf-8")
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="legacy-v3-acceptance-quarantined",
    )

    response = client.get(
        f"/tours/files/{slug}/{accepted['video_relpath']}"
    )

    assert response.status_code == 410
    assert response.content != expected


def test_magicfit_alias_removal_cannot_downgrade_accepted_media_to_generic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "accepted-v3-alias-tamper"
    expected = _accepted_video_bytes()
    accepted = _write_reproducible_magicfit_tour(
        tmp_path,
        slug,
        expected,
        include_scene=True,
    )
    video_relpath = str(accepted["video_relpath"])
    manifest_path = tmp_path / slug / "tour.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("video_provider", None)
    manifest.pop("video_provider_backend_key", None)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="accepted-v3-alias-tamper",
    )

    direct = client.get(f"/tours/files/{slug}/{video_relpath}")
    walkthrough = client.get(f"/tours/{slug}/walkthrough")
    landing = client.get(f"/tours/{slug}")

    assert direct.status_code == 410
    assert direct.content != expected
    assert walkthrough.status_code == 404
    assert landing.status_code == 200
    assert f"/tours/{slug}/walkthrough" not in landing.text


def test_magicfit_media_namespace_never_uses_generic_manifest_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "magicfit-namespace-without-authority"
    bundle = tmp_path / slug
    bundle.mkdir(parents=True)
    relpath = "magicfit-media/unreviewed.mp4"
    target = bundle / relpath
    target.parent.mkdir()
    target.write_bytes(_accepted_video_bytes())
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Unreviewed namespace",
                "video_relpath": relpath,
                "public_assets": [relpath],
            }
        ),
        encoding="utf-8",
    )
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="magicfit-namespace-without-authority",
    )
    _patch_vendor_route_validators(monkeypatch, target)

    response = client.get(f"/tours/files/{slug}/{relpath}")
    walkthrough = client.get(f"/tours/{slug}/walkthrough")
    pano2vr = client.get(f"/tours/pano2vr/{slug}/{relpath}")
    three_d_vista = client.get(f"/tours/3dvista/{slug}/{relpath}")

    assert response.status_code == 410
    assert response.content != target.read_bytes()
    assert walkthrough.status_code == 404
    assert pano2vr.status_code == 404
    assert three_d_vista.status_code == 404


def test_private_magicfit_namespaces_and_control_artifacts_never_enter_allowlist() -> None:
    public_relpath = "media/walkthrough.mp4"
    payload = {
        "slug": "quarantine-allowlist",
        "video_relpath": public_relpath,
        "public_assets": [
            ".magicfit-staging/" + ("a" * 64) + "/video.mp4",
            ".magicfit-deliveries/" + ("b" * 64) + ".mp4",
            "tour.magicfit.pending.json",
            "tour.magicfit.control.mp4",
            "tour.magicfit.json",
            "review.magicfit.json",
            public_relpath,
        ],
    }

    assert public_tour_allowed_asset_paths(payload) == {public_relpath}
    for private_relpath in payload["public_assets"][:-1]:
        assert public_tour_safe_asset_relpath(private_relpath) == ""


@pytest.mark.parametrize("delimiter", tuple(":?#[]@!$&'()*+,;=%"))
def test_public_asset_uri_delimiters_are_rejected_consistently(
    delimiter: str,
) -> None:
    relpath = f"media/walk{delimiter}through.mp4"

    assert public_tour_safe_asset_relpath(relpath) == ""
    assert public_tour_file_url("uri-safe-tour", relpath) == ""
    assert (
        _hosted_public_tour_asset_url(
            "https://propertyquarry.com/tours/uri-safe-tour",
            slug="uri-safe-tour",
            asset_relpath=relpath,
        )
        == ""
    )


def test_public_asset_url_encodes_each_safe_path_component() -> None:
    relpath = "media/Living room Überblick.mp4"
    expected = "/tours/files/uri-safe-tour/media/Living%20room%20%C3%9Cberblick.mp4"

    assert public_tour_file_url("uri-safe-tour", relpath) == expected
    assert (
        _hosted_public_tour_asset_url(
            "/tours/uri-safe-tour",
            slug="uri-safe-tour",
            asset_relpath=relpath,
        )
        == expected
    )
    assert (
        _hosted_public_tour_asset_url(
            "https://propertyquarry.com/tours/uri-safe-tour",
            slug="uri-safe-tour",
            asset_relpath=relpath,
        )
        == f"https://propertyquarry.com{expected}"
    )


def _write_descriptor_route_bundle(root: Path, slug: str, body: bytes) -> Path:
    bundle = root / slug
    target = bundle / "media" / "asset.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(body)
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "title": "Descriptor-bound route",
                "video_relpath": "media/asset.mp4",
                "video_sidecar_relpath": "media/asset.quality.json",
                "scenes": [
                    {
                        "name": "Living room",
                        "role": "video",
                        "asset_relpath": "media/asset.mp4",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (bundle / "media" / "asset.quality.json").write_text(
        json.dumps(
            {
                "acceptance_status": "accepted",
                "launch_eligible": True,
            }
        ),
        encoding="utf-8",
    )
    return target


def _patch_vendor_route_validators(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
) -> None:
    def _validated(
        _slug: str,
        _asset_path: str,
        **_kwargs: object,
    ) -> Path:
        return target

    monkeypatch.setattr(public_tours, "_pano2vr_export_file", _validated)
    monkeypatch.setattr(public_tours, "_3dvista_export_file", _validated)


def _descriptor_route_paths(slug: str) -> tuple[str, ...]:
    return (
        f"/tours/files/{slug}/media/asset.mp4",
        f"/tours/{slug}/walkthrough",
        f"/tours/pano2vr/{slug}/media/asset.mp4",
        f"/tours/3dvista/{slug}/media/asset.mp4",
    )


@pytest.mark.parametrize("route_index", range(4))
def test_all_public_file_routes_preserve_head_range_and_revocation_safe_cache(
    route_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"descriptor-semantics-{route_index}"
    body = b"0123456789abcdef"
    target = _write_descriptor_route_bundle(tmp_path, slug, body)
    _patch_vendor_route_validators(monkeypatch, target)
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id=f"descriptor-semantics-{route_index}",
    )
    route = _descriptor_route_paths(slug)[route_index]

    head = client.head(route)
    partial = client.get(route, headers={"Range": "bytes=3-7"})
    multipart = client.get(route, headers={"Range": "bytes=0-1,8-9"})
    honored_if_range = client.get(
        route,
        headers={"Range": "bytes=3-7", "If-Range": head.headers["etag"]},
    )
    ignored_if_range = client.get(
        route,
        headers={"Range": "bytes=3-7", "If-Range": '"stale-identity"'},
    )
    malformed = client.get(route, headers={"Range": "items=0-1"})
    unsatisfied = client.get(route, headers={"Range": "bytes=999-1000"})

    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(body))
    assert head.headers["accept-ranges"] == "bytes"
    assert head.headers["cache-control"] == "no-store"
    assert partial.status_code == 206
    assert partial.content == body[3:8]
    assert partial.headers["content-range"] == f"bytes 3-7/{len(body)}"
    assert partial.headers["cache-control"] == "no-store"
    assert multipart.status_code == 206
    assert multipart.headers["content-type"].startswith(
        "multipart/byteranges; boundary="
    )
    assert body[0:2] in multipart.content
    assert body[8:10] in multipart.content
    assert honored_if_range.status_code == 206
    assert honored_if_range.content == body[3:8]
    assert ignored_if_range.status_code == 200
    assert ignored_if_range.content == body
    assert malformed.status_code == 400
    assert malformed.headers["cache-control"] == "no-store"
    assert unsatisfied.status_code == 416
    assert unsatisfied.headers["content-range"] == f"bytes */{len(body)}"
    assert unsatisfied.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route_index", range(4))
def test_all_public_file_routes_stream_the_opened_inode_after_path_swap(
    route_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"descriptor-swap-{route_index}"
    original_body = b"original-descriptor-bound-bytes"
    attacker_body = b"attacker-replacement-bytes"
    target = _write_descriptor_route_bundle(tmp_path, slug, original_body)
    _patch_vendor_route_validators(monkeypatch, target)
    original_response = public_tours._descriptor_bound_public_tour_response
    swapped = False

    def _swap_after_open(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            archived = target.with_name("opened-original.mp4")
            os.replace(target, archived)
            target.write_bytes(attacker_body)
        return original_response(*args, **kwargs)

    monkeypatch.setattr(
        public_tours,
        "_descriptor_bound_public_tour_response",
        _swap_after_open,
    )
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id=f"descriptor-swap-{route_index}",
    )

    response = client.get(_descriptor_route_paths(slug)[route_index])

    assert response.status_code == 200
    assert response.content == original_body
    assert response.content != attacker_body


@pytest.mark.parametrize("route_index", range(4))
def test_all_public_file_routes_reject_swap_at_validation_open_boundary(
    route_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"descriptor-validation-swap-{route_index}"
    target = _write_descriptor_route_bundle(tmp_path, slug, b"validated-original")
    attacker_body = b"validation-boundary-attacker"
    swapped = False

    def _swap_target() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        os.replace(target, target.with_name("validated-original.mp4"))
        target.write_bytes(attacker_body)

    if route_index in {0, 1}:
        original_validator = public_tours._asset_file

        def _asset_validator(*args: object, **kwargs: object) -> Path:
            result = original_validator(*args, **kwargs)
            _swap_target()
            return result

        monkeypatch.setattr(public_tours, "_asset_file", _asset_validator)
        _patch_vendor_route_validators(monkeypatch, target)
    else:
        def _vendor_validator(
            _slug: str,
            _asset_path: str,
            **_kwargs: object,
        ) -> Path:
            _swap_target()
            return target

        monkeypatch.setattr(public_tours, "_pano2vr_export_file", _vendor_validator)
        monkeypatch.setattr(public_tours, "_3dvista_export_file", _vendor_validator)
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id=f"descriptor-validation-swap-{route_index}",
    )

    response = client.get(_descriptor_route_paths(slug)[route_index])

    assert response.status_code == 404
    assert response.content != attacker_body


@pytest.mark.parametrize("route_index", range(4))
def test_all_public_file_routes_reject_bundle_directory_swap_during_validation(
    route_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"descriptor-bundle-swap-{route_index}"
    target = _write_descriptor_route_bundle(tmp_path, slug, b"held-bundle-bytes")
    bundle = target.parents[1]
    archived_bundle = tmp_path / f"{slug}-held"
    attacker_body = b"replacement-bundle-attacker"
    swapped = False

    def _swap_bundle() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        bundle.rename(archived_bundle)
        replacement_target = _write_descriptor_route_bundle(
            tmp_path,
            slug,
            attacker_body,
        )
        assert replacement_target == target

    if route_index in {0, 1}:
        original_validator = public_tours._asset_file

        def _asset_validator(*args: object, **kwargs: object) -> Path:
            result = original_validator(*args, **kwargs)
            _swap_bundle()
            return result

        monkeypatch.setattr(public_tours, "_asset_file", _asset_validator)
        _patch_vendor_route_validators(monkeypatch, target)
    else:
        def _vendor_validator(
            _slug: str,
            _asset_path: str,
            **_kwargs: object,
        ) -> Path:
            _swap_bundle()
            return target

        monkeypatch.setattr(public_tours, "_pano2vr_export_file", _vendor_validator)
        monkeypatch.setattr(public_tours, "_3dvista_export_file", _vendor_validator)
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id=f"descriptor-bundle-swap-{route_index}",
    )

    response = client.get(_descriptor_route_paths(slug)[route_index])

    assert response.status_code == 404
    assert response.content != attacker_body


@pytest.mark.parametrize("route_index", range(4))
def test_all_public_file_routes_reject_symlinked_final_asset(
    route_index: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = f"descriptor-symlink-{route_index}"
    target = _write_descriptor_route_bundle(tmp_path, slug, b"original")
    outside = tmp_path / f"outside-{route_index}.mp4"
    outside.write_bytes(b"outside-secret")
    target.unlink()
    target.symlink_to(outside)
    _patch_vendor_route_validators(monkeypatch, target)
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id=f"descriptor-symlink-{route_index}",
    )

    response = client.get(_descriptor_route_paths(slug)[route_index])

    assert response.status_code == 404
    assert response.content != outside.read_bytes()


def test_accepted_magicfit_descriptor_identity_survives_post_open_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slug = "accepted-v3-descriptor-swap"
    expected = _accepted_video_bytes()
    accepted = _write_reproducible_magicfit_tour(tmp_path, slug, expected)
    video_relpath = str(accepted["video_relpath"])
    target = tmp_path / slug / video_relpath
    attacker = tmp_path / "attacker.mp4"
    attacker.write_bytes(b"\x00\x00\x00\x18ftypmp42attacker")
    original_response = public_tours._descriptor_bound_public_tour_response

    def _symlink_swap_after_open(*args: object, **kwargs: object):
        archived = target.with_name("opened-accepted.mp4")
        os.replace(target, archived)
        target.symlink_to(attacker)
        return original_response(*args, **kwargs)

    monkeypatch.setattr(
        public_tours,
        "_descriptor_bound_public_tour_response",
        _symlink_swap_after_open,
    )
    client = _public_media_client(
        tmp_path,
        monkeypatch,
        principal_id="accepted-v3-descriptor-swap",
    )

    response = client.get(f"/tours/files/{slug}/{video_relpath}")

    assert response.status_code == 200
    assert response.content == expected
    assert response.content != attacker.read_bytes()
