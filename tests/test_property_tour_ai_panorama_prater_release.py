from __future__ import annotations

import contextlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.product import property_tour_ai_panorama_admission as admission
from app.product import property_search_storage as storage
from app.product import property_tour_ai_panorama_prater_release as release
from app.product.property_tour_ai_panorama_intake import AiPanoramaIntakeError
from app.product.property_search_tour_binding import (
    property_search_run_record_sha256,
)


def _admission(**overrides: object) -> SimpleNamespace:
    public_root = Path("/data/governed_public_property_tours")
    values: dict[str, object] = {
        "authenticated_principal_id": "private-owner@example.invalid",
        "search_run_id": release.PRATER_SEARCH_RUN_ID,
        "candidate_ref": release.PRATER_CANDIDATE_REF,
        "external_id": release.PRATER_EXTERNAL_ID,
        "listing_url": release.PRATER_LISTING_URL,
        "source_ref": release.PRATER_SOURCE_REF,
        "provider_key": release.PRATER_PROVIDER_KEY,
        "expected_slug": release.PRATER_SLUG,
        "public_control_url": release.PRATER_PUBLIC_CONTROL_URL,
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
        "public_tour_dir": public_root,
        "public_tour_volume_name": release.PRATER_PUBLIC_VOLUME_NAME,
        "public_tour_mount_target": release.PRATER_PUBLIC_MOUNT_TARGET,
        "public_tour_root_device": 41,
        "public_tour_root_inode": 42,
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


def _historical_expected() -> admission.AiPanoramaInstallExpectedBindings:
    return admission.AiPanoramaInstallExpectedBindings(
        subject=admission.CANONICAL_SUBJECT,
        actor_principal_id="propertyquarry-release-controller",
        owner_principal_id="private-owner@example.invalid",
        search_run_id=release.PRATER_SEARCH_RUN_ID,
        candidate_ref=release.PRATER_CANDIDATE_REF,
        external_id=release.PRATER_EXTERNAL_ID,
        listing_url=release.PRATER_LISTING_URL,
        source_ref=release.PRATER_SOURCE_REF,
        provider_key=release.PRATER_PROVIDER_KEY,
        expected_slug=release.PRATER_SLUG,
        expected_source_tree_sha256=release.PRATER_SOURCE_TREE_SHA256,
        expected_tour_sha256=release.PRATER_TOUR_SHA256,
        expected_core_manifest_sha256=release.PRATER_CORE_MANIFEST_SHA256,
        expected_materialization_receipt_sha256=(
            release.PRATER_MATERIALIZATION_RECEIPT_SHA256
        ),
        expected_candidate_marker_sha256=(
            release.PRATER_CANDIDATE_MARKER_SHA256
        ),
        expected_publication_record_sha256="5" * 64,
        artifact_relpath=release.PRATER_ARTIFACT_RELPATH,
        materialization_receipt_relpath=(
            release.PRATER_MATERIALIZATION_RECEIPT_RELPATH
        ),
        request_id="7" * 32,
        repository=admission.CANONICAL_REPOSITORY,
        git_ref=admission.CANONICAL_GIT_REF,
        git_head_sha="d" * 40,
        workflow_ref=admission.CANONICAL_WORKFLOW_REF,
        job=admission.CANONICAL_JOB,
        environment=admission.CANONICAL_ENVIRONMENT,
        review_receipt_sha256="6" * 64,
        web_image=(
            f"{admission.CANONICAL_WEB_IMAGE_REPOSITORY}@sha256:"
            + "c" * 64
        ),
        web_image_id="sha256:" + "d" * 64,
        key_usage=admission.PERMIT_KEY_USAGE,
        key_id="release-key",
        key_epoch=1,
        key_sha256="a" * 64,
        keyring_sha256="b" * 64,
        volume_profile_sha256="c" * 64,
        compose_plan_sha256="d" * 64,
        volume_id=admission.CANONICAL_PUBLIC_TOUR_VOLUME_ID,
        artifact_root_device=31,
        artifact_root_inode=32,
        public_tour_root_device=41,
        public_tour_root_inode=42,
        execution_lease_seconds=600,
    )


def _publication_record(
    *,
    status: str = "processed",
) -> dict[str, object]:
    return {
        "run_id": release.PRATER_SEARCH_RUN_ID,
        "principal_id": "private-owner@example.invalid",
        "status": status,
        "summary": {
            "ranked_candidates": [
                {
                    "candidate_ref": release.PRATER_CANDIDATE_REF,
                    "property_url": release.PRATER_LISTING_URL,
                    "listing_id": release.PRATER_EXTERNAL_ID,
                    "external_id": release.PRATER_EXTERNAL_ID,
                    "source_ref": release.PRATER_SOURCE_REF,
                    "platform": release.PRATER_PROVIDER_KEY,
                    "source_label": "Willhaben",
                }
            ]
        },
    }


@pytest.fixture
def _verified_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        release,
        "_revalidate_ai_panorama_install_admission",
        lambda admission, *, require_consumed: admission,
    )
    monkeypatch.setattr(
        release,
        "prepare_ai_panorama_publication_binding",
        lambda *_args, **_kwargs: {
            "status": "change-required",
            "publication_binding_expected_before_sha256": "1" * 64,
            "publication_binding_expected_after_sha256": "2" * 64,
            "publication_binding_bound_at": "2026-07-24T12:00:00+00:00",
            "database_mutation_performed": False,
            "private_values_redacted": True,
        },
    )
    manifests = iter(
        (
            {
                "state": "absent",
                "target_relpath": release.PRATER_SLUG,
                "public_root_device": 41,
                "public_root_inode": 42,
                "reserved_entry_count": 0,
                "reserved_entries_sha256": "0" * 64,
            },
            {
                "state": "present",
                "target_relpath": release.PRATER_SLUG,
                "public_root_device": 41,
                "public_root_inode": 42,
                "tree_sha256": "7" * 64,
                "tour_private_sha256": "8" * 64,
                "file_count": 9,
                "total_bytes": 1234,
                "reserved_entry_count": 0,
                "reserved_entries_sha256": "0" * 64,
            },
        )
    )
    monkeypatch.setattr(release, "_target_manifest", lambda _verified: next(manifests))
    def _begin(
        _verified: object,
        *,
        evidence: dict[str, object],
    ) -> SimpleNamespace:
        observed["prepared_evidence"] = evidence
        return SimpleNamespace(operation_id="operation")

    monkeypatch.setattr(
        release,
        "begin_ai_panorama_install_operation",
        _begin,
    )
    monkeypatch.setattr(
        release,
        "finish_ai_panorama_install_operation",
        lambda *_args, **_kwargs: "9" * 64,
    )
    return observed


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
    assert observed["public_tour_dir"] == str(admission.public_tour_dir)
    assert observed["expected_publication_record_sha256"] == "1" * 64
    assert receipt["status"] == "validated"
    assert receipt["binding_status"] == "requires_installed_owner_receipt"
    assert receipt["release_eligible"] is False


