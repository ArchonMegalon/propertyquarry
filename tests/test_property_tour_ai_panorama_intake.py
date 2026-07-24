from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from app.product import property_search_storage as storage
from app.product import property_tour_ai_panorama_intake as intake
from app.product.property_search_tour_binding import (
    property_search_run_record_sha256,
    property_search_source_url_sha256,
)
from scripts import install_ai_panorama_tour_bundle as installer_script


_REAL_BIND_CANDIDATE_IN_PUBLICATION_TRANSACTION = (
    intake._bind_candidate_in_publication_transaction
)


LISTING_URL = "https://www.willhaben.at/iad/immobilien/d/1807240910/"
SLUG = "prater-ai-360-candidate-1"
PRINCIPAL = "principal-secret@example.invalid"
RUN_ID = "run-123"
CANDIDATE_REF = "candidate-private-9"
SOURCE_REF = "property-scout:1807240910"
EXTERNAL_ID = "1807240910"


def _authorization_record(**updates: object) -> dict[str, object]:
    candidate = {
        "candidate_ref": CANDIDATE_REF,
        "property_url": LISTING_URL,
        "listing_id": EXTERNAL_ID,
        "external_id": EXTERNAL_ID,
        "source_ref": SOURCE_REF,
        "platform": "willhaben",
        "source_label": "Willhaben",
    }
    record: dict[str, object] = {
        "run_id": RUN_ID,
        "principal_id": PRINCIPAL,
        "status": "processed",
        "summary": {"ranked_candidates": [candidate]},
    }
    record.update(updates)
    return record


@pytest.fixture(autouse=True)
def _strict_contract_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EA_PUBLIC_TOUR_DIR", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR", raising=False)

    def _contract(*, bundle_dir: Path, payload: dict[str, object], mode: str = "full") -> dict[str, object]:
        return {
            "ready": True,
            "reason": "",
            "representation_kind": "ai_panorama_360",
            "property_url_sha256": str(payload.get("property_url_sha256") or ""),
            "core_manifest_sha256": intake._semantic_manifest_sha256(payload),
        }

    monkeypatch.setattr(intake, "_hosted_property_tour_ai_panorama_contract", _contract)
    monkeypatch.setattr(
        intake,
        "load_property_search_run_record_for_publication",
        lambda **_kwargs: _authorization_record(),
    )
    monkeypatch.setattr(
        intake,
        "_revalidate_ai_panorama_install_admission",
        lambda admission, *, require_consumed: (
            admission
            if isinstance(admission, SimpleNamespace)
            and (
                not require_consumed
                or admission.nonce_consumed is True
            )
            else (_ for _ in ()).throw(
                intake.AiPanoramaIntakeError(
                    "ai_panorama_controller_admission_invalid"
                )
            )
        ),
    )
    monkeypatch.setattr(
        intake,
        "_bind_candidate_in_publication_transaction",
        lambda **_kwargs: {
            "publication_binding_verified": "true",
            "publication_binding_status": "applied",
            "publication_binding_before_sha256": "1" * 64,
            "publication_binding_after_sha256": "2" * 64,
        },
    )


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    path.chmod(0o644)


def _make_bundle(
    root: Path,
    *,
    slug: str = SLUG,
    directory_name: str = "sealed-bundle",
) -> Path:
    bundle = root / directory_name
    bundle.mkdir(parents=True, mode=0o755)
    property_url_sha256 = property_search_source_url_sha256(LISTING_URL)
    browser_receipt = {
        "desktop": {"screenshot_relpath": "proof/browser-desktop.png"},
        "mobile": {"screenshot_relpath": "proof/browser-mobile.png"},
        "dollhouse": {"screenshot_relpath": "proof/browser-dollhouse.png"},
    }
    provenance = {
        "contract_name": "propertyquarry.ai_panorama_provenance.v1",
        "property_binding_kind": "willhaben_source_listing_url_sha256",
        "property_url_sha256": property_url_sha256,
    }
    payload = {
        "slug": slug,
        "publication_status": "ready",
        "property_url_sha256": property_url_sha256,
        "walkable_scene": {
            "representation_kind": "ai_reconstruction",
            "floorplan_relpath": "floorplan.webp",
            "scenes": [
                {
                    "id": "living-room",
                    "asset_relpath": "panoramas/living-room.jpg",
                }
            ],
            "acceptance": {
                "provenance_relpath": "proof/provenance.json",
                "browser_receipt_relpath": "proof/browser-proof.json",
            },
        },
    }
    _write(bundle / "tour.json", json.dumps(payload, sort_keys=True).encode("utf-8"))
    _write(bundle / "floorplan.webp", b"floorplan")
    _write(bundle / "panoramas/living-room.jpg", b"panorama")
    _write(bundle / "proof/provenance.json", json.dumps(provenance).encode("utf-8"))
    _write(bundle / "proof/browser-proof.json", json.dumps(browser_receipt).encode("utf-8"))
    _write(bundle / "proof/browser-desktop.png", b"desktop")
    _write(bundle / "proof/browser-mobile.png", b"mobile")
    _write(bundle / "proof/browser-dollhouse.png", b"dollhouse")
    return bundle


def _request(bundle: Path, public_dir: Path, **overrides: object) -> dict[str, object]:
    os.environ["EA_PUBLIC_TOUR_DIR"] = str(public_dir)
    os.environ["PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR"] = str(bundle.parent)
    request: dict[str, object] = {
        "contract": intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT,
        "source_bundle": str(bundle),
        "public_tour_dir": str(public_dir),
        "expected_slug": SLUG,
        "principal_id": PRINCIPAL,
        "search_run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "listing_url": LISTING_URL,
        "provider_key": "willhaben",
        "source_ref": SOURCE_REF,
        "external_id": EXTERNAL_ID,
    }
    request.update(overrides)
    return request


def _hash_bound_request(bundle: Path, public_dir: Path, **overrides: object) -> dict[str, object]:
    request = _request(bundle, public_dir, **overrides)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    return request


