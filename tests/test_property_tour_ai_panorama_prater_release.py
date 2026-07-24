from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

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
def _verified_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        release,
        "_revalidate_ai_panorama_install_admission",
        lambda admission, *, require_consumed: admission,
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
    monkeypatch.setattr(
        release,
        "begin_ai_panorama_install_operation",
        lambda *_args, **_kwargs: SimpleNamespace(operation_id="operation"),
    )
    monkeypatch.setattr(
        release,
        "finish_ai_panorama_install_operation",
        lambda *_args, **_kwargs: "9" * 64,
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
    monkeypatch.setattr(
        release,
        "install_sealed_ai_panorama_bundle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AiPanoramaIntakeError(
                "ai_panorama_publication_binding_store_rejected"
            )
        ),
    )

    with pytest.raises(
        AiPanoramaIntakeError,
        match="ai_panorama_publication_binding_store_rejected",
    ):
        release.run_prater_ai_panorama_release(admission, apply=True)

    assert events == ["failed-clean"]


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