def test_publication_record_discovery_is_exact_and_non_authorizing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _publication_record()
    calls: list[dict[str, object]] = []

    def _load(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return record

    monkeypatch.setattr(
        release,
        "load_unique_property_search_run_record_for_discovery",
        _load,
    )

    result = release.discover_prater_ai_panorama_publication_record()

    assert calls == [
        {
            "run_id": release.PRATER_SEARCH_RUN_ID,
        }
    ]
    assert result["status"] == "record-discovered"
    assert result["expected_publication_record_sha256"] == (
        property_search_run_record_sha256(record)
    )
    assert result["database_mutation_performed"] is False
    assert result["release_authorized"] is False
    assert result["owner_principal_id"] == "private-owner@example.invalid"


def test_publication_record_discovery_rejects_non_ascii_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _publication_record()
    record["principal_id"] = "nön-ascii@example.invalid"
    monkeypatch.setattr(
        release,
        "load_unique_property_search_run_record_for_discovery",
        lambda **_kwargs: record,
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_discovery_owner_invalid",
    ):
        release.discover_prater_ai_panorama_publication_record()


def test_prater_artifact_preflight_is_read_only_and_database_free(
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
        artifact_preflight_only: bool,
    ) -> dict[str, object]:
        observed.update(request)
        assert apply is False
        assert publication_admission is admission
        assert artifact_preflight_only is True
        return {
            "status": "artifact_preflight_validated",
            "release_eligible": False,
            "private_values_redacted": True,
        }

    monkeypatch.setattr(release, "install_sealed_ai_panorama_bundle", _install)

    receipt = release.run_prater_ai_panorama_artifact_preflight(admission)

    assert observed["source_bundle"] == str(
        release.PRATER_CONTROLLER_ARTIFACT_ROOT / release.PRATER_ARTIFACT_RELPATH
    )
    assert observed["materialization_receipt_path"] == str(
        release.PRATER_CONTROLLER_ARTIFACT_ROOT
        / release.PRATER_MATERIALIZATION_RECEIPT_RELPATH
    )
    assert receipt["status"] == "preflight_passed"
    assert receipt["nonce_consumed"] is False
    assert receipt["database_access_performed"] is False
    assert receipt["release_eligible"] is False


def test_prater_apply_finishes_owner_receipt_cas_binding(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: dict[str, object],
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
        assert apply is True
        assert publication_admission is admission
        return {
            "status": "installed",
            "release_eligible": True,
            "publication_binding_verified": True,
            "publication_binding_status": "applied",
            "publication_binding_before_sha256": "1" * 64,
            "publication_binding_after_sha256": "2" * 64,
            "private_values_redacted": True,
        }

    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        _install,
    )

    receipt = release.run_prater_ai_panorama_release(admission, apply=True)

    assert observed["principal_id"] == admission.authenticated_principal_id
    assert observed["search_run_id"] == release.PRATER_SEARCH_RUN_ID
    assert observed["candidate_ref"] == release.PRATER_CANDIDATE_REF
    assert observed["public_control_url"] == release.PRATER_PUBLIC_CONTROL_URL
    assert observed["expected_publication_record_sha256"] == "1" * 64
    assert observed["publication_binding_expected_before_sha256"] == "1" * 64
    assert observed["publication_binding_expected_after_sha256"] == "2" * 64
    prepared = _verified_profile["prepared_evidence"]
    assert isinstance(prepared, dict)
    assert prepared["publication_binding_preparation"] == {
        "status": "change-required",
        "publication_binding_expected_before_sha256": "1" * 64,
        "publication_binding_expected_after_sha256": "2" * 64,
        "publication_binding_bound_at": "2026-07-24T12:00:00+00:00",
        "database_mutation_performed": False,
        "private_values_redacted": True,
    }
    assert receipt["status"] == "released"
    assert receipt["release_eligible"] is True
    assert receipt["binding_status"] == "applied"
    assert receipt["binding_receipt"]["before_sha256"] == "1" * 64


def test_prater_apply_journals_clean_failure_without_public_target(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: None,
) -> None:
    admission = _admission()
    absent = {
        "state": "absent",
        "target_relpath": release.PRATER_SLUG,
        "public_root_device": 41,
        "public_root_inode": 42,
        "reserved_entry_count": 0,
        "reserved_entries_sha256": "0" * 64,
    }
    monkeypatch.setattr(release, "_target_manifest", lambda _verified: dict(absent))
    events: list[str] = []
    monkeypatch.setattr(
        release,
        "finish_ai_panorama_install_operation",
        lambda _operation, *, event, evidence: events.append(event) or "9" * 64,
    )
    failure = AiPanoramaIntakeError(
        "ai_panorama_publication_binding_store_rejected"
    )
    failure.publication_outcome = "uncommitted"
    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_publication_binding_store_rejected",
    ):
        release.run_prater_ai_panorama_release(admission, apply=True)

    assert events == ["failed-clean"]