def _canonical(value: dict[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _v2_lineage_request(
    bundle: Path,
    public_dir: Path,
) -> tuple[dict[str, object], Path, Path]:
    payload = json.loads((bundle / "tour.json").read_text(encoding="utf-8"))
    core_manifest_sha256 = intake._semantic_manifest_sha256(payload)
    source_tree_sha256 = "a" * 64
    bundle_material_sha256 = "b" * 64
    marker = {
        "contract_name": intake.AI_PANORAMA_CANDIDATE_MARKER_CONTRACT,
        "tree_snapshot_algorithm": "regular-files-and-directories.sorted.v2",
        "slug": SLUG,
        "source_tree_sha256": source_tree_sha256,
        "source_file_count": 5,
        "source_size_bytes": 1234,
        "core_manifest_sha256": core_manifest_sha256,
        "bundle_material_sha256": bundle_material_sha256,
    }
    marker_path = bundle.parent / intake.AI_PANORAMA_CANDIDATE_MARKER_RELPATH
    marker_path.write_bytes(_canonical(marker))
    marker_path.chmod(0o600)
    marker_sha256 = hashlib.sha256(marker_path.read_bytes()).hexdigest()
    installer_identity = intake.snapshot_ai_panorama_installer_source_bundle(bundle)
    receipt: dict[str, object] = {
        "contract_name": intake.AI_PANORAMA_MATERIALIZATION_RECEIPT_CONTRACT,
        "status": "pass",
        "slug": SLUG,
        "candidate_public_root": str(bundle.parent),
        "candidate_bundle_relpath": SLUG,
        "candidate_marker_relpath": intake.AI_PANORAMA_CANDIDATE_MARKER_RELPATH,
        "candidate_marker_sha256": marker_sha256,
        "candidate_tree_sha256": "c" * 64,
        "candidate_file_count": installer_identity.file_count,
        "candidate_size_bytes": installer_identity.total_bytes,
        "tree_snapshot_algorithm": "regular-files-and-directories.sorted.v2",
        "core_manifest_sha256": core_manifest_sha256,
        "bundle_material_sha256": bundle_material_sha256,
        "source_tree_sha256": source_tree_sha256,
        "source_file_count": 5,
        "source_size_bytes": 1234,
        "source_copy_identity_verified": True,
        "source_bundle_unchanged": True,
        "source_unchanged_after_candidate_seal": True,
        "production_mutation_performed": False,
        "controller_bypass_performed": False,
        "candidate_identity_rechecked_after_receipt_write": True,
        "installer_source_identity_contract": installer_identity.contract_name,
        "installer_source_tree_algorithm": installer_identity.tree_algorithm,
        "installer_source_relative_root": installer_identity.relative_root,
        "installer_source_relative_path_semantics": (
            installer_identity.relative_path_semantics
        ),
        "installer_source_tree_sha256": installer_identity.tree_sha256,
        "installer_source_tour_sha256": installer_identity.tour_sha256,
        "installer_source_file_count": installer_identity.file_count,
        "installer_source_total_bytes": installer_identity.total_bytes,
        "tour_manifest_sha256": installer_identity.tour_sha256,
        "external_receipt": {
            "written": True,
            "source_unchanged_post_write": True,
            "candidate_unchanged_post_write": True,
        },
    }
    receipt_path = bundle.parent.parent / "materialization-receipt.json"
    receipt_path.write_bytes(_canonical(receipt))
    receipt_path.chmod(0o600)
    request = _request(
        bundle,
        public_dir,
        contract=intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
        materialization_receipt_path=str(receipt_path),
        expected_materialization_receipt_sha256=hashlib.sha256(
            receipt_path.read_bytes()
        ).hexdigest(),
        expected_candidate_marker_sha256=marker_sha256,
    )
    return request, receipt_path, marker_path


def _controller_admission(
    request: dict[str, object],
    **overrides: object,
) -> object:
    source_bundle = Path(str(request.get("source_bundle") or ""))
    source_manifest = json.loads(
        (source_bundle / "tour.json").read_text(encoding="utf-8")
    )
    request.setdefault(
        "expected_core_manifest_sha256",
        intake._semantic_manifest_sha256(source_manifest),
    )
    request.setdefault(
        "expected_publication_record_sha256",
        property_search_run_record_sha256(_authorization_record()),
    )
    request.setdefault(
        "public_control_url",
        f"https://propertyquarry.com/tours/{request['expected_slug']}/control",
    )
    public_tour_dir = Path(str(request.get("public_tour_dir") or ""))
    public_details = public_tour_dir.stat()
    values: dict[str, object] = {
        "authenticated_principal_id": request.get("principal_id"),
        "search_run_id": request.get("search_run_id"),
        "candidate_ref": request.get("candidate_ref"),
        "external_id": request.get("external_id"),
        "listing_url": request.get("listing_url"),
        "source_ref": request.get("source_ref"),
        "provider_key": request.get("provider_key"),
        "expected_slug": request.get("expected_slug"),
        "public_control_url": request.get("public_control_url"),
        "expected_source_tree_sha256": request.get("expected_source_tree_sha256"),
        "expected_tour_sha256": request.get("expected_tour_sha256"),
        "expected_core_manifest_sha256": request.get(
            "expected_core_manifest_sha256"
        ),
        "expected_materialization_receipt_sha256": request.get(
            "expected_materialization_receipt_sha256"
        ),
        "expected_candidate_marker_sha256": request.get(
            "expected_candidate_marker_sha256"
        ),
        "expected_publication_record_sha256": request.get(
            "expected_publication_record_sha256"
        ),
        "source_bundle": source_bundle,
        "materialization_receipt_path": Path(
            str(request.get("materialization_receipt_path") or "")
        ),
        "incoming_root": Path(
            str(os.environ["PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR"])
        ),
        "public_tour_dir": public_tour_dir,
        "public_tour_volume_name": (
            "property_propertyquarry_governed_public_tours"
        ),
        "public_tour_mount_target": (
            "/data/governed_public_property_tours"
        ),
        "public_tour_root_device": int(public_details.st_dev),
        "public_tour_root_inode": int(public_details.st_ino),
        "permit_sha256": "d" * 64,
        "nonce_consumed": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_private_request_loader_requires_owner_only_regular_file(tmp_path: Path) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps({"contract": intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT}),
        encoding="utf-8",
    )
    request_path.chmod(0o644)
    with pytest.raises(intake.AiPanoramaIntakeError, match="request_permissions_invalid"):
        intake.load_private_ai_panorama_install_request(request_path)

    request_path.chmod(0o600)
    loaded = intake.load_private_ai_panorama_install_request(request_path)
    assert loaded["contract"] == intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT

    link_path = tmp_path / "request-link.json"
    link_path.symlink_to(request_path)
    with pytest.raises(intake.AiPanoramaIntakeError, match="request_permissions_invalid"):
        intake.load_private_ai_panorama_install_request(link_path)


def test_private_request_revalidates_the_opened_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_path = tmp_path / "request.json"
    replacement_path = tmp_path / "replacement.json"
    payload = json.dumps(
        {"contract": intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT}
    )
    request_path.write_text(payload, encoding="utf-8")
    request_path.chmod(0o600)
    replacement_path.write_text(
        json.dumps({"contract": intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2}),
        encoding="utf-8",
    )
    replacement_path.chmod(0o600)
    original_open = os.open

    def _swapped_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        target = replacement_path if Path(path) == request_path else path
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(intake.os, "open", _swapped_open)
    with pytest.raises(intake.AiPanoramaIntakeError, match="request_permissions_invalid"):
        intake.load_private_ai_panorama_install_request(request_path)


def test_dry_run_is_default_hash_discovery_and_redacts_private_values(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    receipt = intake.install_sealed_ai_panorama_bundle(_request(bundle, public_dir))

    assert receipt["status"] == "validated"
    assert receipt["mode"] == "dry_run"
    assert receipt["applied"] is False
    assert receipt["install_request_contract"] == (
        intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V1
    )
    assert receipt["materialization_lineage_verified"] is False
    assert receipt["release_eligible"] is False
    assert receipt["source_file_count"] == 8
    assert len(str(receipt["source_tree_sha256"])) == 64
    rendered = json.dumps(receipt, sort_keys=True)
    for private_value in (PRINCIPAL, RUN_ID, CANDIDATE_REF, LISTING_URL, SOURCE_REF, EXTERNAL_ID):
        assert private_value not in rendered
    assert not (public_dir / SLUG).exists()


def test_v1_optional_hash_whitespace_remains_backward_compatible(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    receipt = intake.install_sealed_ai_panorama_bundle(
        _request(
            bundle,
            public_dir,
            expected_source_tree_sha256="   ",
            expected_tour_sha256="\t",
        )
    )

    assert receipt["status"] == "validated"
    assert receipt["release_eligible"] is False


def test_v1_is_strictly_dry_run_only(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source")
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request = _hash_bound_request(bundle, public_dir)

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_v1_apply_forbidden",
    ):
        intake.install_sealed_ai_panorama_bundle(request, apply=True)
    assert not (public_dir / SLUG).exists()


@pytest.mark.parametrize("lineage_fields", (0, 1))
def test_v2_requires_complete_materialization_lineage(
    tmp_path: Path,
    lineage_fields: int,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    overrides: dict[str, object] = {
        "contract": intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
    }
    if lineage_fields:
        overrides["expected_candidate_marker_sha256"] = "0" * 64

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="materialization_lineage_required",
    ):
        intake.install_sealed_ai_panorama_bundle(
            _request(bundle, public_dir, **overrides)
        )


def test_v2_exact_materialization_lineage_is_not_release_eligible_without_admission(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, receipt_path, marker_path = _v2_lineage_request(bundle, public_dir)

    receipt = intake.install_sealed_ai_panorama_bundle(request)
    identity = intake.snapshot_ai_panorama_installer_source_bundle(bundle)

    assert receipt["install_request_contract"] == (
        intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2
    )
    assert receipt["materialization_lineage_verified"] is True
    assert receipt["release_eligible"] is False
    assert receipt["controller_permit_verified"] is False
    assert receipt["publication_authorization_verified"] is True
    assert receipt["principal_binding_verified"] is True
    assert receipt["run_binding_verified"] is True
    assert receipt["candidate_binding_verified"] is True
    assert receipt["listing_identity_verified"] is True
    assert receipt["source_identity_verified"] is True
    assert receipt["run_terminal_verified"] is True
    assert len(str(receipt["publication_authorization_record_sha256"])) == 64
    assert receipt["source_tree_sha256"] == identity.tree_sha256
    assert receipt["source_tour_sha256"] == identity.tour_sha256
    assert receipt["materialization_receipt_sha256"] == hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()
    assert receipt["candidate_marker_sha256"] == hashlib.sha256(
        marker_path.read_bytes()
    ).hexdigest()


def test_v2_protected_dry_run_can_report_release_eligible(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    source_identity = intake.snapshot_ai_panorama_installer_source_bundle(bundle)
    request.update(
        {
            "expected_source_tree_sha256": source_identity.tree_sha256,
            "expected_tour_sha256": source_identity.tour_sha256,
        }
    )
    admission = _controller_admission(request, nonce_consumed=False)

    receipt = intake.install_sealed_ai_panorama_bundle(
        request,
        publication_admission=admission,
    )

    assert receipt["status"] == "validated"
    assert receipt["release_eligible"] is True
    assert receipt["controller_permit_verified"] is True
    assert receipt["controller_nonce_consumed"] is False
    assert not (public_dir / SLUG).exists()


def test_governed_artifact_preflight_accepts_only_exact_sealed_relocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sealed_root = tmp_path / "sealed-root"
    bundle = _make_bundle(
        sealed_root / "bundle",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, original_receipt_path, marker_path = _v2_lineage_request(
        bundle,
        public_dir,
    )
    historical_root = Path("/historical/propertyquarry/prater-source")
    receipt = json.loads(original_receipt_path.read_text(encoding="utf-8"))
    receipt["candidate_public_root"] = str(historical_root)
    receipt_path = sealed_root / "materialization.receipt.json"
    receipt_path.write_bytes(_canonical(receipt))
    receipt_path.chmod(0o600)
    original_receipt_path.unlink()
    source_identity = intake.snapshot_ai_panorama_installer_source_bundle(bundle)
    marker_sha256 = hashlib.sha256(marker_path.read_bytes()).hexdigest()
    receipt_sha256 = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    core_sha256 = intake._semantic_manifest_sha256(
        json.loads((bundle / "tour.json").read_text(encoding="utf-8"))
    )
    request.update(
        {
            "materialization_receipt_path": str(receipt_path),
            "expected_materialization_receipt_sha256": receipt_sha256,
            "expected_candidate_marker_sha256": marker_sha256,
            "expected_source_tree_sha256": source_identity.tree_sha256,
            "expected_tour_sha256": source_identity.tour_sha256,
            "expected_core_manifest_sha256": core_sha256,
        }
    )
    monkeypatch.setattr(intake, "_GOVERNED_RELOCATION_ROOT", sealed_root)
    monkeypatch.setattr(intake, "_GOVERNED_RELOCATION_SLUG", SLUG)
    monkeypatch.setattr(
        intake,
        "_GOVERNED_RELOCATION_HISTORICAL_CANDIDATE_ROOT",
        historical_root,
    )
    monkeypatch.setattr(
        intake,
        "_GOVERNED_RELOCATION_RECEIPT_SHA256",
        receipt_sha256,
    )
    monkeypatch.setattr(
        intake,
        "_GOVERNED_RELOCATION_MARKER_SHA256",
        marker_sha256,
    )
    monkeypatch.setattr(
        intake,
        "_GOVERNED_RELOCATION_SOURCE_TREE_SHA256",
        source_identity.tree_sha256,
    )
    monkeypatch.setattr(
        intake,
        "_GOVERNED_RELOCATION_TOUR_SHA256",
        source_identity.tour_sha256,
    )
    monkeypatch.setattr(
        intake,
        "_GOVERNED_RELOCATION_CORE_MANIFEST_SHA256",
        core_sha256,
    )

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_materialization_relocation_forbidden",
    ):
        intake.install_sealed_ai_panorama_bundle(request)

    monkeypatch.setattr(
        intake,
        "_validate_v2_publication_authority",
        lambda **_kwargs: pytest.fail(
            "artifact preflight must not query publication storage"
        ),
    )
    admission = _controller_admission(request, nonce_consumed=False)
    result = intake.install_sealed_ai_panorama_bundle(
        request,
        publication_admission=admission,
        artifact_preflight_only=True,
    )

    assert result["status"] == "artifact_preflight_validated"
    assert result["release_eligible"] is False
    assert result["controller_nonce_consumed"] is False
    assert result["materialization_receipt_sha256"] == receipt_sha256
    assert not (public_dir / SLUG).exists()


def test_artifact_preflight_rejects_apply_mode() -> None:
    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_artifact_preflight_invalid",
    ):
        intake.install_sealed_ai_panorama_bundle(
            {"contract": intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2},
            apply=True,
            publication_admission=object(),
            artifact_preflight_only=True,
        )


@pytest.mark.parametrize(
    ("record_update", "candidate_update", "reason"),
    (
        (
            {"principal_id": "other-principal"},
            {},
            "property_search_tour_principal_mismatch",
        ),
        (
            {"run_id": "other-run"},
            {},
            "property_search_tour_run_id_mismatch",
        ),
        (
            {"status": "running"},
            {},
            "property_search_tour_run_not_terminal",
        ),
        (
            {},
            {"candidate_ref": "other-candidate"},
            "property_search_tour_candidate_not_found",
        ),
        (
            {},
            {"listing_id": "9999999999"},
            "property_search_tour_listing_id_mismatch",
        ),
        (
            {},
            {"property_url": "https://www.willhaben.at/iad/immobilien/d/9999999999/"},
            "property_search_tour_candidate_url_mismatch",
        ),
        (
            {},
            {"source_ref": "property-scout:9999999999"},
            "property_search_tour_listing_id_mismatch",
        ),
        (
            {},
            {"source_ref": f"property-search:{EXTERNAL_ID}"},
            "property_search_tour_candidate_source_ref_mismatch",
        ),
    ),
)
def test_v2_rejects_unowned_or_identity_drifted_research_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_update: dict[str, object],
    candidate_update: dict[str, object],
    reason: str,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    record = _authorization_record(**record_update)
    candidate = record["summary"]["ranked_candidates"][0]
    candidate.update(candidate_update)
    monkeypatch.setattr(
        intake,
        "load_property_search_run_record_for_publication",
        lambda **_kwargs: record,
    )

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match=f"ai_panorama_publication_authority:{reason}",
    ):
        intake.install_sealed_ai_panorama_bundle(request)
    assert not (public_dir / SLUG).exists()


def test_v2_requires_principal_owned_durable_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    monkeypatch.setattr(
        intake,
        "load_property_search_run_record_for_publication",
        lambda **_kwargs: None,
    )

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_publication_run_not_found",
    ):
        intake.install_sealed_ai_panorama_bundle(request)


def test_v2_apply_revalidates_authority_with_locked_publication_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    locked_connection = object()
    calls: list[dict[str, object]] = []

    @contextlib.contextmanager
    def _authority(_principal_id: object, *, run_id: object = ""):
        yield locked_connection

    def _load(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return _authorization_record()

    monkeypatch.setattr(intake, "property_account_publication_authority", _authority)
    monkeypatch.setattr(intake, "load_property_search_run_record_for_publication", _load)

    receipt = intake.install_sealed_ai_panorama_bundle(
        request,
        apply=True,
        publication_admission=_controller_admission(request),
    )

    assert receipt["status"] == "installed"
    assert calls == [
        {
            "run_id": RUN_ID,
            "principal_id": PRINCIPAL,
            "connection": locked_connection,
            "for_update": True,
        }
    ]


def test_v2_apply_rejects_publication_record_drift_under_row_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    admission = _controller_admission(request)
    locked_connection = object()
    calls: list[dict[str, object]] = []

    @contextlib.contextmanager
    def _authority(_principal_id: object, *, run_id: object = ""):
        yield locked_connection

    changed_record = _authorization_record()
    changed_record["summary"]["review_state"] = "changed-after-permit"

    def _load(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return changed_record

    monkeypatch.setattr(intake, "property_account_publication_authority", _authority)
    monkeypatch.setattr(intake, "load_property_search_run_record_for_publication", _load)

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_publication_record_drift",
    ):
        intake.install_sealed_ai_panorama_bundle(
            request,
            apply=True,
            publication_admission=admission,
        )

    assert calls == [
        {
            "run_id": RUN_ID,
            "principal_id": PRINCIPAL,
            "connection": locked_connection,
            "for_update": True,
        }
    ]
    assert not (public_dir / SLUG).exists()


@pytest.mark.parametrize(
    ("failure_kind", "reason"),
    (
        ("rejected", "ai_panorama_publication_binding_store_rejected"),
        ("exception", "ai_panorama_publication_binding_store_failed"),
    ),
)
def test_v2_binding_cas_failure_rolls_back_before_public_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    reason: str,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    admission = _controller_admission(request)

    def _binding_failure(**_kwargs: object) -> dict[str, str]:
        if failure_kind == "exception":
            raise intake.AiPanoramaIntakeError(
                "ai_panorama_publication_binding_store_failed"
            )
        raise intake.AiPanoramaIntakeError(
            "ai_panorama_publication_binding_store_rejected"
        )

    monkeypatch.setattr(
        intake,
        "_bind_candidate_in_publication_transaction",
        _binding_failure,
    )

    with pytest.raises(intake.AiPanoramaIntakeError, match=reason):
        intake.install_sealed_ai_panorama_bundle(
            request,
            apply=True,
            publication_admission=admission,
        )

    assert not (public_dir / SLUG).exists()
    assert not tuple(public_dir.glob(f".{SLUG}.ai-intake-*"))


def test_post_rename_transaction_failure_exactly_compensates_new_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    admission = _controller_admission(request)
    original_rename = intake.os.rename
    original_fsync = intake.os.fsync
    target_renamed = False
    injected = False

    def _rename(source: object, target: object, *args: object, **kwargs: object):
        nonlocal target_renamed
        result = original_rename(source, target, *args, **kwargs)
        if Path(target) == public_dir / SLUG:
            target_renamed = True
        return result

    def _fsync(descriptor: int) -> None:
        nonlocal injected
        if target_renamed and not injected:
            injected = True
            raise OSError("injected post-rename fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(intake.os, "rename", _rename)
    monkeypatch.setattr(intake.os, "fsync", _fsync)

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_publication_transaction_failed",
    ):
        intake.install_sealed_ai_panorama_bundle(
            request,
            apply=True,
            publication_admission=admission,
        )

    assert injected is True
    assert not (public_dir / SLUG).exists()
    assert not tuple(public_dir.glob(f".{SLUG}.ai-rollback-*"))


def test_raise_before_commit_compensates_only_after_fresh_unbound_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    admission = _controller_admission(request)

    @contextlib.contextmanager
    def _commit_failure(*_args: object, **_kwargs: object):
        yield object()
        raise RuntimeError("injected ambiguous commit failure")

    monkeypatch.setattr(
        intake,
        "property_account_publication_authority",
        _commit_failure,
    )
    monkeypatch.setattr(
        intake,
        "_classify_publication_commit_outcome",
        lambda **_kwargs: "uncommitted",
    )

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_publication_transaction_failed",
    ) as captured:
        intake.install_sealed_ai_panorama_bundle(
            request,
            apply=True,
            publication_admission=admission,
        )

    assert captured.value.rollback_performed is True
    assert captured.value.commit_outcome_ambiguous is False
    assert captured.value.publication_outcome == "uncommitted"
    assert not (public_dir / SLUG).exists()
    assert not tuple(public_dir.glob(f".{SLUG}.ai-rollback-*"))


def test_commit_then_raise_retains_target_after_fresh_bound_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    admission = _controller_admission(request)

    @contextlib.contextmanager
    def _commit_then_raise(*_args: object, **_kwargs: object):
        yield object()
        raise RuntimeError("injected lost commit acknowledgement")

    monkeypatch.setattr(
        intake,
        "property_account_publication_authority",
        _commit_then_raise,
    )
    monkeypatch.setattr(
        intake,
        "_classify_publication_commit_outcome",
        lambda **_kwargs: "committed",
    )

    result = intake.install_sealed_ai_panorama_bundle(
        request,
        apply=True,
        publication_admission=admission,
    )

    assert result["status"] == "installed"
    assert result["publication_binding_verified"] is True
    assert (public_dir / SLUG).is_dir()


@pytest.mark.parametrize(
    ("durable_state", "expected"),
    (("before", "uncommitted"), ("after", "committed")),
)
def test_fresh_publication_outcome_classification_requires_exact_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    durable_state: str,
    expected: str,
) -> None:
    before_record = _authorization_record()
    bundle_identity = {
        "owner_verified": True,
        "search_run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "listing_url": LISTING_URL,
        "property_url": LISTING_URL,
        "property_url_sha256": property_search_source_url_sha256(LISTING_URL),
        "provider_key": "willhaben",
        "source_ref": SOURCE_REF,
        "external_id": EXTERNAL_ID,
    }
    control_url = f"https://propertyquarry.com/tours/{SLUG}/control"
    after_record, binding = intake.plan_property_search_candidate_tour_binding(
        before_record,
        principal_id=PRINCIPAL,
        run_id=RUN_ID,
        candidate_ref=CANDIDATE_REF,
        expected_listing_id=EXTERNAL_ID,
        generated_reconstruction_url=control_url,
        bundle_identity=bundle_identity,
        reconstruction_kind="ai_panorama_360",
    )
    identity = {
        "principal_id": PRINCIPAL,
        "search_run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "external_id": EXTERNAL_ID,
        "listing_url": LISTING_URL,
        "source_ref": SOURCE_REF,
        "provider_key": "willhaben",
        "property_url_sha256": property_search_source_url_sha256(LISTING_URL),
        "publication_binding_before_sha256": binding["before_sha256"],
        "publication_binding_after_sha256": binding["after_sha256"],
    }

    @contextlib.contextmanager
    def _authority(*_args: object, **_kwargs: object):
        yield object()

    monkeypatch.setattr(
        intake,
        "property_account_publication_authority",
        _authority,
    )
    monkeypatch.setattr(
        intake,
        "load_property_search_run_record_for_publication",
        lambda **_kwargs: (
            before_record if durable_state == "before" else after_record
        ),
    )
    monkeypatch.setattr(
        intake,
        "_publication_bundle_identity",
        lambda **_kwargs: bundle_identity,
    )

    assert (
        intake._classify_publication_commit_outcome(
            bundle_dir=tmp_path,
            request={
                "expected_slug": SLUG,
                "public_control_url": control_url,
            },
            identity=identity,
        )
        == expected
    )


def test_publication_storage_cas_checks_erasure_fence_before_row_lock(
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
            query: object,
            _parameters: object = None,
        ) -> None:
            statements.append(" ".join(str(query).split()))

        def fetchone(self) -> tuple[bool]:
            return (True,)

    class _Connection:
        autocommit = False
        info = SimpleNamespace(
            transaction_status=SimpleNamespace(name="INTRANS")
        )

        def cursor(self) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(
        storage,
        "_set_property_search_writer_contract",
        lambda _cursor: None,
    )
    result = storage.update_property_search_run_record_for_publication(
        _Connection(),
        principal_id=PRINCIPAL,
        run_id=RUN_ID,
        expected_record_sha256="a" * 64,
        updated_record=_authorization_record(),
    )

    assert result == {"status": "store_rejected"}
    assert statements[0] == "SELECT property_search_assert_erasure_key()"
    assert "pg_advisory_xact_lock" in statements[1]
    assert "property_search_erasure_fences" in statements[2]
    assert all("property_search_runs" not in statement for statement in statements)


def test_publication_storage_cas_rejects_autocommit_connection() -> None:
    class _Connection:
        autocommit = True
        info = SimpleNamespace(
            transaction_status=SimpleNamespace(name="INTRANS")
        )

        def cursor(self) -> object:
            pytest.fail("autocommit must be rejected before opening a cursor")

    with pytest.raises(
        ValueError,
        match="property_search_publication_active_transaction_required",
    ):
        storage.update_property_search_run_record_for_publication(
            _Connection(),
            principal_id=PRINCIPAL,
            run_id=RUN_ID,
            expected_record_sha256="a" * 64,
            updated_record=_authorization_record(),
        )


def test_identity_free_discovery_rejects_erasure_fence_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    record = _authorization_record()

    class _Cursor:
        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(
            self,
            query: object,
            _parameters: object = None,
        ) -> None:
            statements.append(" ".join(str(query).split()))

        def fetchall(self) -> list[tuple[dict[str, object]]]:
            return [(record,)]

        def fetchone(self) -> tuple[bool]:
            return (True,)

    class _Connection:
        autocommit = False
        info = SimpleNamespace(
            transaction_status=SimpleNamespace(name="INTRANS")
        )

        def cursor(self) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(
        storage,
        "_set_property_search_writer_contract",
        lambda _cursor: None,
    )

    result = storage.load_unique_property_search_run_record_for_discovery(
        run_id=RUN_ID,
        connection=_Connection(),
    )

    assert result is None
    assert "pg_advisory_xact_lock" in statements[2]
    assert "property_search_erasure_fences" in statements[3]
    assert sum(
        "FROM property_search_runs" in statement
        for statement in statements
    ) == 1


def test_identity_free_discovery_rejects_record_drift_after_erasure_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _authorization_record()
    changed = _authorization_record()
    changed["status"] = "changed-during-discovery"
    rows = iter(([(initial,)], [(changed,)]))

    class _Cursor:
        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(
            self,
            _query: object,
            _parameters: object = None,
        ) -> None:
            return None

        def fetchall(self) -> list[tuple[dict[str, object]]]:
            return next(rows)

        def fetchone(self) -> tuple[bool]:
            return (False,)

    class _Connection:
        autocommit = False
        info = SimpleNamespace(
            transaction_status=SimpleNamespace(name="INTRANS")
        )

        def cursor(self) -> _Cursor:
            return _Cursor()

    monkeypatch.setattr(
        storage,
        "_set_property_search_writer_contract",
        lambda _cursor: None,
    )

    assert (
        storage.load_unique_property_search_run_record_for_discovery(
            run_id=RUN_ID,
            connection=_Connection(),
        )
        is None
    )


def test_publication_binding_updates_locked_record_on_existing_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _authorization_record()
    expected_sha256 = property_search_run_record_sha256(record)
    identity = {
        "principal_id": PRINCIPAL,
        "search_run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "external_id": EXTERNAL_ID,
        "listing_url": LISTING_URL,
        "source_ref": SOURCE_REF,
        "provider_key": "willhaben",
        "property_url_sha256": property_search_source_url_sha256(LISTING_URL),
        "expected_publication_record_sha256": expected_sha256,
    }
    request = {
        "expected_slug": SLUG,
        "public_control_url": f"https://propertyquarry.com/tours/{SLUG}/control",
    }
    private_receipt = {
        "principal_id": PRINCIPAL,
        "search_run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "listing_url": LISTING_URL,
        "property_url": LISTING_URL,
        "source_ref": SOURCE_REF,
        "external_id": EXTERNAL_ID,
    }
    connection = object()
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        intake,
        "_load_hosted_property_tour_private_receipt",
        lambda _bundle_dir: private_receipt,
    )
    monkeypatch.setattr(
        intake,
        "load_property_search_run_record_for_publication",
        lambda **kwargs: observed.setdefault("load", kwargs) and record,
    )

    def _update(
        passed_connection: object,
        **kwargs: object,
    ) -> dict[str, object]:
        observed["connection"] = passed_connection
        observed["update"] = kwargs
        updated = dict(kwargs["updated_record"])
        return {
            "status": "applied",
            "record": updated,
            "record_sha256": property_search_run_record_sha256(updated),
        }

    monkeypatch.setattr(
        intake,
        "update_property_search_run_record_for_publication",
        _update,
    )

    receipt = _REAL_BIND_CANDIDATE_IN_PUBLICATION_TRANSACTION(
        connection=connection,
        bundle_dir=tmp_path,
        request=request,
        identity=identity,
    )

    assert observed["load"] == {
        "run_id": RUN_ID,
        "principal_id": PRINCIPAL,
        "connection": connection,
        "for_update": True,
    }
    assert observed["connection"] is connection
    assert observed["update"]["expected_record_sha256"] == expected_sha256
    assert receipt["publication_binding_verified"] == "true"
    assert receipt["publication_binding_status"] == "applied"


def test_v2_release_eligible_lineage_applies_with_exact_cas_hashes(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )

    receipt = intake.install_sealed_ai_panorama_bundle(
        request,
        apply=True,
        publication_admission=_controller_admission(request),
    )

    assert receipt["status"] == "installed"
    assert receipt["applied"] is True
    assert receipt["materialization_lineage_verified"] is True
    assert receipt["release_eligible"] is True
    assert receipt["controller_permit_verified"] is True
    assert receipt["authenticated_principal_verified"] is True
    assert receipt["controller_permit_sha256"] == "d" * 64
    assert (public_dir / SLUG / "tour.json").is_file()


def test_v1_with_complete_lineage_remains_non_release_eligible(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    request["contract"] = intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V1

    receipt = intake.install_sealed_ai_panorama_bundle(request)

    assert receipt["materialization_lineage_verified"] is True
    assert receipt["release_eligible"] is False


@pytest.mark.parametrize(
    ("digest_field", "reason"),
    (
        (
            "expected_materialization_receipt_sha256",
            "materialization_receipt_sha256_mismatch",
        ),
        (
            "expected_candidate_marker_sha256",
            "materialization_receipt_binding_mismatch",
        ),
    ),
)
def test_v2_rejects_mismatched_lineage_digest(
    tmp_path: Path,
    digest_field: str,
    reason: str,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    request[digest_field] = "0" * 64

    with pytest.raises(intake.AiPanoramaIntakeError, match=reason):
        intake.install_sealed_ai_panorama_bundle(request)


@pytest.mark.parametrize(
    ("digest_field", "reason"),
    (
        (
            "expected_materialization_receipt_sha256",
            "materialization_receipt_sha256_invalid",
        ),
        (
            "expected_candidate_marker_sha256",
            "candidate_marker_sha256_invalid",
        ),
    ),
)
def test_v2_rejects_non_string_request_digest(
    tmp_path: Path,
    digest_field: str,
    reason: str,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    request[digest_field] = int("1" * 64)

    with pytest.raises(intake.AiPanoramaIntakeError, match=reason):
        intake.install_sealed_ai_panorama_bundle(request)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_copy_identity_verified", 1),
        ("production_mutation_performed", 0),
    ),
)
def test_v2_rejects_non_boolean_lineage_assertions(
    tmp_path: Path,
    field: str,
    value: int,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload[field] = value
    encoded = _canonical(payload)
    receipt_path.write_bytes(encoded)
    receipt_path.chmod(0o600)
    request["expected_materialization_receipt_sha256"] = hashlib.sha256(
        encoded
    ).hexdigest()

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="materialization_receipt_binding_mismatch",
    ):
        intake.install_sealed_ai_panorama_bundle(request)


@pytest.mark.parametrize(
    "field",
    (
        "candidate_tree_sha256",
        "source_tree_sha256",
        "bundle_material_sha256",
    ),
)
def test_v2_rejects_non_string_lineage_digests(
    tmp_path: Path,
    field: str,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload[field] = int("1" * 64)
    encoded = _canonical(payload)
    receipt_path.write_bytes(encoded)
    receipt_path.chmod(0o600)
    request["expected_materialization_receipt_sha256"] = hashlib.sha256(
        encoded
    ).hexdigest()

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="materialization_receipt_binding_mismatch",
    ):
        intake.install_sealed_ai_panorama_bundle(request)


@pytest.mark.parametrize("encoding", ("pretty", "duplicate", "nonfinite"))
def test_v2_rejects_noncanonical_or_ambiguous_materialization_receipt(
    tmp_path: Path,
    encoding: str,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    if encoding == "pretty":
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    elif encoding == "duplicate":
        encoded = b'{"status":"pass",' + _canonical(payload)[1:]
    else:
        encoded = b'{"nonfinite":NaN,' + _canonical(payload)[1:]
    receipt_path.write_bytes(encoded)
    receipt_path.chmod(0o600)
    request["expected_materialization_receipt_sha256"] = hashlib.sha256(
        encoded
    ).hexdigest()

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="materialization_receipt_invalid",
    ):
        intake.install_sealed_ai_panorama_bundle(request)


@pytest.mark.parametrize(
    ("swap_kind", "reason"),
    (
        ("receipt", "materialization_receipt_invalid"),
        ("marker", "candidate_marker_invalid"),
    ),
)
def test_v2_rejects_lineage_file_swap_between_path_check_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_kind: str,
    reason: str,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, receipt_path, marker_path = _v2_lineage_request(bundle, public_dir)
    target_path = receipt_path if swap_kind == "receipt" else marker_path
    replacement_path = target_path.with_name(f".{target_path.name}.replacement")
    replacement_path.write_bytes(target_path.read_bytes())
    replacement_path.chmod(0o600)
    original_open = intake.os.open

    def open_replacement(
        path: object,
        flags: int,
        *args: object,
        **kwargs: object,
    ) -> int:
        target = replacement_path if Path(path) == target_path else path
        return original_open(target, flags, *args, **kwargs)

    monkeypatch.setattr(intake.os, "open", open_replacement)
    with pytest.raises(intake.AiPanoramaIntakeError, match=reason):
        intake.install_sealed_ai_panorama_bundle(request)


@pytest.mark.parametrize(
    ("swap_kind", "reason"),
    (
        ("receipt", "materialization_receipt_changed"),
        ("marker", "candidate_marker_changed"),
        ("candidate", "materialization_candidate_changed"),
        ("root", "materialization_candidate_changed"),
        ("root_final", "materialization_candidate_changed"),
    ),
)
def test_v2_rejects_lineage_inode_or_candidate_swaps_during_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_kind: str,
    reason: str,
) -> None:
    bundle = _make_bundle(
        tmp_path / "source",
        directory_name=SLUG,
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, receipt_path, marker_path = _v2_lineage_request(bundle, public_dir)
    original_load = intake._load_lineage_json
    swapped = False
    marker_load_count = 0

    def load_then_swap(
        path: Path,
        *,
        code: str,
    ) -> tuple[dict[str, object], bytes, tuple[int, int, int, int]]:
        nonlocal marker_load_count, swapped
        result = original_load(path, code=code)
        if path == marker_path:
            marker_load_count += 1
        trigger = receipt_path if swap_kind == "receipt" else marker_path
        should_replace = not swapped and path == trigger
        if swap_kind == "root_final":
            should_replace = should_replace and marker_load_count == 2
        if should_replace:
            swapped = True
            if swap_kind == "candidate":
                backup = bundle.parent / f".{SLUG}.swapped"
                bundle.rename(backup)
                shutil.copytree(backup, bundle, copy_function=shutil.copy2)
            elif swap_kind in {"root", "root_final"}:
                candidate_root = bundle.parent
                moved_root = candidate_root.with_name(
                    f".{candidate_root.name}.swapped"
                )
                candidate_root.rename(moved_root)
                candidate_root.mkdir()
                (moved_root / bundle.name).rename(candidate_root / bundle.name)
                (
                    moved_root / intake.AI_PANORAMA_CANDIDATE_MARKER_RELPATH
                ).rename(
                    candidate_root / intake.AI_PANORAMA_CANDIDATE_MARKER_RELPATH
                )
                moved_root.rmdir()
            else:
                replacement = path.with_name(f".{path.name}.replacement")
                replacement.write_bytes(result[1])
                replacement.chmod(0o600)
                os.replace(replacement, path)
        return result

    monkeypatch.setattr(intake, "_load_lineage_json", load_then_swap)
    with pytest.raises(intake.AiPanoramaIntakeError, match=reason):
        intake.install_sealed_ai_panorama_bundle(request)


def test_apply_requires_controller_authority_and_both_exact_source_hashes(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_apply_authority_required",
    ):
        intake.install_sealed_ai_panorama_bundle(request, apply=True)

    plan = intake.install_sealed_ai_panorama_bundle(request)
    request["expected_source_tree_sha256"] = plan["source_tree_sha256"]
    request["expected_tour_sha256"] = "0" * 64
    with pytest.raises(intake.AiPanoramaIntakeError, match="source_tour_sha256_mismatch"):
        intake.install_sealed_ai_panorama_bundle(
            request,
            apply=True,
            publication_admission=_controller_admission(request),
        )
    assert not (public_dir / SLUG).exists()


def test_apply_rejects_spoofed_real_owner_before_database_or_filesystem_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    authority = _controller_admission(request)
    spoofed = dict(request, principal_id="other-real-owner@example.invalid")
    monkeypatch.setattr(
        intake,
        "load_property_search_run_record_for_publication",
        lambda **_kwargs: pytest.fail(
            "controller principal mismatch must fail before database lookup"
        ),
    )

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_apply_authority_binding_mismatch",
    ):
        intake.install_sealed_ai_panorama_bundle(
            spoofed,
            apply=True,
            publication_admission=authority,
        )
    assert not (public_dir / SLUG).exists()


def test_apply_revalidates_external_permit_and_ledger_before_any_path_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "contract": intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
        "source_bundle": str(tmp_path / "caller-selected-source"),
        "public_tour_dir": str(tmp_path / "caller-selected-public"),
    }
    monkeypatch.setattr(
        intake,
        "_revalidate_ai_panorama_install_admission",
        lambda _admission, *, require_consumed: (_ for _ in ()).throw(
            intake.AiPanoramaIntakeError(
                "ai_panorama_controller_admission_invalid"
            )
        ),
    )
    monkeypatch.setattr(
        intake,
        "_confined_source_bundle",
        lambda *_args, **_kwargs: pytest.fail(
            "artifact path must not be read before controller revalidation"
        ),
    )

    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="ai_panorama_controller_admission_invalid",
    ):
        intake.install_sealed_ai_panorama_bundle(
            request,
            apply=True,
            publication_admission=object(),
        )


