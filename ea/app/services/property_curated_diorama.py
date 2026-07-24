from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Mapping


CURATED_DIORAMA_CONTRACT = "propertyquarry.curated_diorama_previews.v2"
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_CANDIDATE_REF_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{2,127}")
_LISTING_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{2,127}")
_REQUIRED_GOVERNANCE_REVIEWS = {
    "rights": "approved",
    "privacy": "approved",
    "provenance": "verified",
}
_ALLOWED_PREVIEW_KINDS = {
    "rendered_diorama",
    "illustrative_drawn_diorama",
}


def curated_diorama_governance_subject_sha256(
    *,
    asset_sha256: str,
    source_asset_sha256s: list[str],
) -> str:
    payload = {
        "asset_sha256": str(asset_sha256 or "").strip().lower(),
        "source_asset_sha256s": sorted({str(value or "").strip().lower() for value in source_asset_sha256s}),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _review_is_approved(
    value: object,
    *,
    required_status: str,
    governance_subject_sha256: str,
    now: datetime,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    if str(value.get("status") or "").strip().lower() != required_status:
        return False
    if not str(value.get("basis") or "").strip():
        return False
    if not str(value.get("reviewed_by") or "").strip():
        return False
    if str(value.get("subject_sha256") or "").strip().lower() != governance_subject_sha256:
        return False
    if _SHA256_PATTERN.fullmatch(str(value.get("evidence_sha256") or "").strip().lower()) is None:
        return False
    reviewed_at = str(value.get("reviewed_at") or "").strip()
    if not reviewed_at:
        return False
    try:
        parsed = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        return False
    return parsed.astimezone(timezone.utc) <= now.astimezone(timezone.utc) + timedelta(minutes=5)


def curated_diorama_entry_is_approved(
    entry: object,
    *,
    asset_sha256: str,
    now: datetime | None = None,
) -> bool:
    if not isinstance(entry, Mapping):
        return False
    governance = entry.get("governance")
    if not isinstance(governance, Mapping):
        return False
    source_asset_sha256s = entry.get("source_asset_sha256s")
    if not isinstance(source_asset_sha256s, list) or not source_asset_sha256s:
        return False
    normalized_source_hashes = [str(value or "").strip().lower() for value in source_asset_sha256s]
    if any(_SHA256_PATTERN.fullmatch(value) is None for value in normalized_source_hashes):
        return False
    if len(set(normalized_source_hashes)) != len(normalized_source_hashes):
        return False
    governance_subject_sha256 = curated_diorama_governance_subject_sha256(
        asset_sha256=asset_sha256,
        source_asset_sha256s=normalized_source_hashes,
    )
    effective_now = now or datetime.now(timezone.utc)
    return all(
        _review_is_approved(
            governance.get(review_name),
            required_status=required_status,
            governance_subject_sha256=governance_subject_sha256,
            now=effective_now,
        )
        for review_name, required_status in _REQUIRED_GOVERNANCE_REVIEWS.items()
    )


def build_curated_diorama_entry_index(
    payload: object,
    *,
    static_root: Path,
) -> dict[str, dict[str, object]]:
    if not isinstance(payload, Mapping) or payload.get("contract_name") != CURATED_DIORAMA_CONTRACT:
        return {}
    resolved_static_root = static_root.resolve()
    index: dict[str, dict[str, object]] = {}
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return index
    for raw_entry in entries:
        if not isinstance(raw_entry, Mapping):
            continue
        asset_url = str(raw_entry.get("asset_url") or "").strip()
        declared_sha256 = str(raw_entry.get("asset_sha256") or "").strip().lower()
        preview_kind = str(raw_entry.get("preview_kind") or "").strip()
        representation = str(raw_entry.get("representation") or "").strip().lower()
        source_basis = str(raw_entry.get("source_basis") or "").strip()
        truth_boundary = str(raw_entry.get("truth_boundary") or "").strip()
        if preview_kind not in _ALLOWED_PREVIEW_KINDS:
            continue
        if preview_kind == "illustrative_drawn_diorama" and (
            representation != "illustrative"
            or not source_basis
            or not truth_boundary
        ):
            continue
        if not asset_url.startswith("/static/property/research/") or _SHA256_PATTERN.fullmatch(declared_sha256) is None:
            continue
        if not curated_diorama_entry_is_approved(raw_entry, asset_sha256=declared_sha256):
            continue
        relative_asset = Path(asset_url.removeprefix("/static/"))
        if relative_asset.is_absolute() or ".." in relative_asset.parts:
            continue
        unresolved_asset_path = static_root / relative_asset
        relative_parts = (unresolved_asset_path.relative_to(static_root).parts if unresolved_asset_path.is_relative_to(static_root) else ())
        if not relative_parts:
            continue
        current_path = static_root
        if current_path.is_symlink():
            continue
        symlink_found = False
        for part in relative_parts:
            current_path = current_path / part
            if current_path.is_symlink():
                symlink_found = True
                break
        if symlink_found:
            continue
        asset_path = unresolved_asset_path.resolve()
        if resolved_static_root not in asset_path.parents or not asset_path.is_file():
            continue
        try:
            actual_sha256 = hashlib.sha256(asset_path.read_bytes()).hexdigest()
        except OSError:
            continue
        if not hmac.compare_digest(actual_sha256, declared_sha256):
            continue
        candidate_refs = raw_entry.get("candidate_refs")
        listing_ids = raw_entry.get("listing_ids")
        if not isinstance(candidate_refs, list) or not isinstance(listing_ids, list):
            continue
        normalized_entry = dict(raw_entry)
        normalized_entry["asset_url"] = asset_url
        normalized_entry["asset_sha256"] = declared_sha256
        normalized_entry["preview_kind"] = preview_kind
        normalized_entry["representation"] = representation
        normalized_entry["source_basis"] = source_basis
        normalized_entry["truth_boundary"] = truth_boundary
        entry_index: dict[str, dict[str, object]] = {}
        for candidate_ref in candidate_refs:
            normalized = str(candidate_ref or "").strip().lower()
            if _CANDIDATE_REF_PATTERN.fullmatch(normalized):
                entry_index[f"candidate:{normalized}"] = normalized_entry
        for listing_id in listing_ids:
            normalized = str(listing_id or "").strip().lower()
            if _LISTING_ID_PATTERN.fullmatch(normalized):
                entry_index[f"listing:{normalized}"] = normalized_entry
        if not entry_index:
            continue
        if any(
            key in index
            and str(index[key].get("asset_url") or "") != str(value.get("asset_url") or "")
            for key, value in entry_index.items()
        ):
            return {}
        index.update(entry_index)
    return index


def build_curated_diorama_preview_index(
    payload: object,
    *,
    static_root: Path,
) -> dict[str, str]:
    return {
        lookup_key: str(entry.get("asset_url") or "").strip()
        for lookup_key, entry in build_curated_diorama_entry_index(
            payload,
            static_root=static_root,
        ).items()
    }
