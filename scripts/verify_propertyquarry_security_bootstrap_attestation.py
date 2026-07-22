#!/usr/bin/env python3
"""Verify that a governed bootstrap actually supplied the release security runner.

The workflow that retrieves the artifact is responsible only for authenticating
GitHub API metadata and downloading the selected artifact.  This module keeps
all receipt validation pure and locally testable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Mapping


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RUNNER_LABEL_RE = re.compile(r"^pqsec-[0-9a-f]{32}$")
_WEB_IMAGE_RE = re.compile(
    r"^ghcr\.io/archonmegalon/"
    r"propertyquarry-standalone-web-runtime@sha256:[0-9a-f]{64}$"
)
_RENDER_IMAGE_RE = re.compile(
    r"^ghcr\.io/archonmegalon/"
    r"propertyquarry-standalone-render-runtime@sha256:[0-9a-f]{64}$"
)
_POSITIVE_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_ARTIFACT_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)

DEFAULT_MAX_JSON_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_RECEIPT_JSON_BYTES = 1024 * 1024
DEFAULT_MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_ARCHIVE_ENTRIES = 128
DEFAULT_MAX_MEMBER_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
MAX_MEMBER_NAME_BYTES = 512
MAX_ATTESTATION_SCALAR_LENGTH = 4096


class AttestationError(ValueError):
    """Raised when bootstrap evidence is incomplete or not exactly bound."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise AttestationError(f"{label} must be a JSON object")
    return value


