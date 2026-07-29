#!/usr/bin/env python3
"""Audit and repair the persisted PropertyQuarry public-tour volume.

The repair is deliberately lossless: an exact filesystem snapshot is required
before mutation, and private fields removed from ``tour.json`` are retained in
``tour.private.json``.  Receipts contain counts and digests only, never field
values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.api.routes.public_tour_payloads import (
    PrivateTourReceipt,
    canonical_public_tour_payload,
    public_tour_key_is_exact_location,
)


SCHEMA = "propertyquarry-public-tour-volume-privacy-v1"
SAFE_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EXPLICIT_PRIVATE_KEYS = frozenset(
    {
        "candidate_ref",
        "external_id",
        "listing_url",
        "owner_id",
        "person_id",
        "principal_id",
        "property_url",
        "recipient_email",
        "recipient_name",
        "recipient_phone",
        "search_run_id",
        "source_ref",
        "source_virtual_tour_url",
        "private_exact_location",
        "video_coverage_proof",
        "video_provider",
        "video_provider_key",
        "video_render_provider",
    }
)
PRIVATE_KEY_MARKERS = (
    "api_key",
    "auth_header",
    "authorization",
    "cookie",
    "debug",
    "password",
    "preference",
    "private_recipient",
    "recipient_",
    "refresh_token",
    "secret",
    "session",
    "shortlist",
)


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _read_object(path: Path) -> dict[str, object]:
    details = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or details.st_size <= 0
        or details.st_size > 16 * 1024 * 1024
    ):
        raise ValueError("manifest_not_bounded_regular_file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest_not_object")
    return dict(payload)


def _private_key(key: object) -> bool:
    normalized = str(key or "").strip().lower()
    return (
        normalized in EXPLICIT_PRIVATE_KEYS
        or public_tour_key_is_exact_location(normalized)
        or any(marker in normalized for marker in PRIVATE_KEY_MARKERS)
    )


def _private_snapshot(value: object) -> object | None:
    if isinstance(value, dict):
        selected: dict[str, object] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            if _private_key(key):
                selected[key] = child
                continue
            nested = _private_snapshot(child)
            if nested not in (None, {}, []):
                selected[key] = nested
        return selected or None
    if isinstance(value, list):
        selected_items = [_private_snapshot(child) for child in value]
        return selected_items if any(item is not None for item in selected_items) else None
    return None


def _contains_private_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            _private_key(key) or _contains_private_key(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_private_key(child) for child in value)
    return False


def _canonical_public_payload(
    payload: dict[str, object],
    *,
    bundle_dir: Path,
) -> dict[str, object]:
    return canonical_public_tour_payload(payload, bundle_dir=bundle_dir)


def _merge_private_receipt(
    public_payload: dict[str, object],
    existing: dict[str, object],
) -> dict[str, object]:
    generated = PrivateTourReceipt.from_payload(public_payload).as_dict()
    merged = {
        key: value
        for key, value in generated.items()
        if value not in ("", {}, [], None)
    }
    merged.update(existing)
    legacy = _private_snapshot(public_payload)
    if legacy not in (None, {}, []):
        prior = merged.get("legacy_private_fields")
        if isinstance(prior, dict) and isinstance(legacy, dict):
            merged["legacy_private_fields"] = {**legacy, **prior}
        else:
            merged["legacy_private_fields"] = legacy
    return merged


def _write_atomic(path: Path, content: bytes, *, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.privacy.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(mode)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        details = path.stat(follow_symlinks=False)
        if path.is_symlink():
            raise ValueError("snapshot_contains_symlink")
        if path.is_dir():
            digest.update(f"d:{relative}:{stat.S_IMODE(details.st_mode):04o}\n".encode())
            continue
        if not path.is_file():
            raise ValueError("snapshot_contains_special_file")
        digest.update(
            f"f:{relative}:{stat.S_IMODE(details.st_mode):04o}:{details.st_size}:".encode()
        )
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _snapshot(source: Path, backup_root: Path) -> tuple[str, int]:
    if backup_root.exists() and any(backup_root.iterdir()):
        raise ValueError("backup_root_not_empty")
    backup_root.mkdir(parents=True, exist_ok=True)
    snapshot = backup_root / "public_property_tours"
    shutil.copytree(source, snapshot, copy_function=shutil.copy2)
    return _tree_digest(snapshot), sum(
        1 for path in snapshot.rglob("*") if path.is_file()
    )


def audit_or_repair(
    root: Path,
    *,
    apply: bool = False,
    backup_root: Path | None = None,
) -> dict[str, object]:
    root = root.expanduser().resolve()
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("public_tour_root_invalid")

    snapshot_sha256 = ""
    snapshot_files = 0
    if apply:
        if backup_root is None:
            raise ValueError("backup_root_required")
        snapshot_sha256, snapshot_files = _snapshot(
            root,
            backup_root.expanduser().resolve(),
        )

    counts: dict[str, int] = {
        "bundles": 0,
        "invalid_manifests": 0,
        "private_key_manifests": 0,
        "noncanonical_manifests": 0,
        "private_mode_violations": 0,
        "repaired_manifests": 0,
        "repaired_private_receipts": 0,
    }
    changed_digests: list[str] = []

    for manifest_path in sorted(root.glob("*/tour.json")):
        slug = manifest_path.parent.name
        if not SAFE_SLUG.fullmatch(slug):
            continue
        counts["bundles"] += 1
        try:
            payload = _read_object(manifest_path)
            declared_slug = str(payload.get("slug") or "").strip()
            slug_mismatch = declared_slug != slug
            if slug_mismatch and not apply:
                raise ValueError("manifest_slug_mismatch")
            public_payload = dict(payload)
            if slug_mismatch:
                public_payload["slug"] = slug
            canonical = _canonical_public_payload(
                public_payload,
                bundle_dir=manifest_path.parent,
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            counts["invalid_manifests"] += 1
            continue

        private_path = manifest_path.with_name("tour.private.json")
        existing_private: dict[str, object] = {}
        if private_path.exists():
            try:
                existing_private = _read_object(private_path)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                counts["invalid_manifests"] += 1
                continue
            if stat.S_IMODE(private_path.stat().st_mode) != 0o600:
                counts["private_mode_violations"] += 1

        if slug_mismatch and declared_slug:
            existing_private.setdefault("legacy_declared_slug", declared_slug)
        has_private = _contains_private_key(payload)
        noncanonical = canonical != payload
        counts["private_key_manifests"] += int(has_private)
        counts["noncanonical_manifests"] += int(noncanonical)
        if not apply:
            continue

        merged_private = _merge_private_receipt(payload, existing_private)
        if noncanonical:
            public_bytes = _canonical_json_bytes(canonical)
            _write_atomic(manifest_path, public_bytes, mode=0o644)
            counts["repaired_manifests"] += 1
            changed_digests.append(_sha256_bytes(public_bytes))
        if merged_private or private_path.exists():
            private_bytes = _canonical_json_bytes(merged_private)
            if (
                not private_path.exists()
                or _read_object(private_path) != merged_private
                or stat.S_IMODE(private_path.stat().st_mode) != 0o600
            ):
                _write_atomic(private_path, private_bytes, mode=0o600)
                counts["repaired_private_receipts"] += 1
                changed_digests.append(_sha256_bytes(private_bytes))

    post_failures = (
        counts["invalid_manifests"]
        + (0 if apply else counts["private_key_manifests"])
        + (0 if apply else counts["noncanonical_manifests"])
        + (0 if apply else counts["private_mode_violations"])
    )
    receipt: dict[str, object] = {
        "schema": SCHEMA,
        "status": "pass" if post_failures == 0 else "fail",
        "mode": "apply" if apply else "audit",
        "generated_at": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "root": str(root),
        "counts": counts,
        "snapshot": {
            "required": apply,
            "root": str(backup_root.resolve()) if backup_root else "",
            "files": snapshot_files,
            "tree_sha256": snapshot_sha256,
        },
        "changed_content_set_sha256": (
            _sha256_bytes("\n".join(sorted(changed_digests)).encode())
            if changed_digests
            else ""
        ),
        "secret_values_recorded": False,
    }
    if apply:
        verification = audit_or_repair(root)
        receipt["post_repair"] = {
            "status": verification["status"],
            "counts": verification["counts"],
        }
        receipt["status"] = verification["status"]
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/data/public_property_tours")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-root", default="")
    parser.add_argument("--receipt", default="")
    args = parser.parse_args()

    receipt = audit_or_repair(
        Path(args.root),
        apply=args.apply,
        backup_root=Path(args.backup_root) if args.backup_root else None,
    )
    encoded = _canonical_json_bytes(receipt)
    if args.receipt:
        receipt_path = Path(args.receipt).expanduser()
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(receipt_path, encoded, mode=0o600)
    print(encoded.decode("utf-8"), end="")
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