def test_prater_apply_unknown_db_outcome_is_never_failed_clean(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: None,
) -> None:
    admission = _admission()
    absent = {
        "state": "absent",
        "target_relpath": release.PRATER_SLUG,
        "public_root_device": 41,
        "public_root_inode": 42,
        "reserved_entry_count": 0,
        "reserved_entries_sha256": "0" * 64,
    }
    monkeypatch.setattr(
        release,
        "_target_manifest",
        lambda _verified: dict(absent),
    )
    events: list[str] = []
    monkeypatch.setattr(
        release,
        "finish_ai_panorama_install_operation",
        lambda _operation, *, event, evidence: events.append(event)
        or "9" * 64,
    )
    failure = AiPanoramaIntakeError(
        "ai_panorama_publication_transaction_failed"
    )
    failure.publication_outcome = "unknown"
    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_recovery_required",
    ):
        release.run_prater_ai_panorama_release(admission, apply=True)

    assert events == ["recovery-required"]


def test_prater_apply_journals_exact_compensation_as_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: None,
) -> None:
    admission = _admission()
    absent = {
        "state": "absent",
        "target_relpath": release.PRATER_SLUG,
        "public_root_device": 41,
        "public_root_inode": 42,
        "reserved_entry_count": 0,
        "reserved_entries_sha256": "0" * 64,
    }
    monkeypatch.setattr(release, "_target_manifest", lambda _verified: dict(absent))
    events: list[str] = []
    monkeypatch.setattr(
        release,
        "finish_ai_panorama_install_operation",
        lambda _operation, *, event, evidence: events.append(event) or "9" * 64,
    )
    failure = AiPanoramaIntakeError("ai_panorama_stage_fsync_failed")
    failure.rollback_performed = True
    failure.publication_outcome = "uncommitted"
    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_stage_fsync_failed",
    ):
        release.run_prater_ai_panorama_release(admission, apply=True)

    assert events == ["rolled-back"]


