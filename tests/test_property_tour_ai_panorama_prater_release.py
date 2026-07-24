from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.product import property_tour_ai_panorama_prater_release as release
from app.product import service
from app.product.property_tour_ai_panorama_intake import AiPanoramaIntakeError


def _admission(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "authenticated_principal_id": "private-owner@example.invalid",
        "search_run_id": release.PRATER_SEARCH_RUN_ID,
        "candidate_ref": release.PRATER_CANDIDATE_REF,
        "external_id": release.PRATER_EXTERNAL_ID,
        "listing_url": (
            "https://www.willhaben.at/iad/immobilien/d/1807240910/"
        ),
        "source_ref": release.PRATER_SOURCE_REF,
        "provider_key": release.PRATER_PROVIDER_KEY,
        "expected_slug": release.PRATER_SLUG,
        "expected_source_tree_sha256": release.PRATER_SOURCE_TREE_SHA256,
        "expected_tour_sha256": release.PRATER_TOUR_SHA256,
        "expected_core_manifest_sha256": (
            release.PRATER_CORE_MANIFEST_SHA256
        ),
        "expected_materialization_receipt_sha256": (
            release.PRATER_MATERIALIZATION_RECEIPT_SHA256
        ),
        "expected_candidate_marker_sha256": (
            release.PRATER_CANDIDATE_MARKER_SHA256
        ),
        "expected_publication_record_sha256": "1" * 64,
        "artifact_relpath": release.PRATER_ARTIFACT_RELPATH,
        "materialization_receipt_relpath": (
            release.PRATER_MATERIALIZATION_RECEIPT_RELPATH
        ),
        "incoming_root": release.PRATER_CONTROLLER_ARTIFACT_ROOT,
        "public_tour_dir": release.PRATER_CONTROLLER_PUBLIC_ROOT,
        "source_bundle": (
            release.PRATER_CONTROLLER_ARTIFACT_ROOT
            / release.PRATER_ARTIFACT_RELPATH
        ),
        "materialization_receipt_path": (
            release.PRATER_CONTROLLER_ARTIFACT_ROOT
            / release.PRATER_MATERIALIZATION_RECEIPT_RELPATH
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def _verified_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        release,
        "_revalidate_ai_panorama_install_admission",
        lambda admission, *, require_consumed: admission,
    )
    monkeypatch.setattr(
        release,
        "property_search_source_url_sha256",
        lambda _value: release.PRATER_PROPERTY_URL_SHA256,
    )


def test_prater_dry_run_projects_only_exact_signed_fixed_root_request(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: None,
) -> None:
    admission = _admission()
    observed: dict[str, object] = {}

    def _install(
        request: dict[str, object],
        *,
        apply: bool,
        publication_admission: object,
    ) -> dict[str, object]:
        observed.update(request)
        assert apply is False
        assert publication_admission is admission
        return {
            "status": "validated",
            "release_eligible": True,
            "private_values_redacted": True,
        }

    monkeypatch.setattr(release, "install_sealed_ai_panorama_bundle", _install)

    receipt = release.run_prater_ai_panorama_release(admission)

    assert observed["principal_id"] == admission.authenticated_principal_id
    assert observed["search_run_id"] == release.PRATER_SEARCH_RUN_ID
    assert observed["source_bundle"] == str(admission.source_bundle)
    assert observed["public_tour_dir"] == str(
        release.PRATER_CONTROLLER_PUBLIC_ROOT
    )
    assert observed["expected_publication_record_sha256"] == "1" * 64
    assert receipt["status"] == "validated"
    assert receipt["binding_status"] == "requires_installed_owner_receipt"
    assert receipt["release_eligible"] is False


def test_prater_apply_finishes_owner_receipt_cas_binding(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: None,
) -> None:
    admission = _admission()
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        lambda request, *, apply, publication_admission: {
            "status": "installed",
            "release_eligible": True,
            "private_values_redacted": True,
        },
    )

    def _bind(**kwargs: object) -> dict[str, object]:
        observed.update(kwargs)
        return {
            "status": "applied",
            "mode": "apply",
            "changed": True,
            "before_sha256": "1" * 64,
            "after_sha256": "2" * 64,
            "persisted_sha256": "2" * 64,
            "principal_id": "must-not-leak",
        }

    monkeypatch.setattr(
        service,
        "bind_property_search_candidate_generated_reconstruction",
        _bind,
    )

    receipt = release.run_prater_ai_panorama_release(admission, apply=True)

    assert observed == {
        "principal_id": admission.authenticated_principal_id,
        "run_id": release.PRATER_SEARCH_RUN_ID,
        "candidate_ref": release.PRATER_CANDIDATE_REF,
        "expected_listing_id": release.PRATER_EXTERNAL_ID,
        "generated_reconstruction_url": release.PRATER_PUBLIC_CONTROL_URL,
        "expected_record_sha256": "1" * 64,
        "reconstruction_kind": "ai_panorama_360",
        "apply": True,
    }
    assert receipt["status"] == "released"
    assert receipt["release_eligible"] is True
    assert receipt["binding_status"] == "applied"
    assert "principal_id" not in receipt["binding_receipt"]


def test_prater_profile_rejects_relocation_before_installer_path_read(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: None,
) -> None:
    admission = _admission(
        source_bundle="/tmp/operator-selected/prater",
    )
    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        lambda *_args, **_kwargs: pytest.fail(
            "installer must not inspect a relocated caller path"
        ),
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_artifact_path_mismatch",
    ):
        release.run_prater_ai_panorama_release(admission, apply=True)


def test_prater_profile_rejects_any_public_identity_drift(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: None,
) -> None:
    admission = _admission(expected_tour_sha256="0" * 64)
    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        lambda *_args, **_kwargs: pytest.fail(
            "installer must not inspect a differently sealed artifact"
        ),
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_permit_binding_mismatch",
    ):
        release.run_prater_ai_panorama_release(admission, apply=True)
