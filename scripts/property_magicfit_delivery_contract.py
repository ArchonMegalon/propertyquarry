#!/usr/bin/env python3
"""Closed, reproducible MagicFit delivery contracts.

This module is deliberately pure: every importer, reviewer, accepter, and
post-acceptance verifier reconstructs the same candidate manifest bytes from
the exact base manifest and a closed transform subject.  A staged manifest is
never an authority for its own contents.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import PurePosixPath
from typing import Any, Mapping
from urllib.parse import quote, unquote, urlsplit, urlunsplit


PENDING_DELIVERY_CONTRACT = "propertyquarry.magicfit_delivery_pending.v2"
ACCEPTED_DELIVERY_CONTRACT = "propertyquarry.magicfit_delivery_acceptance.v4"
DELIVERY_REVIEW_CONTRACT = "propertyquarry.magicfit_delivery_review.v4"
EVIDENCE_CONTRACT = "propertyquarry.magicfit_e2e_evidence.v3"
BROWSER_RECEIPT_CONTRACT = "propertyquarry.magicfit_browser_playback.v3"
VISUAL_REVIEW_CONTRACT = "propertyquarry.magicfit_private_visual_review.v3"
MANIFEST_TRANSFORM_CONTRACT = "propertyquarry.magicfit_manifest_transform.v1"
AUDIT_CONTRACT = "propertyquarry.magicfit_delivery_audit.v1"
REVIEW_RECEIPT_BUNDLE_CONTRACT = (
    "propertyquarry.magicfit_private_review_receipt_bundle.v1"
)

REVIEW_RECEIPT_BUNDLE_MANIFEST_NAME = "bundle-manifest.json"
REVIEW_RECEIPT_BUNDLE_ARTIFACT_FILENAMES = {
    "browser_receipt": "browser-receipt.json",
    "evidence_receipt": "evidence-receipt.json",
}
REVIEW_RECEIPT_BUNDLE_MANIFEST_FIELDS = frozenset(
    {"contract_name", "delivery_digest", "artifacts"}
)
REVIEW_RECEIPT_BUNDLE_ARTIFACT_FIELDS = frozenset(
    {"filename", "sha256", "size_bytes"}
)

PENDING_POINTER_RELPATH = "tour.magicfit.pending.json"
STAGING_ROOT_RELPATH = ".magicfit-staging"
DELIVERIES_ROOT_RELPATH = ".magicfit-deliveries"
PUBLIC_VIDEO_EXTENSIONS = frozenset({".mp4", ".m4v", ".mov", ".webm"})
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
MAGICFIT_SOURCE_PROVIDER_FIELDS = (
    "provider",
    "provider_backend_key",
    "provider_key",
)
MAGICFIT_SOURCE_SLUG_FIELDS = (
    "target_slug",
    "tour_slug",
    "property_slug",
    "slug",
)
MAGICFIT_SOURCE_HOSTED_URL_FIELDS = (
    "hosted_walkthrough_video_url",
    "video_output_url",
)
MAGICFIT_COVERAGE_FIELDS = (
    "walkthrough_coverage_proof",
    "magicfit_walkthrough_coverage",
    "walkthrough_quality_receipt",
    "coverage_proof",
)
MAGICFIT_APPROVED_RENDER_STATUSES = frozenset(
    {"completed", "rendered", "success", "succeeded"}
)
MAGICFIT_APPROVED_HOSTS = frozenset(
    {"cdn.pushowl.com", "media.powlcdn.com"}
)

PENDING_SIDECAR_FIELDS = frozenset(
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
        "tour_slug",
        "requested_target_relpath",
        "video_relpath",
        "video_sha256",
        "video_size_bytes",
        "source_receipt_sha256",
        "coverage_proof",
        "generated_at",
        "base_manifest_sha256",
        "staged_video_relpath",
        "staged_manifest_relpath",
        "staged_manifest_sha256",
        "accepted_sidecar_relpath",
    }
)

AUDIT_ARTIFACT_NAMES = (
    "base_manifest",
    "source_receipt",
    "browser_receipt",
    "evidence_receipt",
    "visual_review",
    "reviewer_authority",
    "contact_sheet",
)
AUDIT_ENTRY_FIELDS = frozenset({"relpath", "sha256", "size_bytes"})
AUDIT_FIELDS = frozenset({"contract_name", "artifacts"})


class MagicFitContractError(ValueError):
    pass


class _DuplicateJsonKey(ValueError):
    pass


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"nonfinite:{value}")


def strict_json_object_bytes(body: bytes, *, reason: str) -> dict[str, Any]:
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_nonfinite_json,
        )
    except Exception as exc:
        raise MagicFitContractError(f"{reason}:{type(exc).__name__}") from exc
    if not isinstance(payload, dict):
        raise MagicFitContractError(reason)
    return dict(payload)


def canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    try:
        return (
            json.dumps(
                dict(payload),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MagicFitContractError(
            f"magicfit_contract_json_invalid:{type(exc).__name__}"
        ) from exc


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def valid_sha256(value: object) -> str:
    return value if isinstance(value, str) and SHA256_RE.fullmatch(value) else ""


def require_positive_json_integer(value: object) -> int:
    """Return an exact positive JSON integer, rejecting bools and coercions."""

    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MagicFitContractError("magicfit_positive_json_integer_invalid")
    return value


def require_positive_json_number(value: object) -> float:
    """Return a finite positive JSON number without accepting bools or strings."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MagicFitContractError("magicfit_positive_json_number_invalid")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise MagicFitContractError("magicfit_positive_json_number_invalid") from exc
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise MagicFitContractError("magicfit_positive_json_number_invalid")
    return normalized