def test_prater_apply_marks_commit_exit_ambiguity_recovery_required_even_clean(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: None,
) -> None:
    admission = _admission()
    absent = {
        "state": "absent",
        "target_relpath": release.PRATER_SLUG,
        "public_root_device": 41,
        "public_root_inode": 42,
        "reserved_entry_count": 0,
        "reserved_entries_sha256": "0" * 64,
    }
    monkeypatch.setattr(release, "_target_manifest", lambda _verified: dict(absent))
    events: list[str] = []
    monkeypatch.setattr(
        release,
        "finish_ai_panorama_install_operation",
        lambda _operation, *, event, evidence: events.append(event) or "9" * 64,
    )
    failure = AiPanoramaIntakeError(
        "ai_panorama_publication_transaction_failed"
    )
    failure.rollback_performed = True
    failure.commit_outcome_ambiguous = True
    failure.publication_outcome = "ambiguous"
    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_recovery_required",
    ):
        release.run_prater_ai_panorama_release(admission, apply=True)

    assert events == ["recovery-required"]


def test_prater_apply_marks_ambiguous_public_state_recovery_required(
    monkeypatch: pytest.MonkeyPatch,
    _verified_profile: None,
) -> None:
    admission = _admission()
    manifests = iter(
        (
            {
                "state": "absent",
                "target_relpath": release.PRATER_SLUG,
                "public_root_device": 41,
                "public_root_inode": 42,
                "reserved_entry_count": 0,
                "reserved_entries_sha256": "0" * 64,
            },
            {
                "state": "present",
                "target_relpath": release.PRATER_SLUG,
                "public_root_device": 41,
                "public_root_inode": 42,
                "tree_sha256": "7" * 64,
                "tour_private_sha256": "8" * 64,
                "file_count": 9,
                "total_bytes": 1234,
                "reserved_entry_count": 0,
                "reserved_entries_sha256": "0" * 64,
            },
        )
    )
    monkeypatch.setattr(release, "_target_manifest", lambda _verified: next(manifests))
    events: list[str] = []
    monkeypatch.setattr(
        release,
        "finish_ai_panorama_install_operation",
        lambda _operation, *, event, evidence: events.append(event) or "9" * 64,
    )
    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AiPanoramaIntakeError(
                "ai_panorama_publication_transaction_failed"
            )
        ),
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_recovery_required",
    ):
        release.run_prater_ai_panorama_release(admission, apply=True)

    assert events == ["recovery-required"]


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


