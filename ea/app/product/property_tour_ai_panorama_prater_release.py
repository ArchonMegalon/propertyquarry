from __future__ import annotations

import hmac
from pathlib import Path
from typing import Mapping

from app.product.property_search_tour_binding import (
    PropertySearchTourBindingError,
    property_search_source_url_sha256,
)
from app.product.property_tour_ai_panorama_intake import (
    AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
    AiPanoramaIntakeError,
    _revalidate_ai_panorama_install_admission,
    install_sealed_ai_panorama_bundle,
)


PRATER_AI_PANORAMA_RELEASE_CONTRACT = (
    "propertyquarry.prater_ai_panorama_governed_release.v1"
)
PRATER_SEARCH_RUN_ID = "98bed75e984549c6bd4371d602662ab8"
PRATER_CANDIDATE_REF = "053ad185e1c44b2e"
PRATER_EXTERNAL_ID = "1807240910"
PRATER_SOURCE_REF = "property-scout:1807240910"
PRATER_PROVIDER_KEY = "willhaben"
PRATER_SLUG = "prater-messe-maisonette-ai-360-053ad185e1c44b2e"
PRATER_PROPERTY_URL_SHA256 = (
    "f451d904167c5b1a2b27f698ec38c18f6760fe55b79cca32c99bc986f8293d8e"
)
PRATER_SOURCE_TREE_SHA256 = (
    "fe2bdc9162d82236d70d0e74deb283bb06186026fd2c31c90431711cb87a775c"
)
PRATER_TOUR_SHA256 = (
    "c3795ca2956c18e3e8b1749611660052dac794a08dec7f47db212b51049cf849"
)
PRATER_CORE_MANIFEST_SHA256 = (
    "15e9b6bac56c47363da0fe49b99697215833d9ea6c94ae43253bde4e288c401d"
)
PRATER_MATERIALIZATION_RECEIPT_SHA256 = (
    "accba9c5b5575020d9cd6fcc299ed9653f6d8f094d58598e7bfc13db0061daba"
)
PRATER_CANDIDATE_MARKER_SHA256 = (
    "bf436b0645e44b203fe9b0c2f01c88d1ddce25aa7b1a45d04fa27b805eaf73fd"
)
PRATER_CONTROLLER_ARTIFACT_ROOT = Path("/docker/property/state")
PRATER_CONTROLLER_PUBLIC_ROOT = Path(
    "/docker/property/state/public_property_tours"
)
PRATER_ARTIFACT_RELPATH = (
    "incoming_property_tours/prater-053ad185e1c44b2e/"
    "ai-panorama-v2-yaw65-final/"
    "prater-messe-maisonette-ai-360-053ad185e1c44b2e"
)
PRATER_MATERIALIZATION_RECEIPT_RELPATH = (
    "runtime/"
    "propertyquarry_source_reconcile_27c27669_20260723T194218Z.private/"
    "incoming-canonical-before/prater-053ad185e1c44b2e/"
    "ai-panorama-v2-yaw65-final.receipt.json"
)
PRATER_PUBLIC_CONTROL_URL = (
    "https://propertyquarry.com/tours/"
    "prater-messe-maisonette-ai-360-053ad185e1c44b2e/control"
)


def _fail(code: str) -> None:
    raise AiPanoramaIntakeError(code)


def _exact(actual: object, expected: str) -> bool:
    return hmac.compare_digest(str(actual or "").strip(), expected)


def _validated_prater_admission(
    admission: object,
    *,
    require_consumed: bool,
) -> object:
    verified = _revalidate_ai_panorama_install_admission(
        admission,
        require_consumed=require_consumed,
    )
    exact_bindings = (
        ("search_run_id", PRATER_SEARCH_RUN_ID),
        ("candidate_ref", PRATER_CANDIDATE_REF),
        ("external_id", PRATER_EXTERNAL_ID),
        ("source_ref", PRATER_SOURCE_REF),
        ("provider_key", PRATER_PROVIDER_KEY),
        ("expected_slug", PRATER_SLUG),
        ("expected_source_tree_sha256", PRATER_SOURCE_TREE_SHA256),
        ("expected_tour_sha256", PRATER_TOUR_SHA256),
        ("expected_core_manifest_sha256", PRATER_CORE_MANIFEST_SHA256),
        (
            "expected_materialization_receipt_sha256",
            PRATER_MATERIALIZATION_RECEIPT_SHA256,
        ),
        (
            "expected_candidate_marker_sha256",
            PRATER_CANDIDATE_MARKER_SHA256,
        ),
        ("artifact_relpath", PRATER_ARTIFACT_RELPATH),
        (
            "materialization_receipt_relpath",
            PRATER_MATERIALIZATION_RECEIPT_RELPATH,
        ),
    )
    if any(
        not _exact(getattr(verified, field, None), expected)
        for field, expected in exact_bindings
    ):
        _fail("ai_panorama_prater_permit_binding_mismatch")
    listing_url = str(getattr(verified, "listing_url", "") or "").strip()
    if not hmac.compare_digest(
        property_search_source_url_sha256(listing_url),
        PRATER_PROPERTY_URL_SHA256,
    ):
        _fail("ai_panorama_prater_listing_binding_mismatch")
    if Path(getattr(verified, "incoming_root", "")) != PRATER_CONTROLLER_ARTIFACT_ROOT:
        _fail("ai_panorama_prater_artifact_root_mismatch")
    if Path(getattr(verified, "public_tour_dir", "")) != PRATER_CONTROLLER_PUBLIC_ROOT:
        _fail("ai_panorama_prater_public_root_mismatch")
    if Path(getattr(verified, "source_bundle", "")) != (
        PRATER_CONTROLLER_ARTIFACT_ROOT / PRATER_ARTIFACT_RELPATH
    ):
        _fail("ai_panorama_prater_artifact_path_mismatch")
    if Path(getattr(verified, "materialization_receipt_path", "")) != (
        PRATER_CONTROLLER_ARTIFACT_ROOT
        / PRATER_MATERIALIZATION_RECEIPT_RELPATH
    ):
        _fail("ai_panorama_prater_receipt_path_mismatch")
    return verified


