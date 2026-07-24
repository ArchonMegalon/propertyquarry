from __future__ import annotations

import json
import os
import shutil
import socket
import tempfile
from pathlib import Path

from fastapi import HTTPException
import pytest

from app.api.routes import public_tours


PROTECTED_SLUG = (
    "prater-messe-maisonette-ai-360-053ad185e1c44b2e"
)


def _configure_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    dynamic_root = tmp_path / "dynamic"
    governed_root = tmp_path / "governed"
    dynamic_root.mkdir()
    governed_root.mkdir()
    monkeypatch.setenv("EA_RUNTIME_MODE", "test")
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(dynamic_root))
    monkeypatch.setenv(
        "EA_GOVERNED_PUBLIC_TOUR_DIR",
        str(governed_root),
    )
    return dynamic_root, governed_root


def _write_bundle(
    root: Path,
    slug: str,
    *,
    marker: str,
    asset: bytes = b"",
) -> Path:
    bundle = root / slug
    bundle.mkdir(parents=True)
    payload: dict[str, object] = {"slug": slug, "marker": marker}
    if asset:
        (bundle / "scene.jpg").write_bytes(asset)
    (bundle / "tour.json").write_text(
        json.dumps(payload, sort_keys=True),
        encoding="utf-8",
    )
    return bundle


def _write_closeout(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_UID",
        os.geteuid(),
    )
    monkeypatch.setattr(
        public_tours,
        "_GOVERNED_PRATER_REVOCATION_REQUIRED_GID",
        os.getegid(),
    )
    payload = {
        "schema": public_tours._GOVERNED_PRATER_REVOCATION_SCHEMA,
        "version": 1,
        "authority": "propertyquarry-release-control",
        "status": "revoked",
        "slug": PROTECTED_SLUG,
        "tour_sha256": (
            public_tours._GOVERNED_PRATER_REVOCATION_TOUR_SHA256
        ),
        "revocation_id": "a" * 32,
        "revoked_at": "2026-07-24T12:00:00Z",
    }
    path = root / public_tours._GOVERNED_PRATER_REVOCATION_FILENAME
    path.write_bytes(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )
    path.chmod(public_tours._GOVERNED_PRATER_REVOCATION_MODE)
    return path


def test_protected_manifest_and_asset_snapshot_use_only_governed_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dynamic_root, governed_root = _configure_roots(
        monkeypatch,
        tmp_path,
    )
    _write_bundle(
        dynamic_root,
        PROTECTED_SLUG,
        marker="dynamic-shadow",
        asset=b"dynamic",
    )
    governed_bundle = _write_bundle(
        governed_root,
        PROTECTED_SLUG,
        marker="governed",
        asset=b"governed",
    )

    def _unexpected_revocation(_slug: str) -> dict[str, object]:
        raise AssertionError("dynamic revocation must not gate governed tour")

    monkeypatch.setattr(
        public_tours,
        "hosted_property_tour_revocation_receipt",
        _unexpected_revocation,
    )

    assert public_tours._load_tour(PROTECTED_SLUG)["marker"] == "governed"
    with public_tours._public_tour_file_policy_snapshot(
        PROTECTED_SLUG,
        include_private_receipt=False,
    ) as snapshot:
        assert snapshot.bundle_dir == governed_bundle
        assert snapshot.payload["marker"] == "governed"
        opened = public_tours._open_public_tour_asset_descriptor(
            snapshot,
            "scene.jpg",
        )
        try:
            assert os.read(opened.descriptor, 32) == b"governed"
        finally:
            os.close(opened.descriptor)