def test_target_manifest_hashes_private_receipt_without_disclosing_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / release.PRATER_SLUG
    target.mkdir()
    (target / "tour.json").write_text('{"slug":"public"}\n', encoding="utf-8")
    private_value = "private-owner@example.invalid"
    (target / "tour.private.json").write_text(
        json.dumps({"principal_id": private_value}),
        encoding="utf-8",
    )
    proof = target / "proof"
    proof.mkdir()
    (proof / "browser.png").write_bytes(b"browser-proof")
    details = tmp_path.stat()
    verified = _admission(
        public_tour_dir=tmp_path,
        public_tour_root_device=details.st_dev,
        public_tour_root_inode=details.st_ino,
    )

    manifest = release._target_manifest(verified)

    assert manifest["state"] == "present"
    assert manifest["file_count"] == 3
    assert len(str(manifest["tree_sha256"])) == 64
    assert len(str(manifest["tour_private_sha256"])) == 64
    assert private_value not in json.dumps(manifest, sort_keys=True)


def test_target_manifest_rejects_symlink_and_unstable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / release.PRATER_SLUG
    target.mkdir()
    outside = target / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (target / "tour.json").symlink_to(outside)
    details = tmp_path.stat()
    verified = _admission(
        public_tour_dir=tmp_path,
        public_tour_root_device=details.st_dev,
        public_tour_root_inode=details.st_ino,
    )
    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_target_manifest_invalid",
    ):
        release._target_manifest(verified)

    (target / "tour.json").unlink()
    (target / "tour.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        release,
        "_stable_target_file_sha256",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AiPanoramaIntakeError(
                "ai_panorama_prater_target_manifest_invalid"
            )
        ),
    )
    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_target_manifest_invalid",
    ):
        release._target_manifest(verified)


def test_target_manifest_exposes_leftover_reserved_operation_state(
    tmp_path: Path,
) -> None:
    leftover = (
        tmp_path
        / f".{release.PRATER_SLUG}.ai-rollback-{'d' * 32}"
    )
    leftover.mkdir()
    details = tmp_path.stat()
    verified = _admission(
        public_tour_dir=tmp_path,
        public_tour_root_device=details.st_dev,
        public_tour_root_inode=details.st_ino,
    )

    manifest = release._target_manifest(verified)

    assert manifest["state"] == "absent"
    assert manifest["reserved_entry_count"] == 1
    assert len(str(manifest["reserved_entries_sha256"])) == 64


def test_target_manifest_rejects_every_unreserved_governed_root_entry(
    tmp_path: Path,
) -> None:
    (tmp_path / "other-tour").mkdir()
    details = tmp_path.stat()
    verified = _admission(
        public_tour_dir=tmp_path,
        public_tour_root_device=details.st_dev,
        public_tour_root_inode=details.st_ino,
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_public_root_entry_forbidden",
    ):
        release._target_manifest(verified)


def test_target_manifest_enforces_bounded_file_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / release.PRATER_SLUG
    target.mkdir()
    (target / "one.json").write_text("{}\n", encoding="utf-8")
    (target / "two.json").write_text("{}\n", encoding="utf-8")
    details = tmp_path.stat()
    verified = _admission(
        public_tour_dir=tmp_path,
        public_tour_root_device=details.st_dev,
        public_tour_root_inode=details.st_ino,
    )
    monkeypatch.setattr(release, "_TARGET_MAX_FILES", 1)

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_prater_target_manifest_budget_exceeded",
    ):
        release._target_manifest(verified)