def _prater_install_request(verified: object) -> dict[str, object]:
    """Project only controller-admitted values into the private V2 request."""

    return {
        "contract": AI_PANORAMA_INSTALL_REQUEST_CONTRACT_V2,
        "source_bundle": str(getattr(verified, "source_bundle")),
        "materialization_receipt_path": str(
            getattr(verified, "materialization_receipt_path")
        ),
        "public_tour_dir": str(getattr(verified, "public_tour_dir")),
        "principal_id": str(getattr(verified, "authenticated_principal_id")),
        "search_run_id": PRATER_SEARCH_RUN_ID,
        "candidate_ref": PRATER_CANDIDATE_REF,
        "external_id": PRATER_EXTERNAL_ID,
        "listing_url": str(getattr(verified, "listing_url")),
        "source_ref": PRATER_SOURCE_REF,
        "provider_key": PRATER_PROVIDER_KEY,
        "expected_slug": PRATER_SLUG,
        "expected_source_tree_sha256": PRATER_SOURCE_TREE_SHA256,
        "expected_tour_sha256": PRATER_TOUR_SHA256,
        "expected_core_manifest_sha256": PRATER_CORE_MANIFEST_SHA256,
        "expected_materialization_receipt_sha256": (
            PRATER_MATERIALIZATION_RECEIPT_SHA256
        ),
        "expected_candidate_marker_sha256": PRATER_CANDIDATE_MARKER_SHA256,
        "expected_publication_record_sha256": str(
            getattr(verified, "expected_publication_record_sha256")
        ),
    }


def _binding_receipt_projection(
    receipt: Mapping[str, object],
) -> dict[str, object]:
    allowed = (
        "status",
        "mode",
        "changed",
        "before_sha256",
        "after_sha256",
        "persisted_sha256",
    )
    return {key: receipt[key] for key in allowed if key in receipt}


def run_prater_ai_panorama_release(
    admission: object,
    *,
    apply: bool = False,
) -> dict[str, object]:
    """Run the exact Prater install and owner-receipt CAS binding.

    Admission verification and, for apply, durable nonce consumption happen
    before any artifact path is opened. The source remains in its preserved
    fixed-root location; this operation never copies it into an ad-hoc root.
    """

    verified = _validated_prater_admission(
        admission,
        require_consumed=apply,
    )
    request = _prater_install_request(verified)
    install_receipt = install_sealed_ai_panorama_bundle(
        request,
        apply=apply,
        publication_admission=admission,
    )
    result: dict[str, object] = {
        "contract": PRATER_AI_PANORAMA_RELEASE_CONTRACT,
        "mode": "apply" if apply else "dry_run",
        "status": str(install_receipt.get("status") or ""),
        "slug": PRATER_SLUG,
        "control_path": f"/tours/{PRATER_SLUG}/control",
        "install_receipt": install_receipt,
        "binding_status": "requires_installed_owner_receipt",
        "release_eligible": False,
        "private_values_redacted": True,
    }
    if not apply:
        return result

    try:
        from app.product.service import (
            bind_property_search_candidate_generated_reconstruction,
        )

        binding_receipt = bind_property_search_candidate_generated_reconstruction(
            principal_id=str(getattr(verified, "authenticated_principal_id")),
            run_id=PRATER_SEARCH_RUN_ID,
            candidate_ref=PRATER_CANDIDATE_REF,
            expected_listing_id=PRATER_EXTERNAL_ID,
            generated_reconstruction_url=PRATER_PUBLIC_CONTROL_URL,
            expected_record_sha256=str(
                getattr(verified, "expected_publication_record_sha256")
            ),
            reconstruction_kind="ai_panorama_360",
            apply=True,
        )
    except PropertySearchTourBindingError:
        _fail("ai_panorama_prater_owner_receipt_cas_failed")
    binding_status = str(binding_receipt.get("status") or "").strip()
    if binding_status not in {"applied", "already_bound"}:
        _fail("ai_panorama_prater_owner_receipt_cas_failed")
    result["binding_status"] = binding_status
    result["binding_receipt"] = _binding_receipt_projection(binding_receipt)
    result["release_eligible"] = (
        install_receipt.get("release_eligible") is True
        and binding_status in {"applied", "already_bound"}
    )
    result["status"] = "released" if result["release_eligible"] else "failed"
    return result


__all__ = [
    "PRATER_AI_PANORAMA_RELEASE_CONTRACT",
    "PRATER_ARTIFACT_RELPATH",
    "PRATER_CANDIDATE_MARKER_SHA256",
    "PRATER_CANDIDATE_REF",
    "PRATER_CONTROLLER_ARTIFACT_ROOT",
    "PRATER_CONTROLLER_PUBLIC_ROOT",
    "PRATER_CORE_MANIFEST_SHA256",
    "PRATER_MATERIALIZATION_RECEIPT_RELPATH",
    "PRATER_MATERIALIZATION_RECEIPT_SHA256",
    "PRATER_SEARCH_RUN_ID",
    "PRATER_SLUG",
    "PRATER_SOURCE_TREE_SHA256",
    "PRATER_TOUR_SHA256",
    "run_prater_ai_panorama_release",
]
