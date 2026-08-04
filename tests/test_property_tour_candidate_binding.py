from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.product import property_tour_hosting
from scripts.bind_property_tour_candidate import bind_property_tour_candidate


def _bundle(tmp_path: Path, *, principal_id: str = "") -> tuple[Path, str, str]:
    root = tmp_path / "public-tours"
    slug = "karl-czerny-gasse-2-urban-jungle"
    property_url_sha256 = hashlib.sha256(b"owned-property-url").hexdigest()
    bundle = root / slug
    bundle.mkdir(parents=True)
    (bundle / "tour.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "publication_status": "ready",
                "property_url_sha256": property_url_sha256,
                "video_relpath": "walkthrough.mp4",
            }
        ),
        encoding="utf-8",
    )
    (bundle / "walkthrough.mp4").write_bytes(b"verified-camera-walkthrough")
    private_payload: dict[str, object] = {
        "legacy_private_fields": {
            "video_provider": "propertyquarry_core_gold",
            "video_provider_key": "propertyquarry_core_gold",
            "video_coverage_proof": "boundary_verified_frame_continuation",
        },
        "three_d_vista_target_provenance": {
            "status": "pass",
            "provider": "3dvista",
            "target_slug": slug,
            "authorization": {"status": "approved"},
            "review": {"property_match": "pass", "visual_match": "pass"},
        },
        "three_d_vista_browser_render_proof": {
            "status": "pass",
            "provider": "3dvista",
            "rendered_viewer": True,
            "interactive_viewer": True,
        },
        "three_d_vista_white_label_proof": {
            "non_trial_export_verified": True,
            "trial_branding_present": False,
        },
    }
    if principal_id:
        private_payload["principal_id"] = principal_id
    private_path = bundle / "tour.private.json"
    private_path.write_text(json.dumps(private_payload), encoding="utf-8")
    private_path.chmod(0o600)
    return root, slug, property_url_sha256


def test_candidate_binding_promotes_only_safe_walkthrough_proof_and_binds_owner(
    tmp_path: Path,
) -> None:
    root, slug, property_url_sha256 = _bundle(tmp_path)
    receipt_path = tmp_path / "receipt.json"

    receipt = bind_property_tour_candidate(
        public_tour_dir=root,
        slug=slug,
        principal_id="user-owned-property",
        property_url_sha256=property_url_sha256,
        receipt_path=receipt_path,
    )

    public_payload = json.loads((root / slug / "tour.json").read_text(encoding="utf-8"))
    private_payload = json.loads((root / slug / "tour.private.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "bound"
    assert "video_provider" not in public_payload
    assert "video_provider_key" not in public_payload
    assert "video_coverage_proof" not in public_payload
    assert "three_d_vista_target_provenance" not in public_payload
    assert private_payload["principal_id"] == "user-owned-property"
    assert private_payload["video_provider_key"] == "propertyquarry_core_gold"
    assert private_payload["video_coverage_proof"] == "boundary_verified_frame_continuation"
    assert receipt_path.stat().st_mode & 0o777 == 0o600


def test_candidate_binding_exposes_walkthrough_only_to_bound_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, slug, property_url_sha256 = _bundle(tmp_path)
    bind_property_tour_candidate(
        public_tour_dir=root,
        slug=slug,
        principal_id="user-owned-property",
        property_url_sha256=property_url_sha256,
    )
    monkeypatch.setenv("EA_PUBLIC_TOUR_DIR", str(root))
    tour_url = f"/tours/{slug}"

    assert property_tour_hosting._hosted_property_tour_walkthrough_open_url(
        tour_url,
    ) == ""
    assert property_tour_hosting._hosted_property_tour_walkthrough_open_url(
        tour_url,
        principal_id="user-other-owner",
    ) == ""
    assert property_tour_hosting._hosted_property_tour_walkthrough_open_url(
        tour_url,
        principal_id="user-owned-property",
    ) == f"/tours/{slug}/walkthrough"


def test_candidate_binding_rejects_cross_principal_rebind(tmp_path: Path) -> None:
    root, slug, property_url_sha256 = _bundle(
        tmp_path,
        principal_id="user-existing-owner",
    )

    with pytest.raises(ValueError, match="principal_conflict"):
        bind_property_tour_candidate(
            public_tour_dir=root,
            slug=slug,
            principal_id="user-other-owner",
            property_url_sha256=property_url_sha256,
        )


def test_candidate_binding_rejects_unverified_public_identity(tmp_path: Path) -> None:
    root, slug, property_url_sha256 = _bundle(tmp_path)

    with pytest.raises(ValueError, match="public_identity_invalid"):
        bind_property_tour_candidate(
            public_tour_dir=root,
            slug=slug,
            principal_id="user-owned-property",
            property_url_sha256="0" * 64,
        )