def canonical_relpath(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    if value.startswith("/") or "\\" in value:
        return ""
    if any(part in {"", ".", ".."} for part in value.split("/")):
        return ""
    if any(
        ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        return ""
    return value if PurePosixPath(value).as_posix() == value else ""


def digest_bound_video_relpath(requested_relpath: str, video_sha256: str) -> str:
    requested = canonical_relpath(requested_relpath)
    digest = valid_sha256(video_sha256)
    if not requested or not digest:
        raise MagicFitContractError("magicfit_transform_target_invalid")
    requested_path = PurePosixPath(requested)
    suffix = requested_path.suffix.lower()
    if suffix not in PUBLIC_VIDEO_EXTENSIONS:
        raise MagicFitContractError("magicfit_transform_target_invalid")
    filename = f"{requested_path.stem}.{digest}{suffix}"
    final_path = (PurePosixPath("magicfit-media") / filename).as_posix()
    if len(final_path) > 768 or any(
        len(part) > 255 for part in final_path.split("/")
    ):
        raise MagicFitContractError("magicfit_transform_target_invalid")
    return final_path


def _source_text(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    if any(
        character.isspace()
        or ord(character) < 0x20
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        return ""
    return value


def _normalized_magicfit_hosted_url(value: object) -> str:
    raw = _source_text(value)
    if not raw or "\\" in raw:
        return ""
    try:
        parsed = urlsplit(raw)
        host = str(parsed.hostname or "").lower()
        port = parsed.port
        decoded_path = unquote(parsed.path, errors="strict")
    except (UnicodeError, ValueError):
        return ""
    if (
        parsed.scheme.lower() != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or host not in MAGICFIT_APPROVED_HOSTS
        or parsed.fragment
        or not decoded_path.startswith("/magicfit/")
        or "\\" in decoded_path
        or any(
            character.isspace()
            or ord(character) < 0x20
            or ord(character) == 0x7F
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in decoded_path
        )
    ):
        return ""
    path_parts = decoded_path.split("/")
    if any(part in {"", ".", ".."} for part in path_parts[2:]):
        return ""
    if PurePosixPath(decoded_path).suffix.lower() not in PUBLIC_VIDEO_EXTENSIONS:
        return ""
    normalized_path = quote(
        decoded_path,
        safe="/!$&'()*+,-.:;=@_~",
    )
    return urlunsplit(("https", host, normalized_path, parsed.query, ""))


def validate_magicfit_source_receipt(
    payload: Mapping[str, object], *, slug: str
) -> str:
    """Validate one closed MagicFit provenance profile for every consumer.

    Provider identity aliases are optional because historical provider exports
    do not all carry the same alias, but every alias that is present must be
    the exact literal ``magicfit``.  Slug and hosted-URL aliases fail closed on
    disagreement instead of allowing one valid alias to mask another.
    """

    if not isinstance(payload, Mapping):
        raise MagicFitContractError("magicfit_source_receipt_invalid")
    expected_slug = _source_text(slug)
    if (
        not expected_slug
        or canonical_relpath(expected_slug) != expected_slug
        or "/" in expected_slug
    ):
        raise MagicFitContractError("magicfit_source_receipt_slug_invalid")
    for field in MAGICFIT_SOURCE_PROVIDER_FIELDS:
        if field in payload and payload.get(field) != "magicfit":
            raise MagicFitContractError(
                "magicfit_source_receipt_provider_invalid"
            )
    if payload.get("render_status") not in MAGICFIT_APPROVED_RENDER_STATUSES:
        raise MagicFitContractError("magicfit_source_receipt_status_invalid")

    present_slugs = [
        payload.get(field)
        for field in MAGICFIT_SOURCE_SLUG_FIELDS
        if field in payload
    ]
    if not present_slugs or any(value != expected_slug for value in present_slugs):
        raise MagicFitContractError("magicfit_source_receipt_slug_invalid")

    normalized_urls: list[str] = []
    for field in MAGICFIT_SOURCE_HOSTED_URL_FIELDS:
        if field not in payload:
            continue
        normalized = _normalized_magicfit_hosted_url(payload.get(field))
        if not normalized:
            raise MagicFitContractError("magicfit_source_receipt_url_invalid")
        normalized_urls.append(normalized)
    if not normalized_urls or len(set(normalized_urls)) != 1:
        raise MagicFitContractError("magicfit_source_receipt_url_invalid")
    return normalized_urls[0]


def _coverage_segment(value: object) -> str:
    segment = _source_text(value)
    if not segment or len(segment.encode("utf-8")) > 255:
        raise MagicFitContractError("magicfit_coverage_proof_invalid")
    return segment


def _coverage_segments(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        raise MagicFitContractError("magicfit_coverage_proof_invalid")
    normalized = [_coverage_segment(item) for item in value]
    if len(normalized) != len(set(normalized)):
        raise MagicFitContractError("magicfit_coverage_proof_invalid")
    return normalized


def validate_magicfit_coverage_proof(
    value: object,
) -> dict[str, object]:
    """Return one independent, canonical, fully covered passing proof."""

    if not isinstance(value, dict):
        raise MagicFitContractError("magicfit_coverage_proof_invalid")
    proof = value
    if proof.get("status") != "pass":
        raise MagicFitContractError("magicfit_coverage_proof_invalid")
    expected = _coverage_segments(proof.get("segments_expected"))
    visited = _coverage_segments(proof.get("segments_visited"))
    if set(visited) != set(expected):
        raise MagicFitContractError("magicfit_coverage_proof_invalid")
    intervals = proof.get("coverage_segments")
    if not isinstance(intervals, list) or len(intervals) != len(expected):
        raise MagicFitContractError("magicfit_coverage_proof_invalid")
    interval_segments: list[str] = []
    for interval in intervals:
        if not isinstance(interval, dict) or set(interval) != {
            "segment",
            "start",
            "end",
        }:
            raise MagicFitContractError("magicfit_coverage_proof_invalid")
        segment = _coverage_segment(interval.get("segment"))
        start = interval.get("start")
        end = interval.get("end")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
        ):
            raise MagicFitContractError("magicfit_coverage_proof_invalid")
        try:
            normalized_start = float(start)
            normalized_end = float(end)
        except (OverflowError, TypeError, ValueError) as exc:
            raise MagicFitContractError(
                "magicfit_coverage_proof_invalid"
            ) from exc
        if (
            not math.isfinite(normalized_start)
            or not math.isfinite(normalized_end)
            or normalized_start < 0.0
            or normalized_end <= normalized_start
        ):
            raise MagicFitContractError("magicfit_coverage_proof_invalid")
        interval_segments.append(segment)
    if (
        len(interval_segments) != len(set(interval_segments))
        or set(interval_segments) != set(expected)
    ):
        raise MagicFitContractError("magicfit_coverage_proof_invalid")
    return strict_json_object_bytes(
        canonical_json_bytes(proof), reason="magicfit_coverage_proof_invalid"
    )


def coverage_proof_from_receipt(payload: Mapping[str, object]) -> dict[str, object]:
    supplied: list[dict[str, object]] = []
    for key in MAGICFIT_COVERAGE_FIELDS:
        if key not in payload:
            continue
        supplied.append(validate_magicfit_coverage_proof(payload.get(key)))
    if not supplied:
        return {}
    canonical = canonical_json_bytes(supplied[0])
    if any(canonical_json_bytes(value) != canonical for value in supplied[1:]):
        raise MagicFitContractError("magicfit_coverage_proof_ambiguous")
    return supplied[0]


def delivery_digest(
    *,
    slug: str,
    requested_target_relpath: str,
    video_relpath: str,
    video_sha256: str,
    video_size_bytes: int,
    source_receipt_sha256: str,
    base_manifest_sha256: str,
    generated_at: str,
    coverage_proof: Mapping[str, object],
) -> str:
    normalized_slug = canonical_relpath(slug)
    requested = canonical_relpath(requested_target_relpath)
    final_video = canonical_relpath(video_relpath)
    video_digest = valid_sha256(video_sha256)
    source_digest = valid_sha256(source_receipt_sha256)
    base_digest = valid_sha256(base_manifest_sha256)
    if (
        not normalized_slug
        or "/" in normalized_slug
        or not requested
        or final_video != digest_bound_video_relpath(requested, video_digest)
        or not source_digest
        or not base_digest
        or isinstance(video_size_bytes, bool)
        or not isinstance(video_size_bytes, int)
        or video_size_bytes <= 0
        or not isinstance(generated_at, str)
        or not generated_at
        or not isinstance(coverage_proof, Mapping)
    ):
        raise MagicFitContractError("magicfit_delivery_subject_invalid")
    try:
        normalized_coverage = (
            validate_magicfit_coverage_proof(dict(coverage_proof))
            if coverage_proof
            else {}
        )
    except MagicFitContractError as exc:
        raise MagicFitContractError("magicfit_delivery_subject_invalid") from exc
    subject = {
        "base_manifest_sha256": base_digest,
        "coverage_proof": normalized_coverage,
        "generated_at": generated_at,
        "manifest_transform_contract": MANIFEST_TRANSFORM_CONTRACT,
        "requested_target_relpath": requested,
        "slug": normalized_slug,
        "source_receipt_sha256": source_digest,
        "video_relpath": final_video,
        "video_sha256": video_digest,
        "video_size_bytes": video_size_bytes,
    }
    return sha256_bytes(canonical_json_bytes(subject))


def accepted_sidecar_relpath(delivery_digest_value: str) -> str:
    digest = valid_sha256(delivery_digest_value)
    if not digest:
        raise MagicFitContractError("magicfit_delivery_digest_invalid")
    return f"{DELIVERIES_ROOT_RELPATH}/{digest}.json"


def staged_video_relpath(delivery_digest_value: str, suffix: str) -> str:
    digest = valid_sha256(delivery_digest_value)
    normalized_suffix = str(suffix or "").lower()
    if not digest or normalized_suffix not in PUBLIC_VIDEO_EXTENSIONS:
        raise MagicFitContractError("magicfit_delivery_digest_invalid")
    return f"{STAGING_ROOT_RELPATH}/{digest}/video{normalized_suffix}"


def staged_manifest_relpath(delivery_digest_value: str) -> str:
    digest = valid_sha256(delivery_digest_value)
    if not digest:
        raise MagicFitContractError("magicfit_delivery_digest_invalid")
    return f"{STAGING_ROOT_RELPATH}/{digest}/tour.json"


def audit_relpaths(delivery_digest_value: str) -> dict[str, str]:
    digest = valid_sha256(delivery_digest_value)
    if not digest:
        raise MagicFitContractError("magicfit_delivery_digest_invalid")
    prefix = f"{DELIVERIES_ROOT_RELPATH}/{digest}"
    return {
        "base_manifest": f"{prefix}.base-manifest.json",
        "source_receipt": f"{prefix}.source-receipt.json",
        "browser_receipt": f"{prefix}.browser-receipt.json",
        "evidence_receipt": f"{prefix}.evidence-receipt.json",
        "visual_review": f"{prefix}.visual-review.json",
        "reviewer_authority": f"{prefix}.reviewer-authority.bin",
        "contact_sheet": f"{prefix}.contact-sheet.bin",
    }


def build_candidate_manifest_bytes(
    *,
    base_manifest_bytes: bytes,
    slug: str,
    requested_target_relpath: str,
    video_relpath: str,
    video_sha256: str,
    video_size_bytes: int,
    source_receipt_sha256: str,
    generated_at: str,
    coverage_proof: Mapping[str, object],
) -> bytes:
    base = strict_json_object_bytes(
        base_manifest_bytes, reason="magicfit_base_manifest_invalid"
    )
    normalized_slug = canonical_relpath(slug)
    requested = canonical_relpath(requested_target_relpath)
    final_video = canonical_relpath(video_relpath)
    video_digest = valid_sha256(video_sha256)
    source_digest = valid_sha256(source_receipt_sha256)
    if (
        not normalized_slug
        or "/" in normalized_slug
        or base.get("slug") != normalized_slug
        or not requested
        or final_video != digest_bound_video_relpath(requested, video_digest)
        or not source_digest
        or not isinstance(generated_at, str)
        or not generated_at
        or not isinstance(coverage_proof, Mapping)
    ):
        raise MagicFitContractError("magicfit_manifest_transform_invalid")

    try:
        coverage = (
            validate_magicfit_coverage_proof(dict(coverage_proof))
            if coverage_proof
            else {}
        )
        normalized_video_size = require_positive_json_integer(video_size_bytes)
        digest = delivery_digest(
            slug=normalized_slug,
            requested_target_relpath=requested,
            video_relpath=final_video,
            video_sha256=video_digest,
            video_size_bytes=normalized_video_size,
            source_receipt_sha256=source_digest,
            base_manifest_sha256=sha256_bytes(base_manifest_bytes),
            generated_at=generated_at,
            coverage_proof=coverage,
        )
        sidecar = accepted_sidecar_relpath(digest)
    except MagicFitContractError as exc:
        raise MagicFitContractError("magicfit_manifest_transform_invalid") from exc

    activated = dict(base)
    activated["video_provider"] = "magicfit"
    activated["video_provider_backend_key"] = "magicfit"
    activated["video_relpath"] = final_video
    activated["video_sidecar_relpath"] = sidecar
    magicfit_import: dict[str, object] = {
        "source": "magicfit_rendered_walkthrough",
        "provider_backend_key": "magicfit",
        "proof_status": "delivery_accepted",
        "imported_at": generated_at,
        "requested_target_relpath": requested,
        "target_relpath": final_video,
        "sha256": video_digest,
        "size_bytes": normalized_video_size,
        "source_receipt_sha256": source_digest,
        "delivery_sidecar_relpath": sidecar,
    }
    if coverage:
        activated["video_coverage_proof"] = "route_coverage_verified"
        activated["walkthrough_coverage_proof"] = coverage
        magicfit_import["coverage_proof"] = coverage
    else:
        activated["video_coverage_proof"] = "provider_render_verified"
        activated.pop("walkthrough_coverage_proof", None)
    activated["magicfit_import"] = magicfit_import
    return canonical_json_bytes(activated)


def require_exact_candidate_manifest(
    *,
    staged_manifest_bytes: bytes,
    **transform: object,
) -> dict[str, Any]:
    expected = build_candidate_manifest_bytes(**transform)  # type: ignore[arg-type]
    if staged_manifest_bytes != expected:
        raise MagicFitContractError("magicfit_staged_manifest_not_exact_transform")
    return strict_json_object_bytes(
        staged_manifest_bytes, reason="magicfit_staged_manifest_invalid"
    )


def build_audit_entry(*, relpath: str, body: bytes) -> dict[str, object]:
    canonical = canonical_relpath(relpath)
    if not canonical or not isinstance(body, bytes) or not body:
        raise MagicFitContractError("magicfit_audit_entry_invalid")
    return {
        "relpath": canonical,
        "sha256": sha256_bytes(body),
        "size_bytes": len(body),
    }


def validate_audit_map(
    payload: object, *, delivery_digest_value: str
) -> dict[str, dict[str, object]]:
    if not isinstance(payload, dict) or set(payload) != AUDIT_FIELDS:
        raise MagicFitContractError("magicfit_audit_contract_invalid")
    if payload.get("contract_name") != AUDIT_CONTRACT:
        raise MagicFitContractError("magicfit_audit_contract_invalid")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(
        AUDIT_ARTIFACT_NAMES
    ):
        raise MagicFitContractError("magicfit_audit_contract_invalid")
    expected_paths = audit_relpaths(delivery_digest_value)
    normalized: dict[str, dict[str, object]] = {}
    for name in AUDIT_ARTIFACT_NAMES:
        entry = artifacts.get(name)
        if not isinstance(entry, dict) or set(entry) != AUDIT_ENTRY_FIELDS:
            raise MagicFitContractError("magicfit_audit_contract_invalid")
        relpath = canonical_relpath(entry.get("relpath"))
        digest = valid_sha256(entry.get("sha256"))
        size = entry.get("size_bytes")
        if (
            relpath != expected_paths[name]
            or not digest
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise MagicFitContractError("magicfit_audit_contract_invalid")
        normalized[name] = {
            "relpath": relpath,
            "sha256": digest,
            "size_bytes": size,
        }
    return normalized