@pytest.mark.parametrize("read_only_value", ("on", "true"))
def test_recovery_snapshot_sets_read_only_before_every_other_cursor_statement(
    monkeypatch: pytest.MonkeyPatch,
    read_only_value: str,
) -> None:
    statements: list[tuple[str, object]] = []

    class _Cursor:
        def __init__(self) -> None:
            self.rows = iter(((read_only_value,), (False,)))

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(
            self,
            statement: str,
            parameters: object = None,
        ) -> None:
            statements.append((statement.strip(), parameters))

        def fetchone(self) -> tuple[object, ...]:
            return next(self.rows)

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    connection = _Connection()

    @contextlib.contextmanager
    def _connect():  # type: ignore[no-untyped-def]
        yield connection

    @contextlib.contextmanager
    def _transaction(observed: object):  # type: ignore[no-untyped-def]
        assert observed is connection
        yield

    monkeypatch.setattr(
        storage,
        "_property_search_run_database_url",
        lambda: "postgresql://fixed.invalid/propertyquarry",
    )
    monkeypatch.setattr(
        storage,
        "_property_search_principal_key",
        lambda _principal: "principal-key",
    )
    monkeypatch.setattr(storage, "_property_search_run_connect", _connect)
    monkeypatch.setattr(
        storage,
        "_property_search_run_transaction",
        _transaction,
    )
    monkeypatch.setattr(
        storage,
        "_require_property_search_run_schema",
        lambda: pytest.fail("recovery must not run schema setup"),
    )
    monkeypatch.setattr(
        storage,
        "_set_property_search_writer_contract",
        lambda _cursor: pytest.fail(
            "recovery must not install a writer contract"
        ),
    )

    with storage.property_account_publication_recovery_observation(
        "owner@example.invalid",
        run_id=release.PRATER_SEARCH_RUN_ID,
    ) as yielded:
        assert yielded is connection

    assert statements[0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        None,
    )
    assert statements[1] == ("SHOW transaction_read_only", None)
    assert "property_search_assert_erasure_key" in statements[2][0]
    assert "pg_advisory_xact_lock" in statements[3][0]
    assert "property_search_erasure_fences" in statements[4][0]


def test_recovery_snapshot_rejects_non_read_only_transaction_before_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class _Cursor:
        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(
            self,
            statement: str,
            _parameters: object = None,
        ) -> None:
            statements.append(statement.strip())

        def fetchone(self) -> tuple[str]:
            return ("off",)

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

    @contextlib.contextmanager
    def _connect():  # type: ignore[no-untyped-def]
        yield _Connection()

    @contextlib.contextmanager
    def _transaction(_connection: object):  # type: ignore[no-untyped-def]
        yield

    monkeypatch.setattr(
        storage,
        "_property_search_run_database_url",
        lambda: "postgresql://fixed.invalid/propertyquarry",
    )
    monkeypatch.setattr(
        storage,
        "_property_search_principal_key",
        lambda _principal: "principal-key",
    )
    monkeypatch.setattr(storage, "_property_search_run_connect", _connect)
    monkeypatch.setattr(
        storage,
        "_property_search_run_transaction",
        _transaction,
    )

    with pytest.raises(
        RuntimeError,
        match="property_account_publication_recovery_not_read_only",
    ):
        with storage.property_account_publication_recovery_observation(
            "owner@example.invalid",
            run_id=release.PRATER_SEARCH_RUN_ID,
        ):
            pytest.fail("non-read-only transaction must not yield")

    assert statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY",
        "SHOW transaction_read_only",
    ]