def test_operator_cli_cannot_apply_without_controller_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps({"contract": intake.AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2}),
        encoding="utf-8",
    )
    request_path.chmod(0o600)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "install_ai_panorama_tour_bundle.py",
            "--request",
            str(request_path),
            "--apply",
        ],
    )

    exit_code = installer_script.main()
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["status"] == "failed"
    assert payload["mode"] == "apply"
    assert payload["error"] == "ai_panorama_apply_authority_required"


def test_apply_writes_owned_pair_atomically_and_new_admission_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )

    first_admission = _controller_admission(request)
    second_admission = _controller_admission(request, permit_sha256="e" * 64)
    binding_statuses = iter(("applied", "already_bound"))

    def _binding(**_kwargs: object) -> dict[str, str]:
        status = next(binding_statuses)
        return {
            "publication_binding_verified": "true",
            "publication_binding_status": status,
            "publication_binding_before_sha256": "1" * 64,
            "publication_binding_after_sha256": (
                "2" * 64 if status == "applied" else "1" * 64
            ),
        }

    monkeypatch.setattr(
        intake,
        "_bind_candidate_in_publication_transaction",
        _binding,
    )
    first = intake.install_sealed_ai_panorama_bundle(
        request,
        apply=True,
        publication_admission=first_admission,
    )
    second = intake.install_sealed_ai_panorama_bundle(
        request,
        apply=True,
        publication_admission=second_admission,
    )

    target = public_dir / SLUG
    private_path = target / "tour.private.json"
    private = json.loads(private_path.read_text(encoding="utf-8"))
    assert first["status"] == "installed"
    assert first["applied"] is True
    assert second["status"] == "already_installed"
    assert second["applied"] is False
    assert first["publication_binding_status"] == "applied"
    assert second["publication_binding_status"] == "already_bound"
    assert stat_mode(private_path) == 0o600
    assert {
        key: private[key]
        for key in (
            "principal_id",
            "search_run_id",
            "candidate_ref",
            "listing_url",
            "property_url",
            "source_ref",
            "external_id",
        )
    } == {
        "principal_id": PRINCIPAL,
        "search_run_id": RUN_ID,
        "candidate_ref": CANDIDATE_REF,
        "listing_url": LISTING_URL,
        "property_url": LISTING_URL,
        "source_ref": SOURCE_REF,
        "external_id": EXTERNAL_ID,
    }
    public = json.loads((target / "tour.json").read_text(encoding="utf-8"))
    assert public["property_url_sha256"] == property_search_source_url_sha256(LISTING_URL)
    assert not intake._PRIVATE_MANIFEST_KEYS.intersection(public)


