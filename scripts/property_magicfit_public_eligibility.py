#!/usr/bin/env python3
"""Shared fail-closed public eligibility for accepted MagicFit walkthroughs.

This module is intentionally HTTP- and CLI-agnostic.  The release verifier and
the request-serving application call the same exact-v4 validator.  Small
positive and short-lived negative caches avoid repeatedly hashing accepted
videos.  Cached decisions remain usable only while every integrity subject
retains its complete filesystem identity; negative decisions additionally
bind every attempted file and external reviewer-trust dependency and expire
quickly.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading
import time
from typing import Mapping

try:
    from property_magicfit_contact_sheet import (
        CONTACT_SHEET_MAX_BYTES,
        MagicFitContactSheetError,
        validate_magicfit_contact_sheet_bytes,
    )
    from property_magicfit_delivery_contract import (
        ACCEPTED_DELIVERY_CONTRACT,
        AUDIT_ARTIFACT_NAMES,
        BROWSER_RECEIPT_CONTRACT,
        DELIVERY_REVIEW_CONTRACT,
        EVIDENCE_CONTRACT,
        MANIFEST_TRANSFORM_CONTRACT,
        PUBLIC_VIDEO_EXTENSIONS,
        VISUAL_REVIEW_CONTRACT,
        accepted_sidecar_relpath,
        canonical_relpath,
        coverage_proof_from_receipt,
        delivery_digest as contract_delivery_digest,
        require_exact_candidate_manifest,
        require_positive_json_integer,
        require_positive_json_number,
        strict_json_object_bytes,
        valid_sha256,
        validate_audit_map,
        validate_magicfit_source_receipt,
    )
    from property_magicfit_secure_io import (
        MagicFitSecureIOError,
        StableFileSnapshot,
        hash_stable_bounded_file,
        lexical_absolute_path,
        read_stable_bounded_bytes,
        stat_regular_file_identity,
    )
    from property_magicfit_reviewer_authority import (
        AUTHORIZATION_MAX_BYTES,
        MagicFitReviewerAuthorityError,
        REVIEWER_TRUST_STORE_ENV,
        TRUST_STORE_MAX_BYTES,
        magicfit_reviewer_test_allowed_owner_uids,
        verify_magicfit_reviewer_authorization_bytes,
    )
except ModuleNotFoundError:
    from scripts.property_magicfit_contact_sheet import (  # type: ignore[no-redef]
        CONTACT_SHEET_MAX_BYTES,
        MagicFitContactSheetError,
        validate_magicfit_contact_sheet_bytes,
    )
    from scripts.property_magicfit_delivery_contract import (  # type: ignore[no-redef]
        ACCEPTED_DELIVERY_CONTRACT,
        AUDIT_ARTIFACT_NAMES,
        BROWSER_RECEIPT_CONTRACT,
        DELIVERY_REVIEW_CONTRACT,
        EVIDENCE_CONTRACT,
        MANIFEST_TRANSFORM_CONTRACT,
        PUBLIC_VIDEO_EXTENSIONS,
        VISUAL_REVIEW_CONTRACT,
        accepted_sidecar_relpath,
        canonical_relpath,
        coverage_proof_from_receipt,
        delivery_digest as contract_delivery_digest,
        require_exact_candidate_manifest,
        require_positive_json_integer,
        require_positive_json_number,
        strict_json_object_bytes,
        valid_sha256,
        validate_audit_map,
        validate_magicfit_source_receipt,
    )
    from scripts.property_magicfit_secure_io import (  # type: ignore[no-redef]
        MagicFitSecureIOError,
        StableFileSnapshot,
        hash_stable_bounded_file,
        lexical_absolute_path,
        read_stable_bounded_bytes,
        stat_regular_file_identity,
    )
    from scripts.property_magicfit_reviewer_authority import (  # type: ignore[no-redef]
        AUTHORIZATION_MAX_BYTES,
        MagicFitReviewerAuthorityError,
        REVIEWER_TRUST_STORE_ENV,
        TRUST_STORE_MAX_BYTES,
        magicfit_reviewer_test_allowed_owner_uids,
        verify_magicfit_reviewer_authorization_bytes,
    )


_PROVIDER_FIELDS = (
    "video_provider",
    "video_provider_key",
    "video_provider_backend_key",
    "video_render_provider",
)
_ACCEPTED_SIDECAR_FIELDS = frozenset(
    {
        "contract_name",
        "provider",
        "provider_key",
        "provider_backend_key",
        "render_status",
        "status",
        "acceptance_status",
        "launch_eligible",
        "manifest_transform_contract",
        "requested_target_relpath",
        "video_relpath",
        "video_sha256",
        "video_size_bytes",
        "source_receipt_sha256",
        "coverage_proof",
        "base_manifest_sha256",
        "staged_manifest_sha256",
        "delivery_digest",
        "generated_at",
        "review",
        "audit",
    }
)
_REVIEW_FIELDS = frozenset(
    {
        "contract_name",
        "reviewed_at",
        "reviewer_authority_sha256",
        "reviewer_authorization",
        "evidence_sha256",
        "visual_review_sha256",
        "subject",
        "checklist",
    }
)
_REVIEWER_AUTHORIZATION_PROJECTION_FIELDS = frozenset(
    {
        "contract_name",
        "algorithm",
        "key_id",
        "authority_id",
        "delivery_digest",
        "reviewed_at",
        "issued_at",
        "expires_at",
        "authorization_sha256",
        "signing_payload_sha256",
        "subject_sha256",
        "trust_store_sha256",
        "public_key_record_sha256",
        "public_key_sha256",
    }
)
_REVIEWER_AUTHORIZATION_STABLE_PROJECTION_FIELDS = (
    _REVIEWER_AUTHORIZATION_PROJECTION_FIELDS
    - {"trust_store_sha256", "public_key_record_sha256"}
)
_REVIEW_SUBJECT_FIELDS = frozenset(
    {
        "tour_slug",
        "provider",
        "delivery_contract_name",
        "manifest_transform_contract",
        "requested_target_relpath",
        "source_receipt_sha256",
        "video_relpath",
        "video_sha256",
        "video_size_bytes",
        "coverage_proof",
        "base_manifest_sha256",
        "staged_manifest_sha256",
        "delivery_digest",
    }
)
_REVIEW_CHECKS = frozenset(
    {
        "playback_to_end",
        "continuous_walkthrough",
        "no_visible_rotation_jump",
        "intended_property_and_scope",
        "no_sensitive_or_trial_branding",
    }
)
_EVIDENCE_FIELDS = frozenset(
    {
        "schema",
        "status",
        "provider",
        "target_slug",
        "observed_at",
        "source_receipt_sha256",
        "base_manifest_sha256",
        "staged_manifest_sha256",
        "delivery_digest",
        "video",
        "checklist",
        "artifacts",
    }
)
_BROWSER_FIELDS = frozenset(
    {
        "schema",
        "status",
        "provider",
        "target_slug",
        "observed_at",
        "route",
        "http_status",
        "video_sha256",
        "base_manifest_sha256",
        "staged_manifest_sha256",
        "delivery_digest",
        "duration_seconds",
        "final_current_time",
        "playback_to_end",
        "video_error",
        "console_errors",
        "request_failures",
        "benign_request_aborts",
        "bad_responses",
    }
)
_VISUAL_FIELDS = frozenset(
    {
        "schema",
        "status",
        "provider",
        "target_slug",
        "observed_at",
        "video_sha256",
        "base_manifest_sha256",
        "staged_manifest_sha256",
        "delivery_digest",
        "checklist",
    }
)
_AUDIT_MAX_BYTES = {
    "base_manifest": 8 * 1024 * 1024,
    "source_receipt": 8 * 1024 * 1024,
    "browser_receipt": 64 * 1024,
    "evidence_receipt": 8 * 1024 * 1024,
    "visual_review": 64 * 1024,
    "reviewer_authority": AUTHORIZATION_MAX_BYTES,
    "contact_sheet": CONTACT_SHEET_MAX_BYTES,
}
_MANIFEST_MAX_BYTES = 8 * 1024 * 1024
_SIDECAR_MAX_BYTES = 8 * 1024 * 1024
_VIDEO_MAX_BYTES = 2 * 1024 * 1024 * 1024
_FUTURE_SKEW = timedelta(minutes=5)
_UTC_TIMESTAMP_RE = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)\Z"
)
_CACHE_LIMIT = 64
_NEGATIVE_CACHE_TTL_SECONDS = 15.0
_NEGATIVE_DEPENDENCY_LIMIT = 128


FileIdentity = tuple[int, int, int, int, int, int, int]


@dataclass(frozen=True)
class MagicFitIntegritySubject:
    path: str
    identity: FileIdentity


@dataclass(frozen=True)
class MagicFitPublicEligibility:
    declared: bool
    eligible: bool
    reason: str
    video_relpath: str = ""
    delivery_digest: str = ""
    cache_hit: bool = False
    subjects: tuple[MagicFitIntegritySubject, ...] = field(
        default=(), repr=False
    )
    reviewer_authorization_bytes: bytes = field(default=b"", repr=False)
    reviewer_authorization_subject: tuple[tuple[str, str], ...] = field(
        default=(), repr=False
    )
    reviewer_authorization_projection: tuple[tuple[str, str], ...] = field(
        default=(), repr=False
    )
    reviewer_authorization_observed_at: datetime | None = field(
        default=None, repr=False
    )


@dataclass(frozen=True)
class _ValidationOutcome:
    result: MagicFitPublicEligibility
    cacheable: bool


class _EligibilityInvalid(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason)
        super().__init__(self.reason)


@dataclass(frozen=True)
class _CacheEntry:
    result: MagicFitPublicEligibility


@dataclass(frozen=True)
class _NegativeCacheEntry:
    result: MagicFitPublicEligibility
    payload_sha256: str
    trust_store_configuration: str
    dependency_paths: tuple[str, ...]
    dependency_generation: tuple[tuple[str, tuple[int, ...]], ...]
    expires_at_monotonic: float


_CACHE_CONDITION = threading.Condition(threading.RLock())
_POSITIVE_CACHE: OrderedDict[str, _CacheEntry] = OrderedDict()
_NEGATIVE_CACHE: OrderedDict[str, _NegativeCacheEntry] = OrderedDict()
_IN_FLIGHT: set[str] = set()


def clear_magicfit_public_eligibility_cache() -> None:
    """Clear cached decisions; exposed for deterministic tests and reloads."""

    with _CACHE_CONDITION:
        _POSITIVE_CACHE.clear()
        _NEGATIVE_CACHE.clear()
        _CACHE_CONDITION.notify_all()


def _reviewer_trusted_owner_uids_for_tests() -> list[int] | None:
    try:
        return magicfit_reviewer_test_allowed_owner_uids()
    except MagicFitReviewerAuthorityError as exc:
        raise _EligibilityInvalid(exc.reason) from exc


def _reviewer_projection_still_authorized(
    current: Mapping[str, str], accepted: Mapping[str, object]
) -> bool:
    """Compare immutable authorization/key facts, not trust-file formatting.

    Trust-store and public-key-record digests are acceptance-time audit facts.
    Their current files may change during a safe key-set rotation; the current
    verifier still enforces revocation, identity, validity, signature, and the
    exact public key bytes on every call.
    """

    return all(
        current.get(field_name) == accepted.get(field_name)
        for field_name in _REVIEWER_AUTHORIZATION_STABLE_PROJECTION_FIELDS
    )


def magicfit_provider_declared(payload: Mapping[str, object] | object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    for field_name in _PROVIDER_FIELDS:
        value = payload.get(field_name)
        if isinstance(value, str) and value.strip().lower() == "magicfit":
            return True
    return False


def magicfit_footprint_present(payload: Mapping[str, object] | object) -> bool:
    """Detect MagicFit authority material even when provider aliases are tampered.

    Provider aliases are assertions, not a classification boundary.  Accepted
    sidecar paths, MagicFit import state, and the dedicated public media
    namespace all remain MagicFit-controlled after an alias is removed or
    changed and therefore must continue through the exact-v4 validator.
    """

    if not isinstance(payload, Mapping):
        return False
    if magicfit_provider_declared(payload):
        return True
    for field_name in (
        "video_sidecar_relpath",
        "walkthrough_sidecar_relpath",
    ):
        value = payload.get(field_name)
        if not isinstance(value, str):
            continue
        normalized = value.strip().replace("\\", "/").lower()
        if (
            normalized.startswith(".magicfit-deliveries/")
            or normalized == "tour.magicfit.pending.json"
            or normalized.endswith(".magicfit.json")
        ):
            return True
    if "magicfit_import" in payload:
        return True
    generated = payload.get("generated_reconstruction")
    candidates: list[object] = [
        payload.get("video_relpath"),
        payload.get("video_mobile_relpath"),
        payload.get("flythrough_video_relpath"),
    ]
    if isinstance(generated, Mapping):
        candidates.extend(
            (
                generated.get("walkthrough_video_relpath"),
                generated.get("walkthrough_sidecar_relpath"),
            )
        )
    return any(
        isinstance(value, str)
        and value.strip().replace("\\", "/").lower().startswith(
            "magicfit-media/"
        )
        for value in candidates
    )


def _strict_utc(value: object, *, require_z: bool) -> datetime:
    if (
        not isinstance(value, str)
        or _UTC_TIMESTAMP_RE.fullmatch(value) is None
        or (require_z and not value.endswith("Z"))
    ):
        raise _EligibilityInvalid("magicfit_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _EligibilityInvalid("magicfit_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise _EligibilityInvalid("magicfit_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _now_utc(value: datetime | None) -> datetime:
    now = value or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise _EligibilityInvalid("magicfit_validation_clock_invalid")
    return now.astimezone(timezone.utc)


def _require_snapshot(snapshot: StableFileSnapshot) -> StableFileSnapshot:
    identity = snapshot.identity
    if len(identity) != 7 or identity[3] != 1 or snapshot.size_bytes <= 0:
        raise _EligibilityInvalid("magicfit_integrity_subject_invalid")
    return snapshot


def _read_bytes(
    path: Path,
    *,
    maximum_bytes: int,
    subjects: list[MagicFitIntegritySubject],
    dependency_paths: list[Path] | None = None,
) -> bytes:
    if dependency_paths is not None:
        dependency_paths.append(path)
    try:
        snapshot = _require_snapshot(
            read_stable_bounded_bytes(
                path,
                reason="magicfit_public_integrity_subject_invalid",
                maximum_bytes=maximum_bytes,
            )
        )
    except MagicFitSecureIOError as exc:
        raise _EligibilityInvalid("magicfit_integrity_subject_invalid") from exc
    assert snapshot.body is not None
    subjects.append(
        MagicFitIntegritySubject(str(path), snapshot.identity)
    )
    return snapshot.body


def _hash_file(
    path: Path,
    *,
    maximum_bytes: int,
    subjects: list[MagicFitIntegritySubject],
    prefix_bytes: int = 0,
    dependency_paths: list[Path] | None = None,
) -> StableFileSnapshot:
    if dependency_paths is not None:
        dependency_paths.append(path)
    try:
        snapshot = _require_snapshot(
            hash_stable_bounded_file(
                path,
                reason="magicfit_public_integrity_subject_invalid",
                maximum_bytes=maximum_bytes,
                prefix_bytes=prefix_bytes,
            )
        )
    except MagicFitSecureIOError as exc:
        raise _EligibilityInvalid("magicfit_integrity_subject_invalid") from exc
    subjects.append(
        MagicFitIntegritySubject(str(path), snapshot.identity)
    )
    return snapshot


def _bundle_asset(bundle_dir: Path, relpath: object) -> tuple[str, Path]:
    canonical = canonical_relpath(relpath)
    if not canonical:
        raise _EligibilityInvalid("magicfit_asset_relpath_invalid")
    return canonical, bundle_dir / PurePosixPath(canonical)


def _provider_profile_exact(payload: Mapping[str, object]) -> bool:
    values: list[str] = []
    for field_name in _PROVIDER_FIELDS:
        if field_name not in payload:
            continue
        value = payload.get(field_name)
        if not isinstance(value, str) or value != value.strip():
            return False
        values.append(value.lower())
    return bool(values) and all(value == "magicfit" for value in values)


def _supplied_payload_matches_active(
    supplied: Mapping[str, object], active: Mapping[str, object]
) -> bool:
    # A private receipt may add keys to the route payload.  Every active public
    # manifest key must nevertheless still have the exact value that was read
    # from the descriptor-bound tour.json snapshot.
    return all(key in supplied and supplied.get(key) == value for key, value in active.items())


def _video_prefix_valid(relpath: str, prefix: bytes) -> bool:
    if len(prefix) < 12:
        return False
    suffix = PurePosixPath(relpath).suffix.lower()
    if suffix in {".mp4", ".m4v", ".mov"}:
        return b"ftyp" in prefix[:32]
    if suffix == ".webm":
        return prefix.startswith(b"\x1aE\xdf\xa3")
    return False


def _receipt_subject_timestamp(
    payload: Mapping[str, object],
    *,
    fields: frozenset[str],
    schema: str,
    slug: str,
    video_sha256: str,
    base_manifest_sha256: str,
    staged_manifest_sha256: str,
    delivery_digest: str,
    generated_at: datetime,
    now: datetime,
) -> datetime:
    if (
        set(payload) != fields
        or payload.get("schema") != schema
        or payload.get("status") != "pass"
        or payload.get("provider") != "magicfit"
        or payload.get("target_slug") != slug
        or payload.get("video_sha256") != video_sha256
        or payload.get("base_manifest_sha256") != base_manifest_sha256
        or payload.get("staged_manifest_sha256") != staged_manifest_sha256
        or payload.get("delivery_digest") != delivery_digest
    ):
        raise _EligibilityInvalid("magicfit_review_subject_invalid")
    observed_at = _strict_utc(payload.get("observed_at"), require_z=True)
    if observed_at < generated_at or observed_at > now + _FUTURE_SKEW:
        raise _EligibilityInvalid("magicfit_review_timestamp_invalid")
    return observed_at


def _subjects_unchanged(
    subjects: tuple[MagicFitIntegritySubject, ...],
) -> bool:
    if len(subjects) != 10 or len({subject.path for subject in subjects}) != 10:
        return False
    for subject in subjects:
        try:
            identity = stat_regular_file_identity(
                subject.path,
                reason="magicfit_public_integrity_subject_changed",
            )
        except MagicFitSecureIOError:
            return False
        if identity != subject.identity or identity[3] != 1:
            return False
    return True


def _validate_magicfit_uncached(
    bundle_dir: Path,
    supplied_payload: Mapping[str, object],
    *,
    now: datetime,
    dependency_paths: list[Path] | None = None,
) -> _ValidationOutcome:
    dependencies = dependency_paths if dependency_paths is not None else []
    subjects: list[MagicFitIntegritySubject] = []
    active_manifest_bytes = _read_bytes(
        bundle_dir / "tour.json",
        maximum_bytes=_MANIFEST_MAX_BYTES,
        subjects=subjects,
        dependency_paths=dependencies,
    )
    try:
        active = strict_json_object_bytes(
            active_manifest_bytes, reason="magicfit_active_manifest_invalid"
        )
    except ValueError as exc:
        raise _EligibilityInvalid("magicfit_active_manifest_invalid") from exc

    active_footprint = magicfit_footprint_present(active)
    supplied_footprint = magicfit_footprint_present(supplied_payload)
    active_declares = magicfit_provider_declared(active)
    supplied_declares = magicfit_provider_declared(supplied_payload)
    if not active_footprint:
        if supplied_footprint:
            raise _EligibilityInvalid("magicfit_manifest_payload_changed")
        return _ValidationOutcome(
            MagicFitPublicEligibility(False, True, "not_magicfit"), False
        )
    if (
        not active_declares
        or not supplied_declares
        or not supplied_footprint
        or not _supplied_payload_matches_active(supplied_payload, active)
        or not _provider_profile_exact(active)
    ):
        raise _EligibilityInvalid("magicfit_manifest_payload_changed")

    slug = active.get("slug")
    if (
        not isinstance(slug, str)
        or not slug
        or slug != slug.strip()
        or "/" in slug
        or canonical_relpath(slug) != slug
        or slug != bundle_dir.name
    ):
        raise _EligibilityInvalid("magicfit_manifest_slug_invalid")

    active_video_relpath, video_path = _bundle_asset(
        bundle_dir, active.get("video_relpath")
    )
    if PurePosixPath(active_video_relpath).suffix.lower() not in PUBLIC_VIDEO_EXTENSIONS:
        raise _EligibilityInvalid("magicfit_video_relpath_invalid")
    sidecar_relpath, sidecar_path = _bundle_asset(
        bundle_dir, active.get("video_sidecar_relpath")
    )
    magicfit_import = active.get("magicfit_import")
    if (
        not isinstance(magicfit_import, dict)
        or magicfit_import.get("delivery_sidecar_relpath") != sidecar_relpath
    ):
        raise _EligibilityInvalid("magicfit_sidecar_binding_invalid")
    sidecar_parts = PurePosixPath(sidecar_relpath).parts
    if (
        len(sidecar_parts) != 2
        or sidecar_parts[0] != ".magicfit-deliveries"
        or not sidecar_parts[1].endswith(".json")
        or not valid_sha256(sidecar_parts[1][:-5])
    ):
        raise _EligibilityInvalid("magicfit_sidecar_path_invalid")

    sidecar_bytes = _read_bytes(
        sidecar_path,
        maximum_bytes=_SIDECAR_MAX_BYTES,
        subjects=subjects,
        dependency_paths=dependencies,
    )
    try:
        sidecar = strict_json_object_bytes(
            sidecar_bytes, reason="magicfit_accepted_sidecar_invalid"
        )
    except ValueError as exc:
        raise _EligibilityInvalid("magicfit_accepted_sidecar_invalid") from exc
    if set(sidecar) != _ACCEPTED_SIDECAR_FIELDS:
        raise _EligibilityInvalid("magicfit_accepted_sidecar_fields_invalid")
    exact_sidecar = {
        "contract_name": ACCEPTED_DELIVERY_CONTRACT,
        "provider": "magicfit",
        "provider_key": "magicfit",
        "provider_backend_key": "magicfit",
        "render_status": "completed",
        "status": "delivery_accepted",
        "acceptance_status": "accepted",
        "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
    }
    if (
        any(sidecar.get(key) != value for key, value in exact_sidecar.items())
        or sidecar.get("launch_eligible") is not True
    ):
        raise _EligibilityInvalid("magicfit_accepted_sidecar_status_invalid")

    delivery_digest = valid_sha256(sidecar.get("delivery_digest"))
    video_sha256 = valid_sha256(sidecar.get("video_sha256"))
    source_receipt_sha256 = valid_sha256(sidecar.get("source_receipt_sha256"))
    base_manifest_sha256 = valid_sha256(sidecar.get("base_manifest_sha256"))
    staged_manifest_sha256 = valid_sha256(sidecar.get("staged_manifest_sha256"))
    requested_target_relpath = canonical_relpath(
        sidecar.get("requested_target_relpath")
    )
    receipt_video_relpath = canonical_relpath(sidecar.get("video_relpath"))
    coverage_proof = sidecar.get("coverage_proof")
    try:
        video_size_bytes = require_positive_json_integer(
            sidecar.get("video_size_bytes")
        )
    except ValueError as exc:
        raise _EligibilityInvalid("magicfit_video_size_invalid") from exc
    if (
        not delivery_digest
        or not video_sha256
        or not source_receipt_sha256
        or not base_manifest_sha256
        or not staged_manifest_sha256
        or not requested_target_relpath
        or receipt_video_relpath != active_video_relpath
        or not isinstance(coverage_proof, dict)
        or video_size_bytes > _VIDEO_MAX_BYTES
        or sidecar_relpath != accepted_sidecar_relpath(delivery_digest)
    ):
        raise _EligibilityInvalid("magicfit_accepted_sidecar_subject_invalid")

    generated_at = _strict_utc(sidecar.get("generated_at"), require_z=False)
    if generated_at > now + _FUTURE_SKEW:
        raise _EligibilityInvalid("magicfit_generated_at_invalid")
    try:
        expected_delivery_digest = contract_delivery_digest(
            slug=slug,
            requested_target_relpath=requested_target_relpath,
            video_relpath=active_video_relpath,
            video_sha256=video_sha256,
            video_size_bytes=video_size_bytes,
            source_receipt_sha256=source_receipt_sha256,
            base_manifest_sha256=base_manifest_sha256,
            generated_at=str(sidecar["generated_at"]),
            coverage_proof=coverage_proof,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _EligibilityInvalid("magicfit_delivery_digest_invalid") from exc
    if delivery_digest != expected_delivery_digest:
        raise _EligibilityInvalid("magicfit_delivery_digest_invalid")

    review = sidecar.get("review")
    if not isinstance(review, dict) or set(review) != _REVIEW_FIELDS:
        raise _EligibilityInvalid("magicfit_review_invalid")
    reviewed_at = _strict_utc(review.get("reviewed_at"), require_z=True)
    if (
        review.get("contract_name") != DELIVERY_REVIEW_CONTRACT
        or reviewed_at < generated_at
        or reviewed_at > now + _FUTURE_SKEW
    ):
        raise _EligibilityInvalid("magicfit_review_invalid")
    authority_sha256 = valid_sha256(review.get("reviewer_authority_sha256"))
    evidence_sha256 = valid_sha256(review.get("evidence_sha256"))
    visual_review_sha256 = valid_sha256(review.get("visual_review_sha256"))
    if not authority_sha256 or not evidence_sha256 or not visual_review_sha256:
        raise _EligibilityInvalid("magicfit_review_digest_invalid")
    reviewer_authorization_projection = review.get("reviewer_authorization")
    if (
        not isinstance(reviewer_authorization_projection, dict)
        or set(reviewer_authorization_projection)
        != _REVIEWER_AUTHORIZATION_PROJECTION_FIELDS
        or not all(
            isinstance(value, str) and value
            for value in reviewer_authorization_projection.values()
        )
        or reviewer_authorization_projection.get("delivery_digest")
        != delivery_digest
        or reviewer_authorization_projection.get("reviewed_at")
        != review.get("reviewed_at")
        or reviewer_authorization_projection.get("authorization_sha256")
        != authority_sha256
    ):
        raise _EligibilityInvalid("magicfit_reviewer_authorization_invalid")
    authorization_issued_at = _strict_utc(
        reviewer_authorization_projection.get("issued_at"), require_z=True
    )
    if authorization_issued_at > now + _FUTURE_SKEW:
        raise _EligibilityInvalid("magicfit_reviewer_authorization_invalid")
    expected_review_subject = {
        "tour_slug": slug,
        "provider": "magicfit",
        "delivery_contract_name": ACCEPTED_DELIVERY_CONTRACT,
        "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
        "requested_target_relpath": requested_target_relpath,
        "source_receipt_sha256": source_receipt_sha256,
        "video_relpath": active_video_relpath,
        "video_sha256": video_sha256,
        "video_size_bytes": video_size_bytes,
        "coverage_proof": coverage_proof,
        "base_manifest_sha256": base_manifest_sha256,
        "staged_manifest_sha256": staged_manifest_sha256,
        "delivery_digest": delivery_digest,
    }
    if (
        not isinstance(review.get("subject"), dict)
        or set(review["subject"]) != _REVIEW_SUBJECT_FIELDS
        or review["subject"] != expected_review_subject
    ):
        raise _EligibilityInvalid("magicfit_review_subject_invalid")
    review_checklist = review.get("checklist")
    if (
        not isinstance(review_checklist, dict)
        or set(review_checklist) != _REVIEW_CHECKS
        or not all(review_checklist.get(key) is True for key in _REVIEW_CHECKS)
    ):
        raise _EligibilityInvalid("magicfit_review_checklist_invalid")

    try:
        audit = validate_audit_map(
            sidecar.get("audit"), delivery_digest_value=delivery_digest
        )
    except ValueError as exc:
        raise _EligibilityInvalid("magicfit_audit_contract_invalid") from exc
    audit_bodies: dict[str, bytes] = {}
    for name in AUDIT_ARTIFACT_NAMES:
        entry = audit[name]
        size = entry.get("size_bytes")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or size > _AUDIT_MAX_BYTES[name]
        ):
            raise _EligibilityInvalid("magicfit_audit_size_invalid")
        relpath, path = _bundle_asset(bundle_dir, entry.get("relpath"))
        if relpath != entry.get("relpath"):
            raise _EligibilityInvalid("magicfit_audit_path_invalid")
        body = _read_bytes(
            path,
            maximum_bytes=_AUDIT_MAX_BYTES[name],
            subjects=subjects,
            dependency_paths=dependencies,
        )
        if len(body) != size:
            raise _EligibilityInvalid("magicfit_audit_size_mismatch")
        if hashlib.sha256(body).hexdigest() != entry.get("sha256"):
            raise _EligibilityInvalid("magicfit_audit_digest_mismatch")
        audit_bodies[name] = body

    if (
        hashlib.sha256(audit_bodies["base_manifest"]).hexdigest()
        != base_manifest_sha256
        or hashlib.sha256(audit_bodies["source_receipt"]).hexdigest()
        != source_receipt_sha256
    ):
        raise _EligibilityInvalid("magicfit_source_binding_invalid")

    try:
        source_receipt = strict_json_object_bytes(
            audit_bodies["source_receipt"],
            reason="magicfit_source_receipt_invalid",
        )
        validate_magicfit_source_receipt(source_receipt, slug=slug)
        if coverage_proof_from_receipt(source_receipt) != coverage_proof:
            raise _EligibilityInvalid("magicfit_coverage_binding_invalid")
        require_exact_candidate_manifest(
            staged_manifest_bytes=active_manifest_bytes,
            base_manifest_bytes=audit_bodies["base_manifest"],
            slug=slug,
            requested_target_relpath=requested_target_relpath,
            video_relpath=active_video_relpath,
            video_sha256=video_sha256,
            video_size_bytes=video_size_bytes,
            source_receipt_sha256=source_receipt_sha256,
            generated_at=str(sidecar["generated_at"]),
            coverage_proof=coverage_proof,
        )
        browser = strict_json_object_bytes(
            audit_bodies["browser_receipt"],
            reason="magicfit_browser_receipt_invalid",
        )
        evidence = strict_json_object_bytes(
            audit_bodies["evidence_receipt"],
            reason="magicfit_evidence_receipt_invalid",
        )
        visual = strict_json_object_bytes(
            audit_bodies["visual_review"],
            reason="magicfit_visual_review_invalid",
        )
    except _EligibilityInvalid:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise _EligibilityInvalid("magicfit_audit_payload_invalid") from exc

    if hashlib.sha256(active_manifest_bytes).hexdigest() != staged_manifest_sha256:
        raise _EligibilityInvalid("magicfit_active_manifest_digest_invalid")
    evidence_body_sha256 = hashlib.sha256(
        audit_bodies["evidence_receipt"]
    ).hexdigest()
    visual_body_sha256 = hashlib.sha256(audit_bodies["visual_review"]).hexdigest()
    browser_body_sha256 = hashlib.sha256(audit_bodies["browser_receipt"]).hexdigest()
    authority_body_sha256 = hashlib.sha256(
        audit_bodies["reviewer_authority"]
    ).hexdigest()
    contact_sheet_sha256 = hashlib.sha256(
        audit_bodies["contact_sheet"]
    ).hexdigest()
    if (
        evidence_body_sha256 != evidence_sha256
        or visual_body_sha256 != visual_review_sha256
        or authority_body_sha256 != authority_sha256
    ):
        raise _EligibilityInvalid("magicfit_review_binding_invalid")
    try:
        validate_magicfit_contact_sheet_bytes(audit_bodies["contact_sheet"])
    except MagicFitContactSheetError as exc:
        raise _EligibilityInvalid("magicfit_contact_sheet_invalid") from exc

    browser_observed_at = _receipt_subject_timestamp(
        browser,
        fields=_BROWSER_FIELDS,
        schema=BROWSER_RECEIPT_CONTRACT,
        slug=slug,
        video_sha256=video_sha256,
        base_manifest_sha256=base_manifest_sha256,
        staged_manifest_sha256=staged_manifest_sha256,
        delivery_digest=delivery_digest,
        generated_at=generated_at,
        now=now,
    )
    visual_observed_at = _receipt_subject_timestamp(
        visual,
        fields=_VISUAL_FIELDS,
        schema=VISUAL_REVIEW_CONTRACT,
        slug=slug,
        video_sha256=video_sha256,
        base_manifest_sha256=base_manifest_sha256,
        staged_manifest_sha256=staged_manifest_sha256,
        delivery_digest=delivery_digest,
        generated_at=generated_at,
        now=now,
    )
    try:
        browser_duration = require_positive_json_number(
            browser.get("duration_seconds")
        )
        final_current_time = require_positive_json_number(
            browser.get("final_current_time")
        )
    except ValueError as exc:
        raise _EligibilityInvalid("magicfit_browser_timing_invalid") from exc
    review_route = f"operator-review://propertyquarry/magicfit/{slug}/{video_sha256}"
    expected_abort = {
        "failure": "net::ERR_ABORTED",
        "method": "GET",
        "resource_type": "media",
        "route": review_route,
    }
    benign_aborts = browser.get("benign_request_aborts")
    if (
        browser.get("route") != review_route
        or browser.get("http_status") != 200
        or browser.get("playback_to_end") is not True
        or browser.get("video_error") is not None
        or browser.get("console_errors") != []
        or browser.get("request_failures") != []
        or browser.get("bad_responses") != []
        or not isinstance(benign_aborts, list)
        or len(benign_aborts) > 1
        or any(row != expected_abort for row in benign_aborts)
        or not math.isfinite(browser_duration)
        or not math.isfinite(final_current_time)
        or final_current_time < browser_duration - 0.25
    ):
        raise _EligibilityInvalid("magicfit_browser_receipt_invalid")

    if (
        set(evidence) != _EVIDENCE_FIELDS
        or evidence.get("schema") != EVIDENCE_CONTRACT
        or evidence.get("status") != "pass"
        or evidence.get("provider") != "magicfit"
        or evidence.get("target_slug") != slug
        or evidence.get("source_receipt_sha256") != source_receipt_sha256
        or evidence.get("base_manifest_sha256") != base_manifest_sha256
        or evidence.get("staged_manifest_sha256") != staged_manifest_sha256
        or evidence.get("delivery_digest") != delivery_digest
    ):
        raise _EligibilityInvalid("magicfit_evidence_subject_invalid")
    evidence_observed_at = _strict_utc(evidence.get("observed_at"), require_z=True)
    if (
        evidence_observed_at < generated_at
        or evidence_observed_at > now + _FUTURE_SKEW
    ):
        raise _EligibilityInvalid("magicfit_evidence_timestamp_invalid")
    evidence_video = evidence.get("video")
    if not isinstance(evidence_video, dict) or set(evidence_video) != {
        "sha256",
        "size_bytes",
        "duration_seconds",
    }:
        raise _EligibilityInvalid("magicfit_evidence_video_invalid")
    try:
        evidence_size = require_positive_json_integer(
            evidence_video.get("size_bytes")
        )
        evidence_duration = require_positive_json_number(
            evidence_video.get("duration_seconds")
        )
    except ValueError as exc:
        raise _EligibilityInvalid("magicfit_evidence_video_invalid") from exc
    evidence_artifacts = evidence.get("artifacts")
    evidence_checklist = evidence.get("checklist")
    visual_checklist = visual.get("checklist")
    if (
        evidence_video.get("sha256") != video_sha256
        or evidence_size != video_size_bytes
        or abs(evidence_duration - browser_duration) > 0.1
        or not isinstance(evidence_artifacts, dict)
        or set(evidence_artifacts)
        != {
            "contact_sheet_sha256",
            "browser_receipt_sha256",
            "visual_review_sha256",
        }
        or evidence_artifacts.get("contact_sheet_sha256")
        != contact_sheet_sha256
        or evidence_artifacts.get("browser_receipt_sha256")
        != browser_body_sha256
        or evidence_artifacts.get("visual_review_sha256")
        != visual_body_sha256
        or not isinstance(evidence_checklist, dict)
        or set(evidence_checklist) != _REVIEW_CHECKS
        or evidence_checklist != visual_checklist
        or evidence_checklist != review_checklist
        or not all(evidence_checklist.get(key) is True for key in _REVIEW_CHECKS)
    ):
        raise _EligibilityInvalid("magicfit_evidence_binding_invalid")
    if reviewed_at < max(
        browser_observed_at, visual_observed_at, evidence_observed_at
    ):
        raise _EligibilityInvalid("magicfit_review_timestamp_invalid")

    reviewer_authorization_subject = {
        "delivery_digest": delivery_digest,
        "video_sha256": video_sha256,
        "staged_manifest_sha256": staged_manifest_sha256,
        "browser_receipt_sha256": browser_body_sha256,
        "evidence_receipt_sha256": evidence_body_sha256,
        "visual_review_sha256": visual_body_sha256,
        "contact_sheet_sha256": contact_sheet_sha256,
        "reviewed_at": str(review["reviewed_at"]),
    }
    try:
        verified_reviewer_authorization = (
            verify_magicfit_reviewer_authorization_bytes(
                audit_bodies["reviewer_authority"],
                expected_subject=reviewer_authorization_subject,
                public_tour_root=bundle_dir.parent,
                # Expiry limits initial admission.  Public serving verifies
                # the historical authorization at its signed issue instant,
                # while consulting current trust material so revocation is
                # effective immediately and an accepted asset does not simply
                # disappear when a short authorization window elapses.
                observed_at=authorization_issued_at,
                allowed_owner_uids=_reviewer_trusted_owner_uids_for_tests(),
            )
        )
    except MagicFitReviewerAuthorityError as exc:
        raise _EligibilityInvalid(
            "magicfit_reviewer_authorization_invalid"
        ) from exc
    if not _reviewer_projection_still_authorized(
        verified_reviewer_authorization.as_dict(),
        reviewer_authorization_projection,
    ):
        raise _EligibilityInvalid("magicfit_reviewer_authorization_invalid")

    video_snapshot = _hash_file(
        video_path,
        maximum_bytes=_VIDEO_MAX_BYTES,
        subjects=subjects,
        prefix_bytes=64,
        dependency_paths=dependencies,
    )
    if (
        video_snapshot.sha256 != video_sha256
        or video_snapshot.size_bytes != video_size_bytes
        or bool(stat.S_IMODE(video_snapshot.identity[2]) & 0o222)
        or not _video_prefix_valid(active_video_relpath, video_snapshot.prefix)
    ):
        raise _EligibilityInvalid("magicfit_active_video_invalid")

    stable_subjects = tuple(subjects)
    if not _subjects_unchanged(stable_subjects):
        raise _EligibilityInvalid("magicfit_integrity_subject_changed")
    timestamps = (
        generated_at,
        reviewed_at,
        browser_observed_at,
        visual_observed_at,
        evidence_observed_at,
    )
    result = MagicFitPublicEligibility(
        declared=True,
        eligible=True,
        reason="accepted_v4",
        video_relpath=active_video_relpath,
        delivery_digest=delivery_digest,
        subjects=stable_subjects,
        reviewer_authorization_bytes=audit_bodies["reviewer_authority"],
        reviewer_authorization_subject=tuple(
            sorted(reviewer_authorization_subject.items())
        ),
        reviewer_authorization_projection=tuple(
            sorted(reviewer_authorization_projection.items())
        ),
        reviewer_authorization_observed_at=authorization_issued_at,
    )
    # Clock-skew tolerance remains accepted, but a receipt timestamped after
    # this process's current clock is never retained in the positive cache.
    return _ValidationOutcome(
        result=result,
        cacheable=all(timestamp <= now for timestamp in timestamps),
    )


def _cache_key(bundle_dir: Path) -> str:
    return os.fspath(lexical_absolute_path(bundle_dir))


def _negative_payload_sha256(payload: Mapping[str, object]) -> str:
    try:
        body = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return ""
    return hashlib.sha256(body).hexdigest()


def _reviewer_trust_dependency_paths() -> tuple[str, tuple[Path, ...]]:
    configured = str(os.getenv(REVIEWER_TRUST_STORE_ENV) or "").strip()
    if not configured:
        return "", ()
    try:
        trust_path = lexical_absolute_path(configured)
    except Exception:
        return configured, ()
    paths: list[Path] = [trust_path]
    try:
        snapshot = read_stable_bounded_bytes(
            trust_path,
            reason="magicfit_reviewer_trust_dependency_invalid",
            maximum_bytes=TRUST_STORE_MAX_BYTES,
        )
        if snapshot.body is None:
            return configured, tuple(paths)
        payload = strict_json_object_bytes(
            snapshot.body,
            reason="magicfit_reviewer_trust_dependency_invalid",
        )
        keys = payload.get("keys")
        if not isinstance(keys, list) or len(keys) > 64:
            return configured, tuple(paths)
        trust_root = trust_path.parent
        for entry in keys:
            if not isinstance(entry, Mapping):
                continue
            relpath = canonical_relpath(entry.get("public_key_relpath"))
            if not relpath:
                continue
            candidate = lexical_absolute_path(
                trust_root / PurePosixPath(relpath)
            )
            if candidate == trust_root or trust_root not in candidate.parents:
                continue
            paths.append(candidate)
    except (MagicFitSecureIOError, TypeError, ValueError):
        pass
    return configured, tuple(paths)


def _negative_dependency_paths(
    bundle: Path,
    payload: Mapping[str, object],
    attempted: list[Path],
) -> tuple[str, tuple[str, ...]] | None:
    paths: list[Path] = [bundle, bundle / "tour.json", *attempted]
    relpath_values: list[object] = [
        payload.get("video_relpath"),
        payload.get("video_sidecar_relpath"),
        payload.get("walkthrough_sidecar_relpath"),
    ]
    magicfit_import = payload.get("magicfit_import")
    if isinstance(magicfit_import, Mapping):
        relpath_values.append(magicfit_import.get("delivery_sidecar_relpath"))
    generated = payload.get("generated_reconstruction")
    if isinstance(generated, Mapping):
        relpath_values.extend(
            (
                generated.get("walkthrough_video_relpath"),
                generated.get("walkthrough_sidecar_relpath"),
            )
        )
    for value in relpath_values:
        relpath = canonical_relpath(value)
        if relpath:
            paths.append(bundle / PurePosixPath(relpath))
    trust_configuration, trust_paths = _reviewer_trust_dependency_paths()
    paths.extend(trust_paths)

    deduplicated: list[str] = []
    seen: set[str] = set()
    for path in paths:
        try:
            normalized = os.fspath(lexical_absolute_path(path))
        except Exception:
            continue
        if normalized not in seen:
            seen.add(normalized)
            deduplicated.append(normalized)
    if not deduplicated or len(deduplicated) > _NEGATIVE_DEPENDENCY_LIMIT:
        return None
    return trust_configuration, tuple(deduplicated)


def _lstat_dependency_identity(path: Path) -> tuple[int, ...]:
    try:
        details = os.lstat(path)
    except OSError:
        return (-1,)
    return (
        int(details.st_dev),
        int(details.st_ino),
        int(details.st_mode),
        int(details.st_nlink),
        int(details.st_size),
        int(details.st_mtime_ns),
        int(details.st_ctime_ns),
        int(details.st_uid),
    )


def _negative_dependency_generation(
    dependency_paths: tuple[str, ...],
) -> tuple[tuple[str, tuple[int, ...]], ...]:
    generation: list[tuple[str, tuple[int, ...]]] = []
    included: set[str] = set()
    for raw_path in dependency_paths:
        path = Path(raw_path)
        for candidate in (path.parent, path):
            normalized = os.fspath(candidate)
            if normalized in included:
                continue
            included.add(normalized)
            generation.append(
                (normalized, _lstat_dependency_identity(candidate))
            )
    return tuple(generation)


def _cached_negative_result(
    key: str,
    payload: Mapping[str, object],
) -> MagicFitPublicEligibility | None:
    with _CACHE_CONDITION:
        entry = _NEGATIVE_CACHE.get(key)
    if entry is None:
        return None
    payload_sha256 = _negative_payload_sha256(payload)
    trust_configuration = str(os.getenv(REVIEWER_TRUST_STORE_ENV) or "").strip()
    if (
        not payload_sha256
        or payload_sha256 != entry.payload_sha256
        or trust_configuration != entry.trust_store_configuration
        or time.monotonic() >= entry.expires_at_monotonic
        or _negative_dependency_generation(entry.dependency_paths)
        != entry.dependency_generation
    ):
        with _CACHE_CONDITION:
            if _NEGATIVE_CACHE.get(key) is entry:
                _NEGATIVE_CACHE.pop(key, None)
        return None
    with _CACHE_CONDITION:
        if _NEGATIVE_CACHE.get(key) is not entry:
            return None
        _NEGATIVE_CACHE.move_to_end(key)
    return replace(entry.result, cache_hit=True)


def _cache_negative_result(
    *,
    key: str,
    bundle: Path,
    payload: Mapping[str, object],
    result: MagicFitPublicEligibility,
    attempted_paths: list[Path],
) -> None:
    payload_sha256 = _negative_payload_sha256(payload)
    dependencies = _negative_dependency_paths(bundle, payload, attempted_paths)
    if not payload_sha256 or dependencies is None:
        return
    trust_configuration, dependency_paths = dependencies
    entry = _NegativeCacheEntry(
        result=result,
        payload_sha256=payload_sha256,
        trust_store_configuration=trust_configuration,
        dependency_paths=dependency_paths,
        dependency_generation=_negative_dependency_generation(dependency_paths),
        expires_at_monotonic=time.monotonic() + _NEGATIVE_CACHE_TTL_SECONDS,
    )
    with _CACHE_CONDITION:
        _NEGATIVE_CACHE[key] = entry
        _NEGATIVE_CACHE.move_to_end(key)
        while len(_NEGATIVE_CACHE) > _CACHE_LIMIT:
            _NEGATIVE_CACHE.popitem(last=False)


def _cached_payload_matches(
    key: str,
    payload: Mapping[str, object],
    result: MagicFitPublicEligibility,
) -> bool:
    sidecar_relpath = accepted_sidecar_relpath(result.delivery_digest)
    magicfit_import = payload.get("magicfit_import")
    return bool(
        _provider_profile_exact(payload)
        and payload.get("slug") == Path(key).name
        and payload.get("video_relpath") == result.video_relpath
        and payload.get("video_sidecar_relpath") == sidecar_relpath
        and isinstance(magicfit_import, dict)
        and magicfit_import.get("delivery_sidecar_relpath") == sidecar_relpath
    )


def _cached_result(
    key: str, payload: Mapping[str, object]
) -> MagicFitPublicEligibility | None:
    with _CACHE_CONDITION:
        entry = _POSITIVE_CACHE.get(key)
    trust_valid = False
    if entry is not None:
        try:
            issued_at = entry.result.reviewer_authorization_observed_at
            if (
                issued_at is None
                or issued_at > datetime.now(timezone.utc) + _FUTURE_SKEW
            ):
                raise _EligibilityInvalid(
                    "magicfit_reviewer_authorization_invalid"
                )
            current_projection = verify_magicfit_reviewer_authorization_bytes(
                entry.result.reviewer_authorization_bytes,
                expected_subject=dict(
                    entry.result.reviewer_authorization_subject
                ),
                public_tour_root=Path(key).parent,
                observed_at=issued_at,
                allowed_owner_uids=_reviewer_trusted_owner_uids_for_tests(),
            ).as_dict()
            trust_valid = _reviewer_projection_still_authorized(
                current_projection,
                dict(entry.result.reviewer_authorization_projection),
            )
        except (MagicFitReviewerAuthorityError, _EligibilityInvalid):
            trust_valid = False
    if (
        entry is None
        or not _cached_payload_matches(key, payload, entry.result)
        or not _subjects_unchanged(entry.result.subjects)
        or not trust_valid
    ):
        if entry is not None:
            with _CACHE_CONDITION:
                if _POSITIVE_CACHE.get(key) is entry:
                    _POSITIVE_CACHE.pop(key, None)
        return None
    with _CACHE_CONDITION:
        if _POSITIVE_CACHE.get(key) is not entry:
            return None
        _POSITIVE_CACHE.move_to_end(key)
    return replace(entry.result, cache_hit=True)


def evaluate_magicfit_public_eligibility(
    bundle_dir: str | os.PathLike[str] | Path,
    payload: Mapping[str, object] | object,
    *,
    observed_at: datetime | None = None,
) -> MagicFitPublicEligibility:
    """Return the shared public decision without raising on untrusted files."""

    supplied = dict(payload) if isinstance(payload, Mapping) else {}
    supplied_declares = magicfit_footprint_present(supplied)
    try:
        bundle = lexical_absolute_path(bundle_dir)
        key = _cache_key(bundle)
        now = _now_utc(observed_at)
    except Exception:
        return MagicFitPublicEligibility(
            declared=supplied_declares,
            eligible=not supplied_declares,
            reason=("magicfit_bundle_invalid" if supplied_declares else "not_magicfit"),
        )

    # Custom clocks are validation/test inputs and intentionally bypass the
    # process cache; otherwise one caller could publish a decision made in a
    # different time domain.
    use_cache = observed_at is None and supplied_declares
    if use_cache:
        cached = _cached_result(key, supplied)
        if cached is not None:
            return cached
        cached_negative = _cached_negative_result(key, supplied)
        if cached_negative is not None:
            return cached_negative

    while True:
        with _CACHE_CONDITION:
            if key not in _IN_FLIGHT:
                _IN_FLIGHT.add(key)
                break
            _CACHE_CONDITION.wait()
        if use_cache:
            cached = _cached_result(key, supplied)
            if cached is not None:
                return cached
            cached_negative = _cached_negative_result(key, supplied)
            if cached_negative is not None:
                return cached_negative

    attempted_paths: list[Path] = []
    try:
        outcome = _validate_magicfit_uncached(
            bundle,
            supplied,
            now=now,
            dependency_paths=attempted_paths,
        )
        result = outcome.result
        if (
            use_cache
            and result.declared
            and result.eligible
            and outcome.cacheable
        ):
            with _CACHE_CONDITION:
                _NEGATIVE_CACHE.pop(key, None)
                _POSITIVE_CACHE[key] = _CacheEntry(result=result)
                _POSITIVE_CACHE.move_to_end(key)
                while len(_POSITIVE_CACHE) > _CACHE_LIMIT:
                    _POSITIVE_CACHE.popitem(last=False)
        return result
    except Exception:
        result = MagicFitPublicEligibility(
            declared=True,
            eligible=False,
            reason="magicfit_acceptance_invalid",
        )
        if use_cache:
            _cache_negative_result(
                key=key,
                bundle=bundle,
                payload=supplied,
                result=result,
                attempted_paths=attempted_paths,
            )
        return result
    finally:
        with _CACHE_CONDITION:
            _IN_FLIGHT.discard(key)
            _CACHE_CONDITION.notify_all()


def magicfit_public_eligible(
    bundle_dir: str | os.PathLike[str] | Path,
    payload: Mapping[str, object] | object,
) -> bool:
    """Small boolean API for call sites that do not need diagnostic fields."""

    result = evaluate_magicfit_public_eligibility(bundle_dir, payload)
    return result.declared and result.eligible
