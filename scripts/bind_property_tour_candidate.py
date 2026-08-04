#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(_SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIRECTORY))

from property_tour_host_safety import (
    TourHostSafetyError,
    require_bounded_file,
    tour_manifest_max_bytes,
)
from property_tour_publication_lock import property_tour_publication_lock


_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_PRINCIPAL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+-]{2,199}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_VIDEO_PROVIDERS = {"propertyquarry_core_gold"}
_SAFE_VIDEO_COVERAGE = "boundary_verified_frame_continuation"


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _read_json_file(path: Path, *, private: bool) -> tuple[dict[str, object], bytes]:
    try:
        require_bounded_file(
            path,
            reason_prefix="tour_candidate_binding",
            maximum_bytes=tour_manifest_max_bytes(),
        )
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("tour_candidate_binding_manifest_invalid")
        if private and stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("tour_candidate_binding_private_mode_invalid")
        body = path.read_bytes()
        payload = json.loads(body.decode("utf-8"))
    except (OSError, UnicodeError, ValueError, TourHostSafetyError) as exc:
        raise ValueError("tour_candidate_binding_manifest_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("tour_candidate_binding_manifest_invalid")
    return dict(payload), body


def _write_json_atomic(path: Path, payload: dict[str, object], *, mode: int) -> None:
    body = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.candidate-binding-",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.replace(path)
        path.chmod(mode)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _validated_safe_video_fields(
    private_payload: dict[str, object],
) -> dict[str, str]:
    legacy = (
        dict(private_payload.get("legacy_private_fields") or {})
        if isinstance(private_payload.get("legacy_private_fields"), dict)
        else {}
    )
    provider = str(
        legacy.get("video_provider_key")
        or legacy.get("video_provider")
        or ""
    ).strip().lower()
    coverage = str(legacy.get("video_coverage_proof") or "").strip()
    if provider not in _SAFE_VIDEO_PROVIDERS or coverage != _SAFE_VIDEO_COVERAGE:
        raise ValueError("tour_candidate_binding_walkthrough_proof_invalid")
    return {
        "video_provider": provider,
        "video_provider_key": provider,
        "video_coverage_proof": coverage,
    }


def _require_verified_3dvista_proof(
    private_payload: dict[str, object],
    *,
    slug: str,
) -> None:
    provenance = (
        dict(private_payload.get("three_d_vista_target_provenance") or {})
        if isinstance(private_payload.get("three_d_vista_target_provenance"), dict)
        else {}
    )
    authorization = (
        dict(provenance.get("authorization") or {})
        if isinstance(provenance.get("authorization"), dict)
        else {}
    )
    review = (
        dict(provenance.get("review") or {})
        if isinstance(provenance.get("review"), dict)
        else {}
    )
    browser = (
        dict(private_payload.get("three_d_vista_browser_render_proof") or {})
        if isinstance(private_payload.get("three_d_vista_browser_render_proof"), dict)
        else {}
    )
    white_label = (
        dict(private_payload.get("three_d_vista_white_label_proof") or {})
        if isinstance(private_payload.get("three_d_vista_white_label_proof"), dict)
        else {}
    )
    if (
        str(provenance.get("status") or "").strip().lower() != "pass"
        or str(provenance.get("provider") or "").strip().lower() != "3dvista"
        or str(provenance.get("target_slug") or "").strip() != slug
        or str(authorization.get("status") or "").strip().lower() != "approved"
        or str(review.get("property_match") or "").strip().lower() != "pass"
        or str(review.get("visual_match") or "").strip().lower() != "pass"
        or str(browser.get("status") or "").strip().lower() != "pass"
        or str(browser.get("provider") or "").strip().lower() != "3dvista"
        or not bool(browser.get("rendered_viewer"))
        or not bool(browser.get("interactive_viewer"))
        or not bool(white_label.get("non_trial_export_verified"))
        or bool(white_label.get("trial_branding_present"))
    ):
        raise ValueError("tour_candidate_binding_3dvista_proof_invalid")


def bind_property_tour_candidate(
    *,
    public_tour_dir: Path,
    slug: str,
    principal_id: str,
    property_url_sha256: str,
    receipt_path: Path | None = None,
) -> dict[str, object]:
    normalized_slug = str(slug or "").strip().lower()
    normalized_principal = str(principal_id or "").strip()
    normalized_property_sha256 = str(property_url_sha256 or "").strip().lower()
    if _SLUG_RE.fullmatch(normalized_slug) is None:
        raise ValueError("tour_candidate_binding_slug_invalid")
    if _PRINCIPAL_RE.fullmatch(normalized_principal) is None:
        raise ValueError("tour_candidate_binding_principal_invalid")
    if _SHA256_RE.fullmatch(normalized_property_sha256) is None:
        raise ValueError("tour_candidate_binding_property_digest_invalid")

    public_root = public_tour_dir.expanduser().resolve()
    bundle_dir = (public_root / normalized_slug).resolve()
    if (
        public_root not in bundle_dir.parents
        or bundle_dir.is_symlink()
        or not bundle_dir.is_dir()
    ):
        raise ValueError("tour_candidate_binding_bundle_invalid")
    public_path = bundle_dir / "tour.json"
    private_path = bundle_dir / "tour.private.json"

    with property_tour_publication_lock(
        public_dir=public_root,
        slug=normalized_slug,
    ):
        public_payload, public_before = _read_json_file(public_path, private=False)
        private_payload, private_before = _read_json_file(private_path, private=True)
        if (
            str(public_payload.get("slug") or "").strip() != normalized_slug
            or str(public_payload.get("publication_status") or "").strip().lower()
            != "ready"
            or str(public_payload.get("property_url_sha256") or "").strip().lower()
            != normalized_property_sha256
        ):
            raise ValueError("tour_candidate_binding_public_identity_invalid")
        existing_principal = str(private_payload.get("principal_id") or "").strip()
        if existing_principal and existing_principal != normalized_principal:
            raise ValueError("tour_candidate_binding_principal_conflict")
        _require_verified_3dvista_proof(private_payload, slug=normalized_slug)
        safe_video_fields = _validated_safe_video_fields(private_payload)
        if not str(public_payload.get("video_relpath") or "").strip():
            raise ValueError("tour_candidate_binding_walkthrough_missing")

        # Provider and coverage attestations are owner-scoped receipt data.
        # Keep them in the mode-0600 private manifest; authenticated helpers
        # merge them only after the principal binding succeeds.
        for key in safe_video_fields:
            public_payload.pop(key, None)
        private_payload.update(safe_video_fields)
        private_payload["principal_id"] = normalized_principal
        _write_json_atomic(public_path, public_payload, mode=0o644)
        _write_json_atomic(private_path, private_payload, mode=0o600)
        public_after = public_path.read_bytes()
        private_after = private_path.read_bytes()

    receipt = {
        "contract_name": "propertyquarry.tour_candidate_binding.v1",
        "status": "bound",
        "slug": normalized_slug,
        "property_url_sha256": normalized_property_sha256,
        "principal_id_sha256": _sha256(normalized_principal.encode("utf-8")),
        "public_manifest_sha256_before": _sha256(public_before),
        "public_manifest_sha256_after": _sha256(public_after),
        "private_manifest_sha256_before": _sha256(private_before),
        "private_manifest_sha256_after": _sha256(private_after),
        "walkthrough_provider": safe_video_fields["video_provider_key"],
        "walkthrough_coverage": safe_video_fields["video_coverage_proof"],
        "spatial_provider": "3dvista",
        "bound_at": datetime.now(timezone.utc).isoformat(),
    }
    if receipt_path is not None:
        resolved_receipt_path = receipt_path.expanduser().resolve()
        resolved_receipt_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(resolved_receipt_path, receipt, mode=0o600)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bind a verified hosted tour bundle to one PropertyQuarry principal."
    )
    parser.add_argument("--slug", required=True)
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--property-url-sha256", required=True)
    parser.add_argument("--public-tour-dir", default=os.getenv("EA_PUBLIC_TOUR_DIR") or "/data/public_property_tours")
    parser.add_argument("--receipt", default="")
    args = parser.parse_args()
    receipt = bind_property_tour_candidate(
        public_tour_dir=Path(args.public_tour_dir),
        slug=args.slug,
        principal_id=args.principal_id,
        property_url_sha256=args.property_url_sha256,
        receipt_path=Path(args.receipt) if str(args.receipt or "").strip() else None,
    )
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