def test_existing_target_commit_then_raise_is_reclassified_as_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    intake.install_sealed_ai_panorama_bundle(
        request,
        apply=True,
        publication_admission=_controller_admission(request),
    )

    @contextlib.contextmanager
    def _commit_then_raise(*_args: object, **_kwargs: object):
        yield object()
        raise RuntimeError("injected lost commit acknowledgement")

    monkeypatch.setattr(
        intake,
        "property_account_publication_authority",
        _commit_then_raise,
    )
    monkeypatch.setattr(
        intake,
        "_classify_publication_commit_outcome",
        lambda **_kwargs: "committed",
    )

    result = intake.install_sealed_ai_panorama_bundle(
        request,
        apply=True,
        publication_admission=_controller_admission(
            request,
            permit_sha256="e" * 64,
        ),
    )

    assert result["status"] == "already_installed"
    assert result["already_installed"] is True
    assert (public_dir / SLUG).is_dir()


def stat_mode(path: Path) -> int:
    return path.stat(follow_symlinks=False).st_mode & 0o777


def test_existing_target_rejects_wrong_owner_and_replacement(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path / "source", directory_name=SLUG)
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request, _receipt_path, _marker_path = _v2_lineage_request(bundle, public_dir)
    plan = intake.install_sealed_ai_panorama_bundle(request)
    request.update(
        {
            "expected_source_tree_sha256": plan["source_tree_sha256"],
            "expected_tour_sha256": plan["source_tour_sha256"],
        }
    )
    authority = _controller_admission(request)
    intake.install_sealed_ai_panorama_bundle(
        request,
        apply=True,
        publication_admission=authority,
    )

    wrong_owner = dict(request, principal_id="other-principal")
    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="apply_authority_binding_mismatch",
    ):
        intake.install_sealed_ai_panorama_bundle(
            wrong_owner,
            apply=True,
            publication_admission=authority,
        )

    replacement_bundle = _make_bundle(
        tmp_path / "replacement",
        directory_name=SLUG,
    )
    (replacement_bundle / "panoramas/living-room.jpg").write_bytes(b"different-panorama")
    replacement_request, _replacement_receipt, _replacement_marker = (
        _v2_lineage_request(replacement_bundle, public_dir)
    )
    snapshot = intake._scan_source_bundle(replacement_bundle)
    replacement_request.update(
        {
            "expected_source_tree_sha256": snapshot.tree_sha256,
            "expected_tour_sha256": snapshot.tour_sha256,
        }
    )
    with pytest.raises(intake.AiPanoramaIntakeError, match="target_replace_forbidden"):
        intake.install_sealed_ai_panorama_bundle(
            replacement_request,
            apply=True,
            publication_admission=_controller_admission(replacement_request),
        )