def test_protected_slug_rejects_dynamic_and_legacy_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dynamic_root, governed_root = _configure_roots(
        monkeypatch,
        tmp_path,
    )
    _write_bundle(
        dynamic_root,
        PROTECTED_SLUG,
        marker="dynamic-shadow",
    )
    for root in (dynamic_root, governed_root):
        (root / f"{PROTECTED_SLUG}.json").write_text(
            json.dumps(
                {"slug": PROTECTED_SLUG, "marker": "legacy-shadow"}
            ),
            encoding="utf-8",
        )

    with pytest.raises(HTTPException) as missing:
        public_tours._load_tour(PROTECTED_SLUG)
    assert missing.value.status_code == 404

    governed_bundle = _write_bundle(
        governed_root,
        PROTECTED_SLUG,
        marker="governed",
    )
    assert public_tours._load_tour(PROTECTED_SLUG)["marker"] == "governed"
    governed_bundle.rename(tmp_path / "governed-rolled-back")

    with pytest.raises(HTTPException) as rolled_back:
        public_tours._load_tour(PROTECTED_SLUG)
    assert rolled_back.value.status_code == 404


def test_governed_prod_configuration_is_exact_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    monkeypatch.setenv(
        "EA_PUBLIC_TOUR_DIR",
        "/data/public_property_tours",
    )
    monkeypatch.delenv("EA_GOVERNED_PUBLIC_TOUR_DIR", raising=False)

    with pytest.raises(HTTPException) as missing:
        public_tours._governed_tour_dir()
    assert missing.value.status_code == 503
    assert missing.value.detail == "governed_tour_storage_unconfigured"

    monkeypatch.setenv(
        "EA_GOVERNED_PUBLIC_TOUR_DIR",
        "/data/public_property_tours/governed-shadow",
    )
    with pytest.raises(HTTPException) as shadow:
        public_tours._governed_tour_dir()
    assert shadow.value.status_code == 503
    assert shadow.value.detail == "governed_tour_storage_invalid"

    monkeypatch.setenv(
        "EA_GOVERNED_PUBLIC_TOUR_DIR",
        "/data/governed_public_property_tours",
    )
    assert public_tours._governed_tour_dir() == Path(
        "/data/governed_public_property_tours"
    )


def test_nonprotected_slugs_remain_dynamic_and_revocable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dynamic_root, governed_root = _configure_roots(
        monkeypatch,
        tmp_path,
    )
    slug = "ordinary-tour"
    _write_bundle(dynamic_root, slug, marker="dynamic")
    _write_bundle(governed_root, slug, marker="governed-shadow")
    monkeypatch.setattr(
        public_tours,
        "hosted_property_tour_revocation_receipt",
        lambda _slug: {},
    )

    assert public_tours._load_tour(slug)["marker"] == "dynamic"

    monkeypatch.setattr(
        public_tours,
        "hosted_property_tour_revocation_receipt",
        lambda _slug: {"status": "revoked"},
    )
    with pytest.raises(HTTPException) as revoked:
        public_tours._load_tour(slug)
    assert revoked.value.status_code == 410


def test_governed_bundle_and_asset_symlinks_and_traversal_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _dynamic_root, governed_root = _configure_roots(
        monkeypatch,
        tmp_path,
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "tour.json").write_text(
        json.dumps({"slug": PROTECTED_SLUG}),
        encoding="utf-8",
    )
    (governed_root / PROTECTED_SLUG).symlink_to(
        outside,
        target_is_directory=True,
    )

    with pytest.raises(HTTPException) as bundle_symlink:
        public_tours._resolved_tour_bundle(PROTECTED_SLUG)
    assert bundle_symlink.value.status_code == 404

    (governed_root / PROTECTED_SLUG).unlink()
    bundle = _write_bundle(
        governed_root,
        PROTECTED_SLUG,
        marker="governed",
    )
    outside_asset = outside / "outside.jpg"
    outside_asset.write_bytes(b"outside")
    (bundle / "linked.jpg").symlink_to(outside_asset)

    with public_tours._public_tour_file_policy_snapshot(
        PROTECTED_SLUG,
        include_private_receipt=False,
    ) as snapshot:
        for relpath in ("../outside/outside.jpg", "linked.jpg"):
            with pytest.raises(HTTPException) as rejected:
                public_tours._open_public_tour_asset_descriptor(
                    snapshot,
                    relpath,
                )
            assert rejected.value.status_code == 404