def _text(payload: Mapping[str, Any], key: str, label: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise AttestationError(f"{label}.{key} must be a non-empty string")
    if (
        len(value) > MAX_ATTESTATION_SCALAR_LENGTH
        or "\n" in value
        or "\r" in value
        or "\x00" in value
    ):
        raise AttestationError(f"{label}.{key} is not a bounded safe scalar")
    return value


def _expect_text(
    payload: Mapping[str, Any], key: str, expected: str, label: str
) -> None:
    actual = _text(payload, key, label)
    if actual != expected:
        raise AttestationError(
            f"{label}.{key} mismatch: expected {expected!r}, got {actual!r}"
        )


def _parse_rfc3339(value: str, label: str) -> datetime:
    if len(value) != 20 or not _UTC_RFC3339_RE.fullmatch(value):
        raise AttestationError(f"{label} must be canonical UTC RFC3339")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise AttestationError(f"{label} must be canonical UTC RFC3339") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise AttestationError(f"{label} must be canonical UTC RFC3339")
    return parsed


def _reject_json_constant(value: str) -> None:
    raise AttestationError(f"non-finite JSON number is forbidden: {value}")


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise AttestationError(f"duplicate JSON key is forbidden: {key}")
        payload[key] = value
    return payload


def _decode_json(raw: bytes, label: str) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except AttestationError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttestationError(f"{label} is not valid UTF-8 JSON") from exc


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise AttestationError(f"{label} must be a regular non-symlink file")
    try:
        file_status = path.stat()
    except OSError as exc:
        raise AttestationError(f"{label} cannot be inspected") from exc
    if file_status.st_nlink != 1:
        raise AttestationError(f"{label} must not be hardlinked")
    size = file_status.st_size
    if size <= 0 or size > DEFAULT_MAX_RECEIPT_JSON_BYTES:
        raise AttestationError(f"{label} byte length is outside policy")
    try:
        payload = _decode_json(path.read_bytes(), label)
    except OSError as exc:
        raise AttestationError(f"{label} is not readable canonical JSON") from exc
    return _mapping(payload, label)


def _artifact_file(root: Path, name: str) -> Path:
    if Path(name).name != name:
        raise AttestationError(f"receipt name is not a basename: {name!r}")
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise AttestationError(f"artifact receipt is missing or unsafe: {name}")
    try:
        if path.resolve(strict=True).parent != root.resolve(strict=True):
            raise AttestationError(f"artifact receipt escapes its root: {name}")
    except OSError as exc:
        raise AttestationError(f"artifact receipt cannot be resolved: {name}") from exc
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_stream_bounded(stream: BinaryIO, maximum_bytes: int, label: str) -> bytes:
    if maximum_bytes <= 0:
        raise AttestationError(f"{label} maximum length must be positive")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(1024 * 1024, maximum_bytes - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > maximum_bytes:
            raise AttestationError(f"{label} exceeds {maximum_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def capture_json_stream(
    stream: BinaryIO, *, destination: Path, maximum_bytes: int
) -> None:
    """Capture bounded API JSON and rewrite it into one canonical representation."""

    raw = _read_stream_bounded(stream, maximum_bytes, "API JSON")
    payload = _decode_json(raw, "API response")
    if not isinstance(payload, (dict, list)):
        raise AttestationError("API JSON must be an object or array")
    _write_json_atomic(destination, payload, maximum_bytes=maximum_bytes)


def capture_authenticated_archive(
    stream: BinaryIO,
    *,
    destination: Path,
    expected_digest: str,
    maximum_bytes: int,
) -> dict[str, object]:
    """Write a bounded archive only when its bytes match GitHub metadata."""

    if not _ARTIFACT_DIGEST_RE.fullmatch(expected_digest):
        raise AttestationError("authenticated artifact digest is malformed")
    if maximum_bytes <= 0:
        raise AttestationError("archive maximum length must be positive")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists() or destination.is_symlink():
        raise AttestationError("archive destination must not already exist")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb") as handle:
            while True:
                chunk = stream.read(min(1024 * 1024, maximum_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise AttestationError(
                        f"artifact archive exceeds {maximum_bytes} bytes"
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        actual_digest = f"sha256:{digest.hexdigest()}"
        if actual_digest != expected_digest:
            raise AttestationError(
                "artifact archive digest mismatch: authenticated metadata does not "
                "match downloaded bytes"
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "bytes": total,
        "digest": expected_digest,
        "path": str(destination),
    }


def _canonical_member_parts(name: str) -> tuple[str, ...]:
    if not name or len(name.encode("utf-8")) > MAX_MEMBER_NAME_BYTES:
        raise AttestationError("archive member name is empty or oversized")
    if "\x00" in name or "\\" in name:
        raise AttestationError("archive member name contains an unsafe character")
    if unicodedata.normalize("NFC", name) != name:
        raise AttestationError("archive member name is not canonical Unicode")
    candidate = PurePosixPath(name.rstrip("/"))
    if candidate.is_absolute() or not candidate.parts:
        raise AttestationError("archive member path is absolute or empty")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise AttestationError("archive member path traverses or is non-canonical")
    if candidate.as_posix() != name.rstrip("/") or name.endswith("//"):
        raise AttestationError("archive member path is not canonically encoded")
    if ":" in candidate.parts[0]:
        raise AttestationError("archive member path uses a drive prefix")
    return candidate.parts


def _zip_member_kind(info: zipfile.ZipInfo) -> str:
    mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(mode)
    if file_type == stat.S_IFLNK:
        raise AttestationError(f"archive symlink is forbidden: {info.filename}")
    if file_type not in {stat.S_IFREG, stat.S_IFDIR}:
        if file_type == 0:
            raise AttestationError(
                f"archive member mode metadata is unknown: {info.filename}"
            )
        raise AttestationError(
            f"archive special file or link is forbidden: {info.filename}"
        )
    if info.create_system != 3:
        raise AttestationError(
            f"archive member mode origin is unknown: {info.filename}"
        )
    metadata_is_directory = file_type == stat.S_IFDIR
    name_is_directory = info.filename.endswith("/")
    if metadata_is_directory != name_is_directory:
        raise AttestationError(
            f"archive member directory metadata and name disagree: {info.filename}"
        )
    return "directory" if metadata_is_directory else "file"


def safe_extract_artifact_archive(
    archive_path: Path,
    *,
    destination: Path,
    maximum_archive_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    maximum_entries: int = DEFAULT_MAX_ARCHIVE_ENTRIES,
    maximum_member_bytes: int = DEFAULT_MAX_MEMBER_BYTES,
    maximum_uncompressed_bytes: int = DEFAULT_MAX_UNCOMPRESSED_BYTES,
) -> dict[str, object]:
    """Extract a ZIP artifact without allowing filesystem or resource escapes."""

    if any(
        limit <= 0
        for limit in (
            maximum_archive_bytes,
            maximum_entries,
            maximum_member_bytes,
            maximum_uncompressed_bytes,
        )
    ):
        raise AttestationError("artifact extraction limits must be positive")
    if archive_path.is_symlink() or not archive_path.is_file():
        raise AttestationError("artifact archive must be a regular non-symlink file")
    archive_status = archive_path.stat()
    if archive_status.st_nlink != 1:
        raise AttestationError("artifact archive must not be hardlinked")
    archive_size = archive_status.st_size
    if archive_size <= 0 or archive_size > maximum_archive_bytes:
        raise AttestationError("artifact archive byte length is outside policy")
    if destination.exists() or destination.is_symlink():
        raise AttestationError("artifact extraction destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    os.chmod(temporary, 0o700)
    try:
        try:
            archive = zipfile.ZipFile(archive_path, mode="r")
        except (OSError, zipfile.BadZipFile) as exc:
            raise AttestationError("artifact archive is not a valid ZIP") from exc
        with archive:
            infos = archive.infolist()
            if not infos or len(infos) > maximum_entries:
                raise AttestationError("artifact archive entry count is outside policy")
            if len(archive.comment) > 1024:
                raise AttestationError("artifact archive comment is oversized")
            entries: dict[tuple[str, ...], tuple[str, zipfile.ZipInfo]] = {}
            total_declared = 0
            for info in infos:
                parts = _canonical_member_parts(info.filename)
                if parts in entries:
                    raise AttestationError(
                        f"duplicate archive member path is forbidden: {info.filename}"
                    )
                kind = _zip_member_kind(info)
                if info.flag_bits & 0x1:
                    raise AttestationError(
                        f"encrypted archive member is forbidden: {info.filename}"
                    )
                if info.compress_type not in {
                    zipfile.ZIP_STORED,
                    zipfile.ZIP_DEFLATED,
                }:
                    raise AttestationError(
                        f"archive compression method is forbidden: {info.filename}"
                    )
                if info.file_size < 0 or info.file_size > maximum_member_bytes:
                    raise AttestationError(
                        f"archive member exceeds its byte limit: {info.filename}"
                    )
                if kind == "directory" and info.file_size != 0:
                    raise AttestationError(
                        f"archive directory has a payload: {info.filename}"
                    )
                total_declared += info.file_size
                if total_declared > maximum_uncompressed_bytes:
                    raise AttestationError(
                        "artifact archive aggregate uncompressed size exceeds policy"
                    )
                entries[parts] = (kind, info)

            for parts, (kind, _info) in sorted(
                entries.items(), key=lambda item: len(item[0])
            ):
                for depth in range(1, len(parts)):
                    parent = entries.get(parts[:depth])
                    if parent is not None and parent[0] != "directory":
                        raise AttestationError(
                            "archive file is used as another member's parent"
                        )
                if kind == "directory":
                    target_directory = temporary.joinpath(*parts)
                    target_directory.mkdir(parents=True, exist_ok=False, mode=0o700)

            extracted_bytes = 0
            for parts, (kind, info) in entries.items():
                if kind == "directory":
                    continue
                target = temporary.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                file_descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                member_bytes = 0
                try:
                    with os.fdopen(file_descriptor, "wb") as output, archive.open(
                        info, mode="r"
                    ) as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            member_bytes += len(chunk)
                            extracted_bytes += len(chunk)
                            if member_bytes > maximum_member_bytes:
                                raise AttestationError(
                                    f"archive member expanded past policy: {info.filename}"
                                )
                            if extracted_bytes > maximum_uncompressed_bytes:
                                raise AttestationError(
                                    "archive expanded past aggregate byte policy"
                                )
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                    raise AttestationError(
                        f"archive member failed integrity extraction: {info.filename}"
                    ) from exc
                if member_bytes != info.file_size:
                    raise AttestationError(
                        f"archive member size changed during extraction: {info.filename}"
                    )
                os.chmod(target, 0o600)
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            for child in sorted(temporary.rglob("*"), reverse=True):
                if child.is_symlink() or child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            temporary.rmdir()
    return {
        "archive_bytes": archive_size,
        "entries": len(infos),
        "uncompressed_bytes": total_declared,
        "destination": str(destination),
    }


def _expect_target(
    payload: Mapping[str, Any],
    *,
    label: str,
    run_id: str,
    run_attempt: str,
    job_id: str,
    head_sha: str,
) -> None:
    _expect_text(payload, "run_id", run_id, label)
    _expect_text(payload, "run_attempt", run_attempt, label)
    _expect_text(payload, "job_id", job_id, label)
    _expect_text(payload, "head_sha", head_sha, label)


def _expect_images(
    payload: Mapping[str, Any], *, web_image: str, render_image: str, label: str
) -> None:
    _expect_text(payload, "web", web_image, label)
    _expect_text(payload, "render", render_image, label)


def verify_security_bootstrap_attestation(
    *,
    artifact_root: Path,
    artifact_metadata_path: Path,
    expected_repository: str,
    expected_workflow_ref: str,
    expected_bootstrap_workflow_path: str,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_job_id: str,
    expected_head_sha: str,
    expected_runner_label: str,
    expected_token_expires_at: str,
    expected_web_image: str,
    expected_render_image: str,
    verified_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate a bootstrap bundle and return a same-run attestation payload."""

    if artifact_root.is_symlink() or not artifact_root.is_dir():
        raise AttestationError("artifact root must be a directory, not a symlink")
    if not _POSITIVE_ID_RE.fullmatch(expected_run_id):
        raise AttestationError("expected run id is malformed")
    if not _POSITIVE_ID_RE.fullmatch(expected_run_attempt):
        raise AttestationError("expected run attempt is malformed")
    if not _POSITIVE_ID_RE.fullmatch(expected_job_id):
        raise AttestationError("expected job id is malformed")
    if not _HEAD_SHA_RE.fullmatch(expected_head_sha):
        raise AttestationError("expected head SHA is malformed")
    if not _RUNNER_LABEL_RE.fullmatch(expected_runner_label):
        raise AttestationError("expected runner label is malformed")
    if not _WEB_IMAGE_RE.fullmatch(expected_web_image):
        raise AttestationError("expected web image is not digest pinned")
    if not _RENDER_IMAGE_RE.fullmatch(expected_render_image):
        raise AttestationError("expected render image is not digest pinned")
    if expected_web_image == expected_render_image:
        raise AttestationError("web and render image identities collide")
    _parse_rfc3339(expected_token_expires_at, "expected token expiration")

    expected_artifact_name = (
        "propertyquarry-security-runner-bootstrap-target-"
        f"{expected_run_id}-{expected_run_attempt}-{expected_job_id}"
    )
    metadata = _load_json(artifact_metadata_path, "artifact metadata")
    _expect_text(
        metadata,
        "schema",
        "propertyquarry.security_bootstrap_artifact_metadata.v1",
        "artifact metadata",
    )
    _parse_rfc3339(
        _text(metadata, "observed_at", "artifact metadata"),
        "artifact metadata observation time",
    )
    if metadata.get("selection_count") != 1:
        raise AttestationError("artifact metadata must prove exactly one selection")
    artifact = _mapping(metadata.get("artifact"), "artifact metadata.artifact")
    bootstrap_run = _mapping(
        metadata.get("bootstrap_workflow_run"),
        "artifact metadata.bootstrap_workflow_run",
    )
    artifact_id = _text(artifact, "id", "artifact metadata.artifact")
    if not _POSITIVE_ID_RE.fullmatch(artifact_id):
        raise AttestationError("artifact id is malformed")
    _expect_text(
        artifact,
        "name",
        expected_artifact_name,
        "artifact metadata.artifact",
    )
    artifact_digest = _text(artifact, "digest", "artifact metadata.artifact")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_digest):
        raise AttestationError("GitHub artifact digest is missing or malformed")
    if artifact.get("expired") is not False:
        raise AttestationError("selected bootstrap artifact is expired")

    bootstrap_run_id = _text(
        bootstrap_run, "id", "artifact metadata.bootstrap_workflow_run"
    )
    bootstrap_run_attempt = _text(
        bootstrap_run, "run_attempt", "artifact metadata.bootstrap_workflow_run"
    )
    if not _POSITIVE_ID_RE.fullmatch(bootstrap_run_id):
        raise AttestationError("bootstrap workflow run id is malformed")
    if not _POSITIVE_ID_RE.fullmatch(bootstrap_run_attempt):
        raise AttestationError("bootstrap workflow run attempt is malformed")
    _expect_text(
        artifact,
        "workflow_run_id",
        bootstrap_run_id,
        "artifact metadata.artifact",
    )
    for key, expected in (
        ("repository", expected_repository),
        ("event", "workflow_dispatch"),
        ("path", expected_bootstrap_workflow_path),
        ("head_branch", "main"),
        ("head_sha", expected_head_sha),
        ("status", "completed"),
        ("conclusion", "success"),
    ):
        _expect_text(
            bootstrap_run,
            key,
            expected,
            "artifact metadata.bootstrap_workflow_run",
        )

    expected_runner_name = f"pq-security-{bootstrap_run_id}-{expected_run_id}"
    preflight_name = f"preflight-{expected_run_id}-{expected_run_attempt}.json"
    required_receipts = {
        "bootstrap-receipt.json",
        "consumption.json",
        "post-job-integrity.json",
        preflight_name,
    }
    manifest_path = _artifact_file(artifact_root, "receipt-manifest.json")
    manifest = _load_json(manifest_path, "receipt manifest")
    _expect_text(
        manifest,
        "schema",
        "propertyquarry.security_runner_receipt_manifest.v1",
        "receipt manifest",
    )
    _expect_text(manifest, "status", "pass", "receipt manifest")
    _expect_text(
        manifest, "repository", expected_repository, "receipt manifest"
    )
    _expect_text(
        manifest, "workflow_ref", expected_workflow_ref, "receipt manifest"
    )
    _parse_rfc3339(_text(manifest, "recorded_at", "receipt manifest"), "manifest time")
    _expect_text(
        manifest,
        "registration_token_expires_at",
        expected_token_expires_at,
        "receipt manifest",
    )
    _expect_target(
        _mapping(manifest.get("target"), "receipt manifest.target"),
        label="receipt manifest.target",
        run_id=expected_run_id,
        run_attempt=expected_run_attempt,
        job_id=expected_job_id,
        head_sha=expected_head_sha,
    )
    manifest_bootstrap = _mapping(
        manifest.get("bootstrap"), "receipt manifest.bootstrap"
    )
    _expect_text(
        manifest_bootstrap,
        "run_id",
        bootstrap_run_id,
        "receipt manifest.bootstrap",
    )
    _expect_text(
        manifest_bootstrap,
        "run_attempt",
        bootstrap_run_attempt,
        "receipt manifest.bootstrap",
    )
    _expect_text(
        manifest_bootstrap,
        "head_sha",
        expected_head_sha,
        "receipt manifest.bootstrap",
    )
    manifest_runner = _mapping(manifest.get("runner"), "receipt manifest.runner")
    _expect_text(
        manifest_runner, "name", expected_runner_name, "receipt manifest.runner"
    )
    _expect_text(
        manifest_runner,
        "label",
        expected_runner_label,
        "receipt manifest.runner",
    )
    _expect_images(
        _mapping(manifest.get("images"), "receipt manifest.images"),
        web_image=expected_web_image,
        render_image=expected_render_image,
        label="receipt manifest.images",
    )

    file_digests = _mapping(manifest.get("files"), "receipt manifest.files")
    if set(file_digests) != required_receipts:
        raise AttestationError("receipt manifest file allowlist is not exact")
    receipt_paths: dict[str, Path] = {}
    for name in sorted(required_receipts):
        path = _artifact_file(artifact_root, name)
        receipt_paths[name] = path
        expected_digest = file_digests.get(name)
        if not isinstance(expected_digest, str) or not _SHA256_RE.fullmatch(
            expected_digest
        ):
            raise AttestationError(f"receipt manifest digest is malformed: {name}")
        if _sha256(path) != expected_digest:
            raise AttestationError(f"receipt digest mismatch: {name}")

    consumption = _load_json(receipt_paths["consumption.json"], "consumption receipt")
    _expect_text(
        consumption,
        "schema",
        "propertyquarry.security_runner_consumption.v1",
        "consumption receipt",
    )
    _expect_text(consumption, "status", "pass", "consumption receipt")
    _expect_text(
        consumption, "repository", expected_repository, "consumption receipt"
    )
    _expect_text(
        consumption, "workflow_ref", expected_workflow_ref, "consumption receipt"
    )
    _expect_text(
        consumption,
        "registration_token_expires_at",
        expected_token_expires_at,
        "consumption receipt",
    )
    _parse_rfc3339(
        _text(consumption, "verified_at", "consumption receipt"),
        "consumption verification time",
    )
    _expect_target(
        _mapping(consumption.get("target"), "consumption receipt.target"),
        label="consumption receipt.target",
        run_id=expected_run_id,
        run_attempt=expected_run_attempt,
        job_id=expected_job_id,
        head_sha=expected_head_sha,
    )
    consumption_bootstrap = _mapping(
        consumption.get("bootstrap"), "consumption receipt.bootstrap"
    )
    _expect_text(
        consumption_bootstrap,
        "run_id",
        bootstrap_run_id,
        "consumption receipt.bootstrap",
    )
    _expect_text(
        consumption_bootstrap,
        "run_attempt",
        bootstrap_run_attempt,
        "consumption receipt.bootstrap",
    )
    consumption_runner = _mapping(
        consumption.get("runner"), "consumption receipt.runner"
    )
    _expect_text(
        consumption_runner,
        "name",
        expected_runner_name,
        "consumption receipt.runner",
    )
    _expect_text(
        consumption_runner,
        "label",
        expected_runner_label,
        "consumption receipt.runner",
    )
    _expect_images(
        _mapping(consumption.get("images"), "consumption receipt.images"),
        web_image=expected_web_image,
        render_image=expected_render_image,
        label="consumption receipt.images",
    )

    bootstrap = _load_json(
        receipt_paths["bootstrap-receipt.json"], "bootstrap receipt"
    )
    _expect_text(
        bootstrap,
        "schema",
        "propertyquarry.security_runner_bootstrap.v1",
        "bootstrap receipt",
    )
    _expect_text(bootstrap, "status", "listener_exited", "bootstrap receipt")
    _expect_text(bootstrap, "repository", expected_repository, "bootstrap receipt")
    _parse_rfc3339(
        _text(bootstrap, "recorded_at", "bootstrap receipt"),
        "bootstrap receipt time",
    )
    _expect_target(
        _mapping(bootstrap.get("target"), "bootstrap receipt.target"),
        label="bootstrap receipt.target",
        run_id=expected_run_id,
        run_attempt=expected_run_attempt,
        job_id=expected_job_id,
        head_sha=expected_head_sha,
    )
    bootstrap_identity = _mapping(
        bootstrap.get("bootstrap"), "bootstrap receipt.bootstrap"
    )
    _expect_text(
        bootstrap_identity,
        "run_id",
        bootstrap_run_id,
        "bootstrap receipt.bootstrap",
    )
    _expect_text(
        bootstrap_identity,
        "run_attempt",
        bootstrap_run_attempt,
        "bootstrap receipt.bootstrap",
    )
    bootstrap_runner = _mapping(
        bootstrap.get("runner"), "bootstrap receipt.runner"
    )
    for key, expected in (
        ("name", expected_runner_name),
        ("label", expected_runner_label),
        ("listener_exit_code", "2"),
        ("registration_token_expires_at", expected_token_expires_at),
    ):
        _expect_text(bootstrap_runner, key, expected, "bootstrap receipt.runner")
    if not _POSITIVE_ID_RE.fullmatch(
        _text(bootstrap_runner, "agent_id", "bootstrap receipt.runner")
    ):
        raise AttestationError("bootstrap runner agent id is malformed")
    _expect_images(
        _mapping(bootstrap.get("images"), "bootstrap receipt.images"),
        web_image=expected_web_image,
        render_image=expected_render_image,
        label="bootstrap receipt.images",
    )

    post_job = _load_json(
        receipt_paths["post-job-integrity.json"], "post-job receipt"
    )
    _expect_text(
        post_job,
        "schema",
        "propertyquarry.security_runner_post_job_integrity.v1",
        "post-job receipt",
    )
    _expect_text(post_job, "status", "pass", "post-job receipt")
    _parse_rfc3339(
        _text(post_job, "checked_at", "post-job receipt"),
        "post-job receipt time",
    )
    for key, expected in (
        ("run_id", expected_run_id),
        ("run_attempt", expected_run_attempt),
        ("job_id", expected_job_id),
        ("runner_name", expected_runner_name),
        ("runner_label", expected_runner_label),
    ):
        _expect_text(post_job, key, expected, "post-job receipt")

    preflight = _load_json(receipt_paths[preflight_name], "preflight receipt")
    _expect_text(
        preflight,
        "schema",
        "propertyquarry.security_runner_preflight.v1",
        "preflight receipt",
    )
    _expect_text(preflight, "status", "pass", "preflight receipt")
    _parse_rfc3339(
        _text(preflight, "checked_at", "preflight receipt"),
        "preflight receipt time",
    )
    for key, expected in (
        ("repository", expected_repository),
        ("run_id", expected_run_id),
        ("run_attempt", expected_run_attempt),
        ("head_sha", expected_head_sha),
        ("runner_name", expected_runner_name),
    ):
        _expect_text(preflight, key, expected, "preflight receipt")

    checked_at = (verified_at or datetime.now(timezone.utc)).astimezone(
        timezone.utc
    ).replace(microsecond=0)
    return {
        "schema": "propertyquarry.security_bootstrap_attestation.v1",
        "status": "pass",
        "repository": expected_repository,
        "workflow_ref": expected_workflow_ref,
        "target": {
            "run_id": expected_run_id,
            "run_attempt": expected_run_attempt,
            "job_id": expected_job_id,
            "head_sha": expected_head_sha,
        },
        "bootstrap": {
            "run_id": bootstrap_run_id,
            "run_attempt": bootstrap_run_attempt,
            "workflow_path": expected_bootstrap_workflow_path,
        },
        "runner": {
            "name": expected_runner_name,
            "label": expected_runner_label,
        },
        "images": {
            "web": expected_web_image,
            "render": expected_render_image,
        },
        "registration_token_expires_at": expected_token_expires_at,
        "artifact": {
            "id": artifact_id,
            "name": expected_artifact_name,
            "digest": artifact_digest,
            "workflow_run_id": bootstrap_run_id,
        },
        "receipt_manifest_sha256": _sha256(manifest_path),
        "verified_at": checked_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _write_json_atomic(
    path: Path,
    payload: object,
    *,
    maximum_bytes: int = DEFAULT_MAX_JSON_BYTES,
) -> None:
    if maximum_bytes <= 0:
        raise AttestationError("JSON output maximum length must be positive")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise AttestationError("attestation output must not be a symlink")
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size > maximum_bytes:
            raise AttestationError(f"canonical JSON exceeds {maximum_bytes} bytes")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _positive_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("limit must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    verify = commands.add_parser("verify", help="verify extracted receipt semantics")
    verify.add_argument("--artifact-root", required=True, type=Path)
    verify.add_argument("--artifact-metadata", required=True, type=Path)
    verify.add_argument("--expected-repository", required=True)
    verify.add_argument("--expected-workflow-ref", required=True)
    verify.add_argument("--expected-bootstrap-workflow-path", required=True)
    verify.add_argument("--expected-run-id", required=True)
    verify.add_argument("--expected-run-attempt", required=True)
    verify.add_argument("--expected-job-id", required=True)
    verify.add_argument("--expected-head-sha", required=True)
    verify.add_argument("--expected-runner-label", required=True)
    verify.add_argument("--expected-token-expires-at", required=True)
    verify.add_argument("--expected-web-image", required=True)
    verify.add_argument("--expected-render-image", required=True)
    verify.add_argument("--write", required=True, type=Path)

    capture_json = commands.add_parser(
        "capture-json", help="capture and canonicalize bounded API JSON from stdin"
    )
    capture_json.add_argument("--max-bytes", type=_positive_limit, required=True)
    capture_json.add_argument("--write", required=True, type=Path)

    capture_archive = commands.add_parser(
        "capture-archive",
        help="capture authenticated artifact archive bytes from stdin",
    )
    capture_archive.add_argument("--expected-digest", required=True)
    capture_archive.add_argument("--max-bytes", type=_positive_limit, required=True)
    capture_archive.add_argument("--write", required=True, type=Path)

    extract_archive = commands.add_parser(
        "extract-archive", help="safely extract a previously authenticated ZIP"
    )
    extract_archive.add_argument("--archive", required=True, type=Path)
    extract_archive.add_argument("--destination", required=True, type=Path)
    extract_archive.add_argument(
        "--max-archive-bytes",
        type=_positive_limit,
        default=DEFAULT_MAX_ARCHIVE_BYTES,
    )
    extract_archive.add_argument(
        "--max-entries", type=_positive_limit, default=DEFAULT_MAX_ARCHIVE_ENTRIES
    )
    extract_archive.add_argument(
        "--max-member-bytes", type=_positive_limit, default=DEFAULT_MAX_MEMBER_BYTES
    )
    extract_archive.add_argument(
        "--max-uncompressed-bytes",
        type=_positive_limit,
        default=DEFAULT_MAX_UNCOMPRESSED_BYTES,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "capture-json":
        capture_json_stream(
            sys.stdin.buffer,
            destination=args.write,
            maximum_bytes=args.max_bytes,
        )
        print(f"Captured bounded canonical API JSON: {args.write}")
        return 0
    if args.command == "capture-archive":
        receipt = capture_authenticated_archive(
            sys.stdin.buffer,
            destination=args.write,
            expected_digest=args.expected_digest,
            maximum_bytes=args.max_bytes,
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if args.command == "extract-archive":
        receipt = safe_extract_artifact_archive(
            args.archive,
            destination=args.destination,
            maximum_archive_bytes=args.max_archive_bytes,
            maximum_entries=args.max_entries,
            maximum_member_bytes=args.max_member_bytes,
            maximum_uncompressed_bytes=args.max_uncompressed_bytes,
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if args.command != "verify":
        raise AttestationError("unsupported verifier command")
    payload = verify_security_bootstrap_attestation(
        artifact_root=args.artifact_root,
        artifact_metadata_path=args.artifact_metadata,
        expected_repository=args.expected_repository,
        expected_workflow_ref=args.expected_workflow_ref,
        expected_bootstrap_workflow_path=args.expected_bootstrap_workflow_path,
        expected_run_id=args.expected_run_id,
        expected_run_attempt=args.expected_run_attempt,
        expected_job_id=args.expected_job_id,
        expected_head_sha=args.expected_head_sha,
        expected_runner_label=args.expected_runner_label,
        expected_token_expires_at=args.expected_token_expires_at,
        expected_web_image=args.expected_web_image,
        expected_render_image=args.expected_render_image,
    )
    _write_json_atomic(args.write, payload)
    print(f"PropertyQuarry security bootstrap attestation passed: {args.write}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