def test_rejects_symlinks_extra_files_and_provider_mislabel(tmp_path: Path) -> None:
    public_dir = tmp_path / "public"
    public_dir.mkdir()

    symlink_bundle = _make_bundle(tmp_path / "symlink-source")
    (symlink_bundle / "proof/browser-mobile.png").unlink()
    (symlink_bundle / "proof/browser-mobile.png").symlink_to(
        symlink_bundle / "proof/browser-desktop.png"
    )
    with pytest.raises(intake.AiPanoramaIntakeError, match="source_symlink_forbidden"):
        intake.install_sealed_ai_panorama_bundle(_request(symlink_bundle, public_dir))

    extra_bundle = _make_bundle(tmp_path / "extra-source")
    _write(extra_bundle / "proof/unbound.json", b"{}")
    with pytest.raises(intake.AiPanoramaIntakeError, match="source_file_set_mismatch"):
        intake.install_sealed_ai_panorama_bundle(_request(extra_bundle, public_dir))

    mislabelled_bundle = _make_bundle(tmp_path / "mislabelled-source")
    provenance_path = mislabelled_bundle / "proof/provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["property_binding_kind"] = "propertyquarry_research_url_sha256"
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")
    with pytest.raises(
        intake.AiPanoramaIntakeError,
        match="provider_qualified_provenance_mismatch",
    ):
        intake.install_sealed_ai_panorama_bundle(_request(mislabelled_bundle, public_dir))