def test_governed_closeout_blocks_manifests_assets_and_inflight_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _dynamic_root, governed_root = _configure_roots(
        monkeypatch,
        tmp_path,
    )
    _write_bundle(
        governed_root,
        PROTECTED_SLUG,
        marker="governed",
        asset=b"governed",
    )

    with pytest.raises(HTTPException) as inflight:
        with public_tours._public_tour_file_policy_snapshot(
            PROTECTED_SLUG,
            include_private_receipt=False,
        ):
            _write_closeout(governed_root, monkeypatch)
    assert inflight.value.status_code == 410
    assert inflight.value.detail == "tour_revoked"

    with pytest.raises(HTTPException) as manifest:
        public_tours._load_tour(PROTECTED_SLUG)
    assert manifest.value.status_code == 410
    assert manifest.value.detail == "tour_revoked"

    with pytest.raises(HTTPException) as private_merge:
        public_tours._load_tour_with_private_receipt(PROTECTED_SLUG)
    assert private_merge.value.status_code == 410
    assert private_merge.value.detail == "tour_revoked"

    with pytest.raises(HTTPException) as asset:
        with public_tours._public_tour_file_policy_snapshot(
            PROTECTED_SLUG,
            include_private_receipt=False,
        ):
            pass
    assert asset.value.status_code == 410


@pytest.mark.parametrize(
    "target_kind",
    (
        "partial-directory",
        "regular-file",
        "symlink",
        "broken-symlink",
        "fifo",
        "socket",
    ),
)
def test_governed_closeout_is_marker_first_for_any_protected_target_node(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target_kind: str,
) -> None:
    short_root: Path | None = None
    if target_kind == "socket":
        short_root = Path(tempfile.mkdtemp(prefix="pqr-", dir="/tmp"))
        dynamic_root = short_root / "d"
        governed_root = short_root / "g"
        dynamic_root.mkdir()
        governed_root.mkdir()
        monkeypatch.setenv("EA_RUNTIME_MODE", "test")
        monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(dynamic_root))
        monkeypatch.setenv(
            "EA_GOVERNED_PUBLIC_TOUR_DIR",
            str(governed_root),
        )
    else:
        _dynamic_root, governed_root = _configure_roots(
            monkeypatch,
            tmp_path,
        )
    target = governed_root / PROTECTED_SLUG
    if target_kind == "partial-directory":
        target.mkdir()
        (target / "partial.tmp").write_bytes(b"partial")
    elif target_kind == "regular-file":
        target.write_bytes(b"partial")
    elif target_kind == "symlink":
        outside = tmp_path / "existing-target"
        outside.write_bytes(b"outside")
        target.symlink_to(outside)
    elif target_kind == "broken-symlink":
        target.symlink_to(tmp_path / "missing-target")
    elif target_kind == "fifo":
        os.mkfifo(target)
    elif target_kind == "socket":
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            endpoint.bind(str(target))
        finally:
            endpoint.close()
    else:
        raise AssertionError(target_kind)
    _write_closeout(governed_root, monkeypatch)

    with pytest.raises(HTTPException) as revoked:
        public_tours._load_tour(PROTECTED_SLUG)
    assert revoked.value.status_code == 410
    assert revoked.value.detail == "tour_revoked"

    with pytest.raises(HTTPException) as snapshot:
        with public_tours._public_tour_file_policy_snapshot(
            PROTECTED_SLUG,
            include_private_receipt=False,
        ):
            pass
    assert snapshot.value.status_code == 410
    assert snapshot.value.detail == "tour_revoked"
    if short_root is not None:
        shutil.rmtree(short_root)