def test_historical_classifier_holds_read_only_db_lock_through_terminal_append(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _historical_expected()
    historical = SimpleNamespace(
        ledger_instance_id="a" * 32,
        ledger_sequence=1,
        ledger_entry_sha256="b" * 64,
    )
    operation = SimpleNamespace(
        state="consumed-no-operation",
        operation_id="c" * 64,
        permit_sha256="d" * 64,
        request_id_sha256="e" * 64,
        nonce_sha256="f" * 64,
        context_sha256="1" * 64,
        prepared_entry_sha256="",
        prepared_evidence_sha256="",
        prepared_evidence={},
        terminal_event="",
        terminal_entry_sha256="",
        terminal_evidence_sha256="",
        terminal_evidence={},
        historical_consumption=historical,
    )
    held = {"value": False}
    observed: dict[str, object] = {}

    @contextlib.contextmanager
    def _recovery_context(
        principal_id: str,
        *,
        run_id: str,
    ):  # type: ignore[no-untyped-def]
        assert principal_id == expected.owner_principal_id
        assert run_id == release.PRATER_SEARCH_RUN_ID
        held["value"] = True
        try:
            yield object()
        finally:
            held["value"] = False

    def _load_record(**kwargs: object) -> dict[str, object]:
        assert held["value"] is True
        observed["record_kwargs"] = kwargs
        return {"record": "read-only-snapshot"}

    target_manifest = {
        "state": "absent",
        "target_relpath": release.PRATER_SLUG,
        "public_root_device": expected.public_tour_root_device,
        "public_root_inode": expected.public_tour_root_inode,
        "reserved_entry_count": 0,
        "reserved_entries_sha256": "0" * 64,
    }
    target_identity = {
        "state": "absent",
        "source_tree_sha256": release.PRATER_SOURCE_TREE_SHA256,
        "source_tour_sha256": release.PRATER_TOUR_SHA256,
        "core_manifest_sha256": release.PRATER_CORE_MANIFEST_SHA256,
        "public_root_device": expected.public_tour_root_device,
        "public_root_inode": expected.public_tour_root_inode,
        "private_values_redacted": True,
    }

    def _append(
        permit_relpath: str,
        append_expected: object,
        append_operation: object,
        *,
        evidence: dict[str, object],
    ) -> str:
        assert held["value"] is True
        assert permit_relpath.endswith(f"{expected.request_id}.v2.json")
        assert append_expected is expected
        assert append_operation is operation
        assert evidence["observed_publication_binding_exact"] is False
        observed["terminal_evidence"] = evidence
        return "9" * 64

    monkeypatch.setattr(
        release,
        "load_historical_ai_panorama_install_operation",
        lambda *_args: operation,
    )
    monkeypatch.setattr(
        release,
        "property_account_publication_recovery_observation",
        _recovery_context,
    )
    monkeypatch.setattr(
        release,
        "load_property_search_run_record_for_publication",
        _load_record,
    )
    monkeypatch.setattr(
        release,
        "property_search_run_record_sha256",
        lambda _record: expected.expected_publication_record_sha256,
    )
    monkeypatch.setattr(
        release,
        "_target_manifest",
        lambda _holder: (
            pytest.fail("manifest observed outside DB lock")
            if not held["value"]
            else dict(target_manifest)
        ),
    )
    monkeypatch.setattr(
        release,
        "inspect_ai_panorama_historical_publication_target",
        lambda *_args, **_kwargs: (
            pytest.fail("target observed outside DB lock")
            if not held["value"]
            else dict(target_identity)
        ),
    )
    monkeypatch.setattr(
        release,
        "exact_property_search_candidate_tour_binding_receipt",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        release,
        "_record_consumed_without_operation_failed_clean",
        _append,
    )

    result = release.recover_prater_ai_panorama_historical_operation(
        f"prater-ai-panorama-install-{expected.request_id}.v2.json",
        expected,
    )

    assert held["value"] is False
    assert result["classification"] == "failed-clean"
    assert result["database_mutation_performed"] is False
    assert result["public_target_mutation_performed"] is False
    record_kwargs = observed["record_kwargs"]
    assert isinstance(record_kwargs, dict)
    assert record_kwargs["for_update"] is False


def test_historical_classifier_transient_db_failure_writes_no_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = _historical_expected()
    operation = SimpleNamespace(
        state="consumed-no-operation",
        operation_id="c" * 64,
        permit_sha256="d" * 64,
        request_id_sha256="e" * 64,
        nonce_sha256="f" * 64,
        context_sha256="1" * 64,
        prepared_entry_sha256="",
        prepared_evidence_sha256="",
        prepared_evidence={},
        terminal_event="",
        terminal_entry_sha256="",
        terminal_evidence_sha256="",
        terminal_evidence={},
        historical_consumption=SimpleNamespace(
            ledger_instance_id="a" * 32,
            ledger_sequence=1,
            ledger_entry_sha256="b" * 64,
        ),
    )

    @contextlib.contextmanager
    def _unavailable(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        raise RuntimeError("database-unavailable")
        yield

    monkeypatch.setattr(
        release,
        "load_historical_ai_panorama_install_operation",
        lambda *_args: operation,
    )
    monkeypatch.setattr(
        release,
        "property_account_publication_recovery_observation",
        _unavailable,
    )
    monkeypatch.setattr(
        release,
        "_record_consumed_without_operation_failed_clean",
        lambda *_args, **_kwargs: pytest.fail(
            "transient observation failure must not append a terminal"
        ),
    )

    with pytest.raises(RuntimeError, match="database-unavailable"):
        release.recover_prater_ai_panorama_historical_operation(
            f"prater-ai-panorama-install-{expected.request_id}.v2.json",
            expected,
        )