def test_rejects_source_and_destination_outside_configured_roots(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(tmp_path / "source")
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    request = _request(bundle, public_dir)

    other_public_dir = tmp_path / "other-public"
    other_public_dir.mkdir()
    os.environ["EA_PUBLIC_TOUR_DIR"] = str(other_public_dir)
    with pytest.raises(intake.AiPanoramaIntakeError, match="public_dir_not_configured"):
        intake.install_sealed_ai_panorama_bundle(request)

    os.environ["EA_PUBLIC_TOUR_DIR"] = str(public_dir)
    other_incoming_dir = tmp_path / "other-incoming"
    other_incoming_dir.mkdir()
    os.environ["PROPERTYQUARRY_TOUR_EXPORT_INCOMING_DIR"] = str(other_incoming_dir)
    with pytest.raises(intake.AiPanoramaIntakeError, match="source_outside_incoming_root"):
        intake.install_sealed_ai_panorama_bundle(request)


def test_web_image_copies_binder_and_installer_operator_clis() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[1] / "ea" / "Dockerfile.property-web"
    ).read_text(encoding="utf-8")
    assert (
        "COPY --chmod=0555 scripts/install_ai_panorama_tour_bundle.py "
        "/app/scripts/install_ai_panorama_tour_bundle.py"
    ) in dockerfile
    assert (
        "COPY --chmod=0555 scripts/bind_property_search_candidate_tour.py "
        "/app/scripts/bind_property_search_candidate_tour.py"
    ) in dockerfile
