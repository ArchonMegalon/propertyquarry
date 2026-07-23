#!/usr/bin/env python3
"""Materialize, build, and verify the fixed f7 tour-publication package.

This protocol is intentionally distinct from the single-host runtime package.
It grants only short-lived, machine-bound scratch dispatch of the five fixed
tour-v4 operations.  It never grants host installation or runtime deployment.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


TOOLS = Path(__file__).resolve().parent
sys.dont_write_bytecode = True

_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_single_host_package_v2_for_tour", TOOLS / "package.py"
)
if _PACKAGE_SPEC is None or _PACKAGE_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("package-module-unavailable")
package = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_SPEC.name] = package
_PACKAGE_SPEC.loader.exec_module(package)

_MATERIALIZE_SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_single_host_materialize_v2_for_tour",
    TOOLS / "materialize.py",
)
if _MATERIALIZE_SPEC is None or _MATERIALIZE_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("materialize-module-unavailable")
materialize = importlib.util.module_from_spec(_MATERIALIZE_SPEC)
sys.modules[_MATERIALIZE_SPEC.name] = materialize
_MATERIALIZE_SPEC.loader.exec_module(materialize)


PACKAGE_SCHEMA = (
    "propertyquarry.release-control.single-host-tour-publication-package.v4"
)
PACKAGE_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-tour-publication-package-"
    b"manifest-signature.v4\0"
)
MATERIALIZATION_SCHEMA = (
    "propertyquarry.release-control.single-host-tour-publication-materialization.v4"
)
MATERIALIZATION_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-tour-publication-"
    b"materialization-signature.v4\0"
)
PROFILE = "single-host-tour-publication-v4"
ARCHIVE_FORMAT = "ustar-v1"
NON_AUTHORITATIVE_UNTIL = (
    "package-and-self-bound-scratch-dispatch-reverification"
)
MATERIALIZATION_TTL_SECONDS = 3600
ACCEPTED_INSTALLER_MODE = "dispatch-tour-v4"

ARTIFACT_SLUG = (
    "ab-1-8-modern-and-fully-furnited-loft-apartment-top-moderne-und-voll-"
    "mblierte-loft-wohnung-nas-layout-first-d07edad7af3fc379574d"
)
ARTIFACT_BUNDLE_PATH = (
    "/tmp/property-f7-tour-final-v4.HUQw8lU4/" + ARTIFACT_SLUG
)
ARTIFACT_MANIFEST_SHA256 = (
    "sha256:5bc06e8758bc3d9b9e88a82a7608a1238340c19fd581d3ec3565a56d75d1fa06"
)
ARTIFACT_PUBLIC_TREE_SHA256 = (
    "sha256:d69c032b96264d892bbd6e269b884a9f33cc11cf3d0f5a7d96a878a062058548"
)
PUBLICATION_TARGET_ROOT = (
    "/var/lib/docker/volumes/property_propertyquarry_public_tours/_data"
)
ALLOWED_OPERATIONS = [
    "tour-v4-authority-info",
    "tour-inspect-v4",
    "tour-publish-v4",
    "tour-recover-v4",
    "tour-rollback-v4",
]
MACHINE_ID_PATH = Path("/etc/machine-id")

MANIFEST_NAME = "manifest.tour-v4.json"
MANIFEST_SIGNATURE_NAME = "manifest.tour-v4.sig"
MATERIALIZATION_NAME = "tour-publication-materialization.v4.json"
MATERIALIZATION_SIGNATURE_NAME = "tour-publication-materialization.v4.sig"
MATERIALIZATION_FILES = frozenset(
    {MATERIALIZATION_NAME, MATERIALIZATION_SIGNATURE_NAME}
)

CONTROLLER_PATH = "material/propertyquarry-release-single-host-v2"
BUILD_RECEIPT_PATH = "material/native-build-receipt.v2.json"
PACKAGE_ANCHOR_PATH = "material/package-authority-v2.pem"
AUTHORITY_BOOTSTRAP_PATH = "material/authority-bootstrap.v2.json"
AUTHORITY_BOOTSTRAP_SIGNATURE_PATH = "material/authority-bootstrap.v2.sig"
RECEIPT_KEY_PATH = "material/receipt-authority-v2.key"
RECEIPT_ANCHOR_PATH = "material/receipt-authority-v2.pem"
MATERIALIZATION_PATH = "material/" + MATERIALIZATION_NAME
MATERIALIZATION_SIGNATURE_PATH = "material/" + MATERIALIZATION_SIGNATURE_NAME

FILE_LAYOUT: dict[str, tuple[int, str]] = {
    AUTHORITY_BOOTSTRAP_PATH: (0o444, "authority-bootstrap"),
    AUTHORITY_BOOTSTRAP_SIGNATURE_PATH: (
        0o444,
        "authority-bootstrap-signature",
    ),
    BUILD_RECEIPT_PATH: (0o444, "native-build-receipt"),
    PACKAGE_ANCHOR_PATH: (0o444, "package-authority-anchor"),
    CONTROLLER_PATH: (0o555, "tour-publication-controller"),
    RECEIPT_KEY_PATH: (0o400, "receipt-signing-private-key"),
    RECEIPT_ANCHOR_PATH: (0o444, "receipt-verification-anchor"),
    MATERIALIZATION_PATH: (0o444, "tour-publication-materialization"),
    MATERIALIZATION_SIGNATURE_PATH: (
        0o444,
        "tour-publication-materialization-signature",
    ),
}
EXACT_MEMBER_NAMES = frozenset(
    {MANIFEST_NAME, MANIFEST_SIGNATURE_NAME, *FILE_LAYOUT}
)
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
MACHINE_ID_PATTERN = re.compile(rb"^[0-9a-f]{32}$")

MATERIALIZATION_KEYS = frozenset(
    {
        "accepted_installer_mode",
        "allowed_operations",
        "artifact_bundle_path",
        "artifact_manifest_sha256",
        "artifact_public_tree_sha256",
        "artifact_slug",
        "authoritative",
        "authority_bootstrap_sha256",
        "host_install_permitted",
        "host_machine_id_digest",
        "materialized_at_epoch",
        "native_build_receipt_sha256",
        "network_required",
        "package_authority_key_id",
        "performs_release_effects",
        "persistent_credential_installation_permitted",
        "production_ready",
        "publication_dispatch_authorized",
        "publication_target_root",
        "receipt_authority_key_id",
        "receipt_authority_public_sha256",
        "root_helper_authorization_required",
        "runtime_deployment_permitted",
        "schema",
        "source_manifest_digest",
        "valid_until_epoch",
        "version",
    }
)
MANIFEST_KEYS = frozenset(
    {
        "accepted_installer_mode",
        "archive_format",
        "authority_bootstrap_sha256",
        "files",
        "host_install_permitted",
        "materialization_sha256",
        "native_build_receipt_sha256",
        "network_required",
        "non_authoritative_until",
        "package_authority_key_id",
        "package_signing_private_key_included",
        "performs_runtime_deployment",
        "profile",
        "receipt_authority_key_id",
        "receipt_signing_private_key_included",
        "root_helper_verification_required",
        "runtime_deployment_permitted",
        "schema",
        "source_manifest_digest",
        "version",
    }
)


@dataclass(frozen=True)
class AuthorityMaterial:
    package_anchor: bytes
    package_private: Ed25519PrivateKey
    package_public: Ed25519PublicKey
    package_key_id: str
    receipt_private_raw: bytes
    receipt_public_raw: bytes
    receipt_key_id: str
    authority_bootstrap_raw: bytes
    authority_bootstrap_signature: bytes


@dataclass(frozen=True)
class VerifiedTourPackage:
    archive_sha256: str
    manifest_sha256: str
    manifest: dict[str, Any]
    materialization: dict[str, Any]
    members: dict[str, bytes]
    modes: dict[str, int]


def _public_raw(public: Ed25519PublicKey) -> bytes:
    return public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _load_authority_material() -> AuthorityMaterial:
    root = os.fspath(materialize.PRODUCTION_RECEIPT_AUTHORITY_ROOT)
    (
        package_anchor,
        package_private,
        package_public,
        receipt_private,
        receipt_public,
        package_key_id,
        receipt_key_id,
    ) = materialize._load_authority(root)  # noqa: SLF001
    files = materialize._read_exact_private_directory(  # noqa: SLF001
        root, materialize.AUTHORITY_FILES
    )
    anchored_public, canonical_anchor, anchored_key_id = package.load_public_key(
        package_anchor, "tour-package-authority-anchor"
    )
    receipt_public_loaded, receipt_public_raw, loaded_receipt_id = (
        package.load_public_key(
            files["receipt-authority-v2.pem"],
            "tour-receipt-authority-anchor",
        )
    )
    receipt_private_loaded, receipt_private_raw = package.load_private_key(
        files["receipt-authority-v2.key"], "tour-receipt-authority-key"
    )
    if (
        canonical_anchor != package_anchor
        or anchored_key_id != package_key_id
        or loaded_receipt_id != receipt_key_id
        or _public_raw(anchored_public) != _public_raw(package_public)
        or _public_raw(package_private.public_key()) != _public_raw(package_public)
        or _public_raw(receipt_private.public_key()) != _public_raw(receipt_public)
        or _public_raw(receipt_private_loaded.public_key())
        != _public_raw(receipt_public_loaded)
        or _public_raw(receipt_public_loaded) != _public_raw(receipt_public)
        or package_key_id == receipt_key_id
    ):
        package.fail("tour-authority-key-binding-invalid")
    bootstrap_raw = files["authority-bootstrap.v2.json"]
    bootstrap_signature = files["authority-bootstrap.v2.sig"]
    if len(bootstrap_signature) != 64:
        package.fail("tour-authority-bootstrap-signature-size-invalid")
    try:
        package_public.verify(
            bootstrap_signature,
            package.framed(materialize.AUTHORITY_SIGNATURE_DOMAIN, bootstrap_raw),
        )
    except InvalidSignature:
        package.fail("tour-authority-bootstrap-signature-invalid")
    bootstrap = package.parse_strict_json(
        bootstrap_raw, "tour-authority-bootstrap"
    )
    expected_bootstrap_keys = {
        "created_at_epoch",
        "package_authority_key_id",
        "package_authority_private_sha256",
        "package_authority_public_sha256",
        "package_authority_source",
        "receipt_authority_key_id",
        "receipt_authority_public_sha256",
        "schema",
        "version",
    }
    if (
        set(bootstrap) != expected_bootstrap_keys
        or bootstrap.get("schema") != materialize.AUTHORITY_SCHEMA
        or bootstrap.get("version") != 2
        or type(bootstrap.get("created_at_epoch")) is not int
        or bootstrap["created_at_epoch"] < 1
        or bootstrap.get("package_authority_key_id") != package_key_id
        or bootstrap.get("package_authority_public_sha256")
        != package.sha256(package_anchor)
        or bootstrap.get("receipt_authority_key_id") != receipt_key_id
        or bootstrap.get("receipt_authority_public_sha256")
        != package.sha256(receipt_public_raw)
        or not SHA256_PATTERN.fullmatch(
            str(bootstrap.get("package_authority_private_sha256", ""))
        )
        or not isinstance(bootstrap.get("package_authority_source"), str)
        or not bootstrap["package_authority_source"]
    ):
        package.fail("tour-authority-bootstrap-binding-invalid")
    return AuthorityMaterial(
        package_anchor=package_anchor,
        package_private=package_private,
        package_public=package_public,
        package_key_id=package_key_id,
        receipt_private_raw=receipt_private_raw,
        receipt_public_raw=receipt_public_raw,
        receipt_key_id=receipt_key_id,
        authority_bootstrap_raw=bootstrap_raw,
        authority_bootstrap_signature=bootstrap_signature,
    )


def _host_machine_id_digest() -> str:
    raw = package.read_regular(
        MACHINE_ID_PATH, 64, expected_modes=(0o444, 0o644)
    ).strip()
    if not MACHINE_ID_PATTERN.fullmatch(raw):
        package.fail("tour-host-machine-id-invalid")
    return package.sha256(raw)


def _controller_and_build_receipt(
    controller_path: str, build_receipt_path: str, package_key_id: str
) -> tuple[bytes, bytes, dict[str, Any]]:
    controller = package.read_regular(
        controller_path,
        package.MAX_BINARY_BYTES,
        expected_modes=(0o555, 0o755),
    )
    build_receipt_raw = package.read_regular(
        build_receipt_path,
        package.MAX_JSON_BYTES,
        expected_modes=(0o444, 0o644),
    )
    build_receipt = package.parse_strict_json(
        build_receipt_raw, "tour-native-build-receipt", trailing_newline=True
    )
    package.validate_build_receipt(
        build_receipt, controller, package_key_id
    )
    if (
        build_receipt.get("installer_package_authority_bound") is not True
        or build_receipt.get("installer_package_authority_key_id")
        != package_key_id
    ):
        package.fail("tour-installer-package-authority-binding-required")
    return controller, build_receipt_raw, build_receipt


def _materialization_payload(
    *,
    authority: AuthorityMaterial,
    build_receipt_raw: bytes,
    build_receipt: dict[str, Any],
    host_machine_id_digest: str,
    materialized_at_epoch: int,
) -> dict[str, Any]:
    return {
        "accepted_installer_mode": ACCEPTED_INSTALLER_MODE,
        "allowed_operations": ALLOWED_OPERATIONS,
        "artifact_bundle_path": ARTIFACT_BUNDLE_PATH,
        "artifact_manifest_sha256": ARTIFACT_MANIFEST_SHA256,
        "artifact_public_tree_sha256": ARTIFACT_PUBLIC_TREE_SHA256,
        "artifact_slug": ARTIFACT_SLUG,
        "authoritative": False,
        "authority_bootstrap_sha256": package.sha256(
            authority.authority_bootstrap_raw
        ),
        "host_install_permitted": False,
        "host_machine_id_digest": host_machine_id_digest,
        "materialized_at_epoch": materialized_at_epoch,
        "native_build_receipt_sha256": package.sha256(build_receipt_raw),
        "network_required": False,
        "package_authority_key_id": authority.package_key_id,
        "performs_release_effects": False,
        "persistent_credential_installation_permitted": False,
        "production_ready": False,
        "publication_dispatch_authorized": True,
        "publication_target_root": PUBLICATION_TARGET_ROOT,
        "receipt_authority_key_id": authority.receipt_key_id,
        "receipt_authority_public_sha256": package.sha256(
            authority.receipt_public_raw
        ),
        "root_helper_authorization_required": True,
        "runtime_deployment_permitted": False,
        "schema": MATERIALIZATION_SCHEMA,
        "source_manifest_digest": build_receipt["source_manifest_digest"],
        "valid_until_epoch": materialized_at_epoch
        + MATERIALIZATION_TTL_SECONDS,
        "version": 4,
    }


def _publish_private_materialization(
    output: str, materialization_raw: bytes, signature: bytes
) -> None:
    target = Path(output)
    if not target.is_absolute() or target.name in {"", ".", ".."}:
        package.fail("tour-materialization-output-path-invalid")
    materialize._controlled_parent(target)  # noqa: SLF001
    temporary = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.tour-v4.", dir=os.fspath(target.parent)
        )
    )
    published = False
    try:
        os.chmod(temporary, 0o700)
        package.write_new_file(
            os.fspath(temporary / MATERIALIZATION_NAME),
            materialization_raw,
            0o400,
        )
        package.write_new_file(
            os.fspath(temporary / MATERIALIZATION_SIGNATURE_NAME),
            signature,
            0o400,
        )
        materialize._sync_directory(temporary)  # noqa: SLF001
        materialize._rename_noreplace(temporary, target)  # noqa: SLF001
        published = True
        materialize._sync_directory(target.parent)  # noqa: SLF001
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def materialize_tour(
    *,
    controller_path: str,
    build_receipt_path: str,
    output: str,
    now: int | None = None,
) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    if type(current) is not int or current < 1:
        package.fail("tour-materialization-time-invalid")
    authority = _load_authority_material()
    _, build_receipt_raw, build_receipt = _controller_and_build_receipt(
        controller_path, build_receipt_path, authority.package_key_id
    )
    host_digest = _host_machine_id_digest()
    payload = _materialization_payload(
        authority=authority,
        build_receipt_raw=build_receipt_raw,
        build_receipt=build_receipt,
        host_machine_id_digest=host_digest,
        materialized_at_epoch=current,
    )
    raw = package.canonical_json(payload)
    signature = authority.package_private.sign(
        package.framed(MATERIALIZATION_SIGNATURE_DOMAIN, raw)
    )
    if _host_machine_id_digest() != host_digest:
        package.fail("tour-host-machine-id-changed")
    _publish_private_materialization(output, raw, signature)
    return {
        "authoritative": False,
        "host_install_permitted": False,
        "materialization_root": output,
        "materialization_sha256": package.sha256(raw),
        "network_required": False,
        "package_authority_key_id": authority.package_key_id,
        "performs_release_effects": False,
        "production_ready": False,
        "publication_dispatch_authorized": True,
        "receipt_authority_key_id": authority.receipt_key_id,
        "runtime_deployment_permitted": False,
        "schema": (
            "propertyquarry.release-control.single-host-tour-publication-"
            "materialization-result.v4"
        ),
        "valid_until_epoch": payload["valid_until_epoch"],
        "version": 4,
    }


def _validate_materialization(
    *,
    raw: bytes,
    signature: bytes,
    authority: AuthorityMaterial,
    build_receipt_raw: bytes,
    build_receipt: dict[str, Any],
    current: int,
) -> dict[str, Any]:
    if len(signature) != 64:
        package.fail("tour-materialization-signature-size-invalid")
    try:
        authority.package_public.verify(
            signature,
            package.framed(MATERIALIZATION_SIGNATURE_DOMAIN, raw),
        )
    except InvalidSignature:
        package.fail("tour-materialization-signature-invalid")
    payload = package.parse_strict_json(raw, "tour-materialization")
    materialized = payload.get("materialized_at_epoch")
    valid_until = payload.get("valid_until_epoch")
    false_claims = (
        "authoritative",
        "host_install_permitted",
        "network_required",
        "performs_release_effects",
        "persistent_credential_installation_permitted",
        "production_ready",
        "runtime_deployment_permitted",
    )
    true_claims = (
        "publication_dispatch_authorized",
        "root_helper_authorization_required",
    )
    if (
        set(payload) != MATERIALIZATION_KEYS
        or any(payload.get(key) is not False for key in false_claims)
        or any(payload.get(key) is not True for key in true_claims)
        or type(materialized) is not int
        or type(valid_until) is not int
        or type(current) is not int
        or materialized < 1
        or valid_until != materialized + MATERIALIZATION_TTL_SECONDS
        or current < materialized
        or current > valid_until
    ):
        package.fail("tour-materialization-shape-or-freshness-invalid")
    expected = _materialization_payload(
        authority=authority,
        build_receipt_raw=build_receipt_raw,
        build_receipt=build_receipt,
        host_machine_id_digest=_host_machine_id_digest(),
        materialized_at_epoch=materialized,
    )
    if payload != expected:
        package.fail("tour-materialization-binding-invalid")
    return payload


def _read_materialization_root(root: str) -> dict[str, bytes]:
    return materialize._read_exact_private_directory(  # noqa: SLF001
        root, MATERIALIZATION_FILES
    )


def _payloads_for(
    *,
    controller: bytes,
    build_receipt_raw: bytes,
    authority: AuthorityMaterial,
    materialization_files: dict[str, bytes],
) -> dict[str, bytes]:
    payloads = {
        CONTROLLER_PATH: controller,
        BUILD_RECEIPT_PATH: build_receipt_raw,
        PACKAGE_ANCHOR_PATH: authority.package_anchor,
        AUTHORITY_BOOTSTRAP_PATH: authority.authority_bootstrap_raw,
        AUTHORITY_BOOTSTRAP_SIGNATURE_PATH: (
            authority.authority_bootstrap_signature
        ),
        RECEIPT_KEY_PATH: authority.receipt_private_raw,
        RECEIPT_ANCHOR_PATH: authority.receipt_public_raw,
        MATERIALIZATION_PATH: materialization_files[MATERIALIZATION_NAME],
        MATERIALIZATION_SIGNATURE_PATH: materialization_files[
            MATERIALIZATION_SIGNATURE_NAME
        ],
    }
    if set(payloads) != set(FILE_LAYOUT):
        package.fail("tour-package-internal-file-layout-invalid")
    return payloads


def _file_records(payloads: dict[str, bytes]) -> list[dict[str, Any]]:
    return [
        {
            "mode": f"{FILE_LAYOUT[path][0]:04o}",
            "path": path,
            "purpose": FILE_LAYOUT[path][1],
            "sha256": package.sha256(payloads[path]),
            "size": len(payloads[path]),
        }
        for path in sorted(payloads)
    ]


def _manifest_for(
    *,
    payloads: dict[str, bytes],
    authority: AuthorityMaterial,
    materialization: dict[str, Any],
) -> dict[str, Any]:
    return {
        "accepted_installer_mode": ACCEPTED_INSTALLER_MODE,
        "archive_format": ARCHIVE_FORMAT,
        "authority_bootstrap_sha256": package.sha256(
            authority.authority_bootstrap_raw
        ),
        "files": _file_records(payloads),
        "host_install_permitted": False,
        "materialization_sha256": package.sha256(
            payloads[MATERIALIZATION_PATH]
        ),
        "native_build_receipt_sha256": package.sha256(
            payloads[BUILD_RECEIPT_PATH]
        ),
        "network_required": False,
        "non_authoritative_until": NON_AUTHORITATIVE_UNTIL,
        "package_authority_key_id": authority.package_key_id,
        "package_signing_private_key_included": False,
        "performs_runtime_deployment": False,
        "profile": PROFILE,
        "receipt_authority_key_id": authority.receipt_key_id,
        "receipt_signing_private_key_included": True,
        "root_helper_verification_required": True,
        "runtime_deployment_permitted": False,
        "schema": PACKAGE_SCHEMA,
        "source_manifest_digest": materialization["source_manifest_digest"],
        "version": 4,
    }


def build_package(
    *,
    controller_path: str,
    build_receipt_path: str,
    materialization_root: str,
    output: str,
    now: int | None = None,
) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    authority = _load_authority_material()
    controller, build_receipt_raw, build_receipt = (
        _controller_and_build_receipt(
            controller_path, build_receipt_path, authority.package_key_id
        )
    )
    materialization_files = _read_materialization_root(materialization_root)
    materialization = _validate_materialization(
        raw=materialization_files[MATERIALIZATION_NAME],
        signature=materialization_files[MATERIALIZATION_SIGNATURE_NAME],
        authority=authority,
        build_receipt_raw=build_receipt_raw,
        build_receipt=build_receipt,
        current=current,
    )
    payloads = _payloads_for(
        controller=controller,
        build_receipt_raw=build_receipt_raw,
        authority=authority,
        materialization_files=materialization_files,
    )
    manifest = _manifest_for(
        payloads=payloads,
        authority=authority,
        materialization=materialization,
    )
    manifest_raw = package.canonical_json(manifest)
    manifest_signature = authority.package_private.sign(
        package.framed(PACKAGE_SIGNATURE_DOMAIN, manifest_raw)
    )
    members: dict[str, tuple[int, bytes]] = {
        MANIFEST_NAME: (0o444, manifest_raw),
        MANIFEST_SIGNATURE_NAME: (0o444, manifest_signature),
    }
    members.update(
        {path: (FILE_LAYOUT[path][0], raw) for path, raw in payloads.items()}
    )
    archive_raw = package._tar_bytes(members)  # noqa: SLF001
    package.write_new_file(output, archive_raw, 0o400)
    return {
        "authoritative": False,
        "host_install_permitted": False,
        "manifest_sha256": package.sha256(manifest_raw),
        "network_required": False,
        "package_authority_key_id": authority.package_key_id,
        "package_sha256": package.sha256(archive_raw),
        "performs_release_effects": False,
        "production_ready": False,
        "publication_dispatch_authorized": True,
        "receipt_authority_key_id": authority.receipt_key_id,
        "runtime_deployment_permitted": False,
        "schema": (
            "propertyquarry.release-control.single-host-tour-publication-"
            "package-build-result.v4"
        ),
        "valid_until_epoch": materialization["valid_until_epoch"],
        "version": 4,
    }


def _safe_archive_name(name: str) -> bool:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
        or len(name.encode("utf-8")) > 240
    ):
        return False
    parsed = PurePosixPath(name)
    return (
        all(part not in {"", ".", ".."} for part in parsed.parts)
        and "/".join(parsed.parts) == name
    )


def _archive_members(package_path: str) -> tuple[bytes, dict[str, bytes], dict[str, int]]:
    archive_raw = package.read_regular(
        package_path, package.MAX_ARCHIVE_BYTES, expected_modes=(0o400,)
    )
    try:
        archive = tarfile.open(fileobj=io.BytesIO(archive_raw), mode="r:")
    except tarfile.TarError:
        package.fail("tour-archive-invalid")
    members: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    with archive:
        if archive.pax_headers:
            package.fail("tour-archive-pax-header-invalid")
        infos = archive.getmembers()
        if len(infos) != len(EXACT_MEMBER_NAMES):
            package.fail("tour-archive-member-count-invalid")
        for info in infos:
            if (
                not _safe_archive_name(info.name)
                or info.name in members
                or info.name not in EXACT_MEMBER_NAMES
                or info.type != tarfile.REGTYPE
                or not info.isfile()
                or info.uid != 0
                or info.gid != 0
                or info.uname != ""
                or info.gname != ""
                or info.mtime != 0
                or info.linkname != ""
                or info.pax_headers
                or not 1 <= info.size <= package.MAX_BINARY_BYTES
                or info.mode & ~0o777
            ):
                package.fail("tour-archive-member-invalid")
            extracted = archive.extractfile(info)
            if extracted is None:
                package.fail("tour-archive-member-unreadable")
            raw = extracted.read(info.size + 1)
            if len(raw) != info.size:
                package.fail("tour-archive-member-size-invalid")
            members[info.name] = raw
            modes[info.name] = info.mode
    if set(members) != EXACT_MEMBER_NAMES:
        package.fail("tour-archive-member-set-invalid")
    reconstructed = package._tar_bytes(  # noqa: SLF001
        {name: (modes[name], raw) for name, raw in members.items()}
    )
    if reconstructed != archive_raw:
        package.fail("tour-archive-not-deterministic-ustar")
    return archive_raw, members, modes


def _validate_file_records(
    manifest: dict[str, Any],
    members: dict[str, bytes],
    modes: dict[str, int],
) -> None:
    records = manifest.get("files")
    if not isinstance(records, list) or len(records) != len(FILE_LAYOUT):
        package.fail("tour-manifest-files-invalid")
    expected_paths = sorted(FILE_LAYOUT)
    observed_paths: list[str] = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "mode",
            "path",
            "purpose",
            "sha256",
            "size",
        }:
            package.fail("tour-manifest-file-shape-invalid")
        path = record.get("path")
        if not isinstance(path, str) or path not in FILE_LAYOUT:
            package.fail("tour-manifest-file-path-invalid")
        observed_paths.append(path)
        expected_mode, expected_purpose = FILE_LAYOUT[path]
        size = record.get("size")
        if (
            record.get("mode") != f"{expected_mode:04o}"
            or record.get("purpose") != expected_purpose
            or record.get("sha256") != package.sha256(members[path])
            or type(size) is not int
            or size != len(members[path])
            or modes[path] != expected_mode
        ):
            package.fail("tour-manifest-file-binding-invalid")
    if observed_paths != expected_paths:
        package.fail("tour-manifest-file-order-invalid")


def verify_package(
    package_path: str, *, now: int | None = None
) -> VerifiedTourPackage:
    current = int(time.time()) if now is None else now
    authority = _load_authority_material()
    archive_raw, members, modes = _archive_members(package_path)
    if (
        modes[MANIFEST_NAME] != 0o444
        or modes[MANIFEST_SIGNATURE_NAME] != 0o444
        or len(members[MANIFEST_SIGNATURE_NAME]) != 64
    ):
        package.fail("tour-manifest-member-metadata-invalid")
    manifest_raw = members[MANIFEST_NAME]
    try:
        authority.package_public.verify(
            members[MANIFEST_SIGNATURE_NAME],
            package.framed(PACKAGE_SIGNATURE_DOMAIN, manifest_raw),
        )
    except InvalidSignature:
        package.fail("tour-manifest-signature-invalid")
    manifest = package.parse_strict_json(manifest_raw, "tour-manifest")
    false_claims = (
        "host_install_permitted",
        "network_required",
        "package_signing_private_key_included",
        "performs_runtime_deployment",
        "runtime_deployment_permitted",
    )
    true_claims = (
        "receipt_signing_private_key_included",
        "root_helper_verification_required",
    )
    if (
        set(manifest) != MANIFEST_KEYS
        or any(manifest.get(key) is not False for key in false_claims)
        or any(manifest.get(key) is not True for key in true_claims)
    ):
        package.fail("tour-manifest-shape-invalid")
    _validate_file_records(manifest, members, modes)
    if (
        members[PACKAGE_ANCHOR_PATH] != authority.package_anchor
        or members[AUTHORITY_BOOTSTRAP_PATH]
        != authority.authority_bootstrap_raw
        or members[AUTHORITY_BOOTSTRAP_SIGNATURE_PATH]
        != authority.authority_bootstrap_signature
        or members[RECEIPT_KEY_PATH] != authority.receipt_private_raw
        or members[RECEIPT_ANCHOR_PATH] != authority.receipt_public_raw
    ):
        package.fail("tour-packaged-authority-material-mismatch")
    build_receipt = package.parse_strict_json(
        members[BUILD_RECEIPT_PATH],
        "tour-packaged-native-build-receipt",
        trailing_newline=True,
    )
    package.validate_build_receipt(
        build_receipt, members[CONTROLLER_PATH], authority.package_key_id
    )
    if (
        build_receipt.get("installer_package_authority_bound") is not True
        or build_receipt.get("installer_package_authority_key_id")
        != authority.package_key_id
    ):
        package.fail("tour-packaged-installer-authority-binding-invalid")
    materialization = _validate_materialization(
        raw=members[MATERIALIZATION_PATH],
        signature=members[MATERIALIZATION_SIGNATURE_PATH],
        authority=authority,
        build_receipt_raw=members[BUILD_RECEIPT_PATH],
        build_receipt=build_receipt,
        current=current,
    )
    payloads = {path: members[path] for path in FILE_LAYOUT}
    expected_manifest = _manifest_for(
        payloads=payloads,
        authority=authority,
        materialization=materialization,
    )
    if manifest != expected_manifest:
        package.fail("tour-manifest-binding-invalid")
    return VerifiedTourPackage(
        archive_sha256=package.sha256(archive_raw),
        manifest_sha256=package.sha256(manifest_raw),
        manifest=manifest,
        materialization=materialization,
        members=members,
        modes=modes,
    )


def _verified_result(verified: VerifiedTourPackage) -> dict[str, Any]:
    return {
        "authoritative": False,
        "fresh": True,
        "host_install_permitted": False,
        "manifest_sha256": verified.manifest_sha256,
        "network_required": False,
        "package_authority_key_id": verified.manifest[
            "package_authority_key_id"
        ],
        "package_sha256": verified.archive_sha256,
        "performs_release_effects": False,
        "production_ready": False,
        "publication_dispatch_authorized": True,
        "receipt_authority_key_id": verified.manifest[
            "receipt_authority_key_id"
        ],
        "runtime_deployment_permitted": False,
        "schema": (
            "propertyquarry.release-control.single-host-tour-publication-"
            "package-verify-result.v4"
        ),
        "valid_until_epoch": verified.materialization["valid_until_epoch"],
        "version": 4,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    materialization = commands.add_parser("materialize")
    materialization.add_argument("--controller", required=True)
    materialization.add_argument("--native-build-receipt", required=True)
    materialization.add_argument("--output", required=True)
    build = commands.add_parser("build")
    build.add_argument("--controller", required=True)
    build.add_argument("--native-build-receipt", required=True)
    build.add_argument("--materialization-root", required=True)
    build.add_argument("--output", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--package", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
        if arguments.command == "materialize":
            result = materialize_tour(
                controller_path=arguments.controller,
                build_receipt_path=arguments.native_build_receipt,
                output=arguments.output,
            )
        elif arguments.command == "build":
            result = build_package(
                controller_path=arguments.controller,
                build_receipt_path=arguments.native_build_receipt,
                materialization_root=arguments.materialization_root,
                output=arguments.output,
            )
        else:
            result = _verified_result(verify_package(arguments.package))
        sys.stdout.buffer.write(package.canonical_json(result) + b"\n")
        return 0
    except (
        package.PackageFailure,
        materialize.MaterializeFailure,
        materialize.package.PackageFailure,
    ) as error:
        sys.stderr.write(f"propertyquarry-tour-package-rejected:{error}\n")
        return 50
    except KeyboardInterrupt:
        sys.stderr.write("propertyquarry-tour-package-rejected:interrupted\n")
        return 50
    except Exception:
        sys.stderr.write("propertyquarry-tour-package-rejected:internal-failure\n")
        return 50


if __name__ == "__main__":
    raise SystemExit(main())