def test_malformed_or_symlinked_governed_closeout_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _dynamic_root, governed_root = _configure_roots(
        monkeypatch,
        tmp_path,
    )
    _write_bundle(
        governed_root,
        PROTECTED_SLUG,
        marker="governed",
    )
    closeout_path = (
        governed_root / public_tours._GOVERNED_PRATER_REVOCATION_FILENAME
    )
    closeout_path.write_bytes(b"{}\n")
    closeout_path.chmod(public_tours._GOVERNED_PRATER_REVOCATION_MODE)

    with pytest.raises(HTTPException) as malformed:
        public_tours._load_tour(PROTECTED_SLUG)
    assert malformed.value.status_code == 503
    assert malformed.value.detail == "governed_tour_closeout_invalid"

    closeout_path.unlink()
    outside = tmp_path / "outside-closeout.json"
    outside.write_bytes(b"{}\n")
    closeout_path.symlink_to(outside)
    with pytest.raises(HTTPException) as symlink:
        public_tours._load_tour(PROTECTED_SLUG)
    assert symlink.value.status_code == 503
    assert symlink.value.detail == "governed_tour_closeout_invalid"


@pytest.mark.parametrize(
    "malformation",
    (
        "zero",
        "truncated",
        "extra-key",
        "duplicate-key",
        "noncanonical",
        "directory",
        "fifo",
        "wrong-mode",
        "hardlink",
        "wrong-owner",
        "impossible-date",
    ),
)
def test_every_malformed_governed_closeout_shape_is_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    malformation: str,
) -> None:
    _dynamic_root, governed_root = _configure_roots(
        monkeypatch,
        tmp_path,
    )
    _write_bundle(
        governed_root,
        PROTECTED_SLUG,
        marker="governed",
    )
    closeout_path = _write_closeout(governed_root, monkeypatch)
    valid_raw = closeout_path.read_bytes()
    valid_payload = json.loads(valid_raw.decode("ascii"))
    closeout_path.chmod(0o644)

    if malformation == "zero":
        closeout_path.write_bytes(b"")
    elif malformation == "truncated":
        closeout_path.write_bytes(valid_raw[:-2])
    elif malformation == "extra-key":
        valid_payload["extra"] = True
        closeout_path.write_bytes(
            json.dumps(
                valid_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
    elif malformation == "duplicate-key":
        closeout_path.write_bytes(
            valid_raw.replace(
                b"{",
                (
                    b'{"authority":'
                    b'"propertyquarry-release-control",'
                ),
                1,
            )
        )
    elif malformation == "noncanonical":
        closeout_path.write_text(
            json.dumps(valid_payload, indent=2) + "\n",
            encoding="ascii",
        )
    elif malformation == "directory":
        closeout_path.unlink()
        closeout_path.mkdir()
    elif malformation == "fifo":
        closeout_path.unlink()
        os.mkfifo(closeout_path)
    elif malformation == "wrong-mode":
        closeout_path.chmod(0o644)
    elif malformation == "hardlink":
        os.link(closeout_path, tmp_path / "closeout-hardlink")
    elif malformation == "wrong-owner":
        monkeypatch.setattr(
            public_tours,
            "_GOVERNED_PRATER_REVOCATION_REQUIRED_UID",
            os.geteuid() + 1,
        )
    elif malformation == "impossible-date":
        valid_payload["revoked_at"] = "2026-99-99T99:99:99Z"
        closeout_path.write_bytes(
            json.dumps(
                valid_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )

    if closeout_path.is_file():
        closeout_path.chmod(
            0o644
            if malformation == "wrong-mode"
            else public_tours._GOVERNED_PRATER_REVOCATION_MODE
        )
    with pytest.raises(HTTPException) as rejected:
        public_tours._load_tour_with_private_receipt(PROTECTED_SLUG)
    assert rejected.value.status_code == 503
    assert rejected.value.detail == "governed_tour_closeout_invalid"


def test_governed_manifest_slug_binding_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _dynamic_root, governed_root = _configure_roots(
        monkeypatch,
        tmp_path,
    )
    bundle = _write_bundle(
        governed_root,
        PROTECTED_SLUG,
        marker="governed",
    )
    (bundle / "tour.json").write_text(
        json.dumps({"slug": "other-tour", "marker": "governed"}),
        encoding="utf-8",
    )

    with pytest.raises(HTTPException) as mismatch:
        public_tours._load_tour(PROTECTED_SLUG)
    assert mismatch.value.status_code == 500
    assert mismatch.value.detail == "tour_payload_invalid"
