#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any


THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA = "propertyquarry.3dvista_target_provenance.v1"
THREE_D_VISTA_PROVENANCE_FILENAMES = (
    "3dvista-target-provenance.json",
    "3dvista-provenance.json",
    "provenance.json",
)


def safe_relpath(value: object) -> str:
    raw = str(value or "").strip().replace("\\", "/").lstrip("/")
    raw_parts = [part for part in raw.split("/") if part]
    parts = [part for part in raw_parts if part not in {".", ".."}]
    if not parts or len(parts) != len(raw_parts):
        return ""
    return "/".join(parts)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(str(value).strip().encode("utf-8")).hexdigest()


def export_tree_sha256(export_dir: Path) -> str:
    root = export_dir.expanduser().resolve()
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.parent == root and path.name in THREE_D_VISTA_PROVENANCE_FILENAMES:
            continue
        if path.is_symlink():
            raise ValueError("3dvista_export_symlink_not_allowed")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("3dvista_export_special_file_not_allowed")
        relpath = path.relative_to(root).as_posix()
        file_digest = sha256_file(path)
        digest.update(relpath.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(path.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
        file_count += 1
    return digest.hexdigest() if file_count else ""


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def find_3dvista_provenance_receipt(export_dir: Path) -> Path | None:
    for name in THREE_D_VISTA_PROVENANCE_FILENAMES:
        candidate = export_dir / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def _valid_sha256(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        return ""
    return normalized


def _valid_reviewed_at(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return ""
    return raw


def validate_3dvista_target_provenance(
    receipt: dict[str, Any],
    *,
    target_slug: str,
    export_dir: Path | None = None,
    entry_relpath: str = "",
    provider_url: str = "",
) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    expected_slug = str(target_slug or "").strip()
    artifact = dict(receipt.get("artifact") or {}) if isinstance(receipt.get("artifact"), dict) else {}
    authorization = (
        dict(receipt.get("authorization") or {})
        if isinstance(receipt.get("authorization"), dict)
        else {}
    )
    review = dict(receipt.get("review") or {}) if isinstance(receipt.get("review"), dict) else {}

    schema = str(receipt.get("schema") or "").strip()
    status = str(receipt.get("status") or "").strip().lower()
    provider = str(receipt.get("provider") or "").strip().lower()
    receipt_slug = str(receipt.get("target_slug") or "").strip()
    evidence_kind = str(artifact.get("kind") or "").strip().lower()
    evidence_sha256 = _valid_sha256(artifact.get("sha256"))
    receipt_entry = safe_relpath(artifact.get("entry_relpath"))
    authorization_status = str(authorization.get("status") or "").strip().lower()
    authorization_reference = str(authorization.get("reference") or "").strip()
    property_match = str(review.get("property_match") or "").strip().lower()
    visual_match = str(review.get("visual_match") or "").strip().lower()
    reviewed_by = str(review.get("reviewed_by") or "").strip()
    reviewed_at = _valid_reviewed_at(review.get("reviewed_at"))

    if schema != THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA:
        errors.append("schema_invalid")
    if status != "pass":
        errors.append("status_not_pass")
    if provider != "3dvista":
        errors.append("provider_mismatch")
    if not expected_slug or receipt_slug != expected_slug:
        errors.append("target_slug_mismatch")
    if evidence_kind not in {"local_export", "hosted_url"}:
        errors.append("artifact_kind_invalid")
    if not evidence_sha256:
        errors.append("artifact_sha256_invalid")
    if authorization_status != "approved":
        errors.append("authorization_not_approved")
    if not authorization_reference:
        errors.append("authorization_reference_missing")
    if property_match != "pass":
        errors.append("property_match_not_pass")
    if visual_match != "pass":
        errors.append("visual_match_not_pass")
    if not reviewed_by:
        errors.append("reviewer_missing")
    if not reviewed_at:
        errors.append("reviewed_at_invalid")

    actual_sha256 = ""
    expected_entry = safe_relpath(entry_relpath)
    if evidence_kind == "local_export":
        if not receipt_entry:
            errors.append("artifact_entry_relpath_invalid")
        if expected_entry and receipt_entry != expected_entry:
            errors.append("artifact_entry_relpath_mismatch")
        if export_dir is None:
            errors.append("local_export_missing")
        else:
            try:
                actual_sha256 = export_tree_sha256(export_dir)
            except (OSError, ValueError):
                actual_sha256 = ""
            if not actual_sha256:
                errors.append("local_export_unhashable")
    elif evidence_kind == "hosted_url":
        normalized_url = str(provider_url or "").strip()
        if not normalized_url:
            errors.append("hosted_url_missing")
        else:
            actual_sha256 = sha256_text(normalized_url)
    if evidence_sha256 and actual_sha256 and evidence_sha256 != actual_sha256:
        errors.append("artifact_sha256_mismatch")

    normalized = {
        "schema": THREE_D_VISTA_TARGET_PROVENANCE_SCHEMA,
        "status": status,
        "provider": provider,
        "target_slug": receipt_slug,
        "artifact": {
            "kind": evidence_kind,
            "sha256": evidence_sha256,
            "entry_relpath": receipt_entry,
        },
        "authorization": {
            "status": authorization_status,
            "reference": authorization_reference,
        },
        "review": {
            "property_match": property_match,
            "visual_match": visual_match,
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
        },
    }
    return normalized, list(dict.fromkeys(errors))
