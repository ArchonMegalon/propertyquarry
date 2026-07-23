#!/usr/bin/env python3
"""Bootstrap and materialize the single-host production signing authority.

The persistent authority is created once.  Each materialization then observes
the live host, constructs the exact transaction plan, signs the profile, and
publishes a short-lived private bundle with no-replace semantics.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
import fcntl
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import grp
import pwd
import re
import secrets
import selectors
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, NoReturn

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


TOOLS = Path(__file__).resolve().parent
MODULE = TOOLS.parent
REPOSITORY_ROOT = MODULE.parents[1]
sys.dont_write_bytecode = True
_PACKAGE_SPEC = importlib.util.spec_from_file_location(
    "propertyquarry_single_host_package_v2", TOOLS / "package.py"
)
if _PACKAGE_SPEC is None or _PACKAGE_SPEC.loader is None:  # pragma: no cover
    raise RuntimeError("package-module-unavailable")
package = importlib.util.module_from_spec(_PACKAGE_SPEC)
sys.modules[_PACKAGE_SPEC.name] = package
_PACKAGE_SPEC.loader.exec_module(package)


AUTHORITY_SCHEMA = (
    "propertyquarry.release-control.single-host-production-authority-bootstrap.v2"
)
AUTHORITY_SIGNATURE_DOMAIN = AUTHORITY_SCHEMA.encode("ascii") + b"\0"
AUTHORITY_INTENT_SCHEMA = (
    "propertyquarry.release-control.single-host-production-authority-bootstrap-intent.v2"
)
AUTHORITY_INTENT_SIGNATURE_DOMAIN = AUTHORITY_INTENT_SCHEMA.encode("ascii") + b"\0"
MATERIAL_SCHEMA = (
    "propertyquarry.release-control.single-host-production-materialization.v2"
)
MATERIAL_SIGNATURE_DOMAIN = MATERIAL_SCHEMA.encode("ascii") + b"\0"
CANONICAL_AUTHORITY_ROOT = Path(
    "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/"
    "authority-static-canonical"
)
CANONICAL_PACKAGE_PRIVATE = CANONICAL_AUTHORITY_ROOT / "keys/package-authority-v2.key"
CANONICAL_PACKAGE_ANCHOR = CANONICAL_AUTHORITY_ROOT / "anchors/package-authority-v2.pem"
PRODUCTION_RECEIPT_AUTHORITY_ROOT = Path(
    "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/"
    "single-host-v2-receipt-authority"
)
RUNNER_RESERVATION_PARENT = Path(
    "/docker/property/state/runtime/propertyquarry-release-authority-v2.private"
)
RUNNER_RESERVATION_ROOT = (
    RUNNER_RESERVATION_PARENT / "single-host-v2-runner-reservation"
)
RUNNER_RESERVATION_LOCK = (
    RUNNER_RESERVATION_PARENT / ".single-host-v2-runner-reservation.lock"
)
RUNNER_RESERVATION_TERMINAL_ROOT = (
    RUNNER_RESERVATION_PARENT / "single-host-v2-runner-reservation-terminal"
)
RUNNER_PREREQUISITE_APPROVAL_ROOT = (
    RUNNER_RESERVATION_PARENT / "single-host-v2-runner-prerequisite-approvals"
)
RUNNER_RELEASE_CHECKOUT_ROOT = (
    RUNNER_RESERVATION_PARENT / "single-host-v2-release-checkouts"
)
RUNNER_RESERVATION_NAME = "runner-reservation.v2.json"
RUNNER_RESERVATION_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-reservation.v2"
)
RUNNER_RESERVATION_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-reservation-signature.v2\0"
)
RUNNER_LAUNCH_TICKET_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-launch-ticket.v2"
)
RUNNER_LAUNCH_TICKET_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-launch-ticket-signature.v2\0"
)
RUNNER_MATERIALIZATION_CLAIM_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-materialization-claim.v2"
)
RUNNER_MATERIALIZATION_CLAIM_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-materialization-claim-signature.v2\0"
)
RUNNER_MATERIALIZATION_BINDING_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-materialization-binding.v2"
)
RUNNER_MATERIALIZATION_BINDING_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-materialization-binding-signature.v2\0"
)
RUNNER_PREREQUISITE_INTENT_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-intent.v2"
)
RUNNER_PREREQUISITE_INTENT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-intent-signature.v2\0"
)
RUNNER_PREREQUISITE_APPROVAL_SCHEMA = (
    "propertyquarry.release-control.single-host-runner-prerequisite-approval.v2"
)
RUNNER_PREREQUISITE_APPROVAL_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-prerequisite-approval-signature.v2\0"
)
RUNNER_PREREQUISITE_ENVIRONMENT = "propertyquarry-production"
RUNNER_PREREQUISITE_JOB = "propertyquarry-protected-dispatch-inputs"
RUNNER_LABEL_DERIVATION_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-label.v2\0"
)
RUNNER_RESERVATION_TTL_SECONDS = 6 * 60 * 60
RUNNER_TICKET_TTL_SECONDS = 30 * 60
CANONICAL_PACKAGE_PRIVATE_SHA256 = (
    "sha256:8b9106db85e8ce423d454bb14c863b6c0d481b061eaae0bd4b584d7071cbc2e1"
)
CANONICAL_PACKAGE_ANCHOR_SHA256 = (
    "sha256:2496701f8bc363ad470e43bdd3827e20cb86f95d731cdeaa86dae2d823504eda"
)
CANONICAL_PACKAGE_KEY_ID = (
    "sha256:d50fb57cafaddf6825b5f0907b772857957356ac3b29307a07c1a113a8d27af8"
)
LEGACY_INSTALLED_PACKAGE_ANCHOR = Path(
    "/etc/propertyquarry-release-control-v2/package-authority-v2.pem"
)
INSTALLED_AUTHORITY_ROOT = "/etc/propertyquarry-release-single-host-v2"
FIXED_CLOUDFLARED_IMAGE = (
    "cloudflare/cloudflared@sha256:"
    "18626b1baac4450214535cd5bc40ef44c0635244d585ebf707749c22b6f3408f"
)
MAX_OBSERVATION_SECONDS = 900
MAX_PUBLICATION_EVIDENCE_AGE_SECONDS = 21_600
MAX_RELEASE_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_REMOTE_BUNDLE_BYTES = 2 * 1024 * 1024
GITHUB_API_HOST = "api.github.com"
GITHUB_API_VERSION = "2026-03-10"
GITHUB_USER_AGENT = "propertyquarry-release-materializer-v2"
GITHUB_REPOSITORY_API = "repos/ArchonMegalon/propertyquarry"
PUBLISH_WORKFLOW_PATH = ".github/workflows/propertyquarry-publish-runtime-images.yml"
SMOKE_WORKFLOW_PATH = ".github/workflows/smoke-runtime.yml"
PUBLISH_WORKFLOW_REF = (
    "ArchonMegalon/propertyquarry/.github/workflows/"
    "propertyquarry-publish-runtime-images.yml@refs/heads/main"
)
PUBLISH_WORKFLOW_IDENTITY = "https://github.com/" + PUBLISH_WORKFLOW_REF
GITHUB_ATTESTATION_VERIFIER_SHA256 = (
    "sha256:56b8bbbb27b066ecb33dbef9a256dc9d1314adaeff0908a752feba6c34053b40"
)
GITHUB_ATTESTATION_VERIFIER_BYTES = 40_722_594
GITHUB_ATTESTATION_VERIFIER_VERSION = "gh version 2.96.0 (2026-07-02)"
FINAL_ARTIFACT_MEMBERS = frozenset(
    {"receipt.json", "sigstore/render-bundle.json", "sigstore/web-bundle.json"}
)
PREFLIGHT_ARTIFACT_MEMBERS = frozenset(
    {"hygiene.sha256", "preflight.json", "release-hygiene.json"}
)
AUTHORITY_FILES = frozenset(
    {
        "authority-bootstrap.v2.json",
        "authority-bootstrap.v2.sig",
        "receipt-authority-v2.key",
        "receipt-authority-v2.pem",
    }
)
MATERIAL_FILES = frozenset(
    {
        "authority.v2.json",
        "authority.v2.sig",
        "materialization-receipt.v2.json",
        "materialization-receipt.v2.sig",
        "runner-launch-ticket.v2.json",
        "runner-prerequisite-approval.v2.json",
        "runner-prerequisite-intent.v2.json",
        "runner-reservation.v2.json",
        "transaction-plan.v2.json",
    }
)
SHA1 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
NUMERIC_ID = re.compile(r"^[1-9][0-9]{0,19}$")
AT_FDCWD = -100
RENAME_NOREPLACE = 1


def _bootstrap_checkpoint(_name: str) -> None:
    """Test-only crash boundary; production intentionally does nothing."""


class MaterializeFailure(ValueError):
    """A secret-free production-material rejection."""


def fail(code: str) -> NoReturn:
    raise MaterializeFailure(code)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _numeric_id(value: Any) -> bool:
    return isinstance(value, str) and NUMERIC_ID.fullmatch(value) is not None


def _framed(domain: bytes, raw: bytes) -> bytes:
    return domain + len(raw).to_bytes(8, "big") + raw


def _absolute(value: str, code: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value or value == "/":
        fail(code)
    return path


def _controlled_parent(target: Path) -> None:
    try:
        metadata = target.parent.lstat()
    except OSError:
        fail("output-parent-unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or target.parent.resolve() != target.parent
    ):
        fail("output-parent-unsafe")


def _rename_noreplace(source: Path, target: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        fail("rename-noreplace-unavailable")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(target),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error == 17:
            fail("output-exists")
        fail("output-publish-failed")


def _publish_private_directory(target_value: str, files: dict[str, bytes]) -> None:
    target = _absolute(target_value, "output-path-invalid")
    _controlled_parent(target)
    if set(files) not in (AUTHORITY_FILES, MATERIAL_FILES):
        fail("output-file-set-invalid")
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.materializing.", dir=target.parent)
    )
    published = False
    try:
        os.chmod(temporary, 0o700)
        for name in sorted(files):
            if not name or "/" in name or name in {".", ".."}:
                fail("output-name-invalid")
            descriptor = os.open(
                temporary / name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o400,
            )
            try:
                os.fchmod(descriptor, 0o400)
                raw = files[name]
                written = 0
                while written < len(raw):
                    written += os.write(descriptor, raw[written:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        directory = os.open(
            temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        _rename_noreplace(temporary, target)
        published = True
        parent = os.open(
            target.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if not published:
            shutil.rmtree(temporary, ignore_errors=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_private_file_noreplace(path: Path, raw: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | os.O_NOFOLLOW
        | os.O_CLOEXEC,
        0o400,
    )
    succeeded = False
    try:
        _bootstrap_checkpoint(f"private-file:{path.name}:after-open")
        os.fchmod(descriptor, 0o400)
        _bootstrap_checkpoint(f"private-file:{path.name}:after-fchmod")
        written = 0
        while written < len(raw):
            count = os.write(descriptor, raw[written:])
            if count < 1:
                fail("bootstrap-stage-write-failed")
            written += count
            _bootstrap_checkpoint(f"private-file:{path.name}:after-write")
        os.fsync(descriptor)
        _bootstrap_checkpoint(f"private-file:{path.name}:after-fsync")
        succeeded = True
    finally:
        os.close(descriptor)
        if not succeeded:
            try:
                path.unlink()
            except OSError:
                pass
    _sync_directory(path.parent)
    _bootstrap_checkpoint(f"private-file:{path.name}:after-parent-fsync")


def _bootstrap_stage_paths() -> tuple[Path, Path, Path]:
    target = PRODUCTION_RECEIPT_AUTHORITY_ROOT
    stage = target.parent / f".{target.name}.bootstrap.v2"
    lock = target.parent / f".{target.name}.bootstrap.v2.lock"
    return target, stage, lock


def _acquire_bootstrap_lock(lock: Path) -> int:
    descriptor = os.open(
        lock,
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
        ):
            fail("bootstrap-lock-invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        after = os.fstat(descriptor)
        if (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino):
            fail("bootstrap-lock-mutated")
        _sync_directory(lock.parent)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_bootstrap_stage_file(
    path: Path, *, allow_empty: bool = False, maximum: int = package.MAX_JSON_BYTES
) -> bytes:
    try:
        metadata = path.lstat()
    except OSError:
        fail("bootstrap-stage-file-unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & ~0o400
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_nlink != 1
        or metadata.st_size < (0 if allow_empty else 1)
        or metadata.st_size > maximum
    ):
        fail("bootstrap-stage-file-metadata-invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o400:
        try:
            os.chmod(path, 0o400)
        except OSError:
            fail("bootstrap-stage-file-mode-repair-failed")
        _sync_directory(path.parent)
        metadata = path.lstat()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        raw = b""
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(raw) > maximum
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    ):
        fail("bootstrap-stage-file-mutated")
    return raw


def _bootstrap_intent(
    *, created: int, package_id: str, package_private: Ed25519PrivateKey
) -> bytes:
    payload = {
        "created_at_epoch": created,
        "package_authority_key_id": package_id,
        "schema": AUTHORITY_INTENT_SCHEMA,
        "target": os.fspath(PRODUCTION_RECEIPT_AUTHORITY_ROOT),
        "version": 2,
    }
    payload_raw = package.canonical_json(payload)
    signature = package_private.sign(
        _framed(AUTHORITY_INTENT_SIGNATURE_DOMAIN, payload_raw)
    )
    return package.canonical_json(
        {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii"),
            "signature_key_id": package_id,
        }
    )


def _validate_bootstrap_intent(
    raw: bytes, *, package_id: str, package_public: Ed25519PublicKey
) -> int:
    try:
        wire = package.parse_strict_json(raw, "authority-bootstrap-intent")
        if set(wire) != {"payload", "signature", "signature_key_id"}:
            fail("bootstrap-intent-shape-invalid")
        payload = wire["payload"]
        signature_text = wire["signature"]
        if (
            type(payload) is not dict
            or type(signature_text) is not str
            or wire["signature_key_id"] != package_id
        ):
            fail("bootstrap-intent-shape-invalid")
        signature = base64.b64decode(
            signature_text + "=" * ((4 - len(signature_text) % 4) % 4),
            altchars=b"-_",
            validate=True,
        )
        if (
            len(signature) != 64
            or base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
            != signature_text
        ):
            fail("bootstrap-intent-signature-encoding-invalid")
        payload_raw = package.canonical_json(payload)
        package_public.verify(
            signature, _framed(AUTHORITY_INTENT_SIGNATURE_DOMAIN, payload_raw)
        )
    except (InvalidSignature, ValueError, binascii.Error, package.PackageFailure):
        fail("bootstrap-intent-invalid")
    created = payload.get("created_at_epoch")
    if payload != {
        "created_at_epoch": created,
        "package_authority_key_id": package_id,
        "schema": AUTHORITY_INTENT_SCHEMA,
        "target": os.fspath(PRODUCTION_RECEIPT_AUTHORITY_ROOT),
        "version": 2,
    } or type(created) is not int or created < 1:
        fail("bootstrap-intent-binding-invalid")
    return created


def _read_exact_private_directory(
    root_value: str, names: frozenset[str]
) -> dict[str, bytes]:
    root = _absolute(root_value, "private-root-invalid")
    try:
        before = root.lstat()
    except OSError:
        fail("private-root-unavailable")
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or stat.S_IMODE(before.st_mode) != 0o700
        or before.st_uid != os.geteuid()
        or before.st_gid != os.getegid()
        or root.resolve() != root
    ):
        fail("private-root-metadata-invalid")
    try:
        actual = frozenset(item.name for item in root.iterdir())
    except OSError:
        fail("private-root-read-failed")
    if actual != names:
        fail("private-root-file-set-invalid")
    result: dict[str, bytes] = {}
    for name in sorted(names):
        path = root / name
        try:
            metadata = path.lstat()
        except OSError:
            fail("private-file-unavailable")
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & ~0o400
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or not 1 <= metadata.st_size <= package.MAX_JSON_BYTES
        ):
            fail("private-file-metadata-invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(descriptor)
            raw = b""
            while len(raw) <= package.MAX_JSON_BYTES:
                chunk = os.read(descriptor, min(65_536, package.MAX_JSON_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw += chunk
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            len(raw) > package.MAX_JSON_BYTES
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != len(raw)
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        ):
            fail("private-file-mutated")
        result[name] = raw
    return result


def _public_identity(public: Ed25519PublicKey) -> tuple[bytes, str]:
    der = public.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return der, _digest(der)


def _key_material(private: Ed25519PrivateKey) -> tuple[bytes, bytes, str]:
    private_raw = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _der, key_id = _public_identity(private.public_key())
    return private_raw, public_raw, key_id


def _read_pinned_file(
    path: Path,
    *,
    mode: int,
    uid: int,
    gid: int,
    size: int,
    expected_digest: str | None = None,
) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        fail("pinned-authority-file-unavailable")
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or stat.S_IMODE(before.st_mode) != mode
        or before.st_uid != uid
        or before.st_gid != gid
        or before.st_nlink != 1
        or before.st_size != size
    ):
        fail("pinned-authority-file-metadata-invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        raw = os.read(descriptor, size + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(raw) != size
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or (expected_digest is not None and _digest(raw) != expected_digest)
    ):
        fail("pinned-authority-file-binding-invalid")
    return raw


def _load_canonical_package_authority(
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey, bytes, str]:
    for directory in (
        CANONICAL_AUTHORITY_ROOT,
        CANONICAL_AUTHORITY_ROOT / "keys",
        CANONICAL_AUTHORITY_ROOT / "anchors",
    ):
        try:
            metadata = directory.lstat()
        except OSError:
            fail("canonical-authority-root-unavailable")
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != 1000
            or metadata.st_gid != 1000
            or directory.resolve() != directory
        ):
            fail("canonical-authority-root-invalid")
    private_raw = _read_pinned_file(
        CANONICAL_PACKAGE_PRIVATE,
        mode=0o600,
        uid=1000,
        gid=1000,
        size=119,
        expected_digest=CANONICAL_PACKAGE_PRIVATE_SHA256,
    )
    anchor_raw = _read_pinned_file(
        CANONICAL_PACKAGE_ANCHOR,
        mode=0o600,
        uid=1000,
        gid=1000,
        size=113,
        expected_digest=CANONICAL_PACKAGE_ANCHOR_SHA256,
    )
    try:
        private = serialization.load_pem_private_key(private_raw, password=None)
        public = serialization.load_pem_public_key(anchor_raw)
    except (TypeError, ValueError):
        fail("canonical-package-key-invalid")
    if not isinstance(private, Ed25519PrivateKey) or not isinstance(
        public, Ed25519PublicKey
    ):
        fail("canonical-package-key-type-invalid")
    private_der, private_id = _public_identity(private.public_key())
    public_der, public_id = _public_identity(public)
    if (
        private_der != public_der
        or private_id != CANONICAL_PACKAGE_KEY_ID
        or public_id != CANONICAL_PACKAGE_KEY_ID
    ):
        fail("canonical-package-key-binding-invalid")
    installed = _read_pinned_file(
        LEGACY_INSTALLED_PACKAGE_ANCHOR,
        mode=0o444,
        uid=0,
        gid=0,
        size=113,
    )
    if installed != anchor_raw:
        fail("canonical-installed-package-anchor-mismatch")
    return private, public, anchor_raw, public_id


def _require_production_authority_root(authority_root: str) -> None:
    if authority_root != os.fspath(PRODUCTION_RECEIPT_AUTHORITY_ROOT):
        fail("production-receipt-authority-root-invalid")


def _validate_bootstrap_stage_directory(stage: Path) -> None:
    try:
        metadata = stage.lstat()
    except OSError:
        fail("bootstrap-stage-unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or stat.S_IMODE(metadata.st_mode) & ~0o700
        or stage.resolve() != stage
    ):
        fail("bootstrap-stage-metadata-invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        try:
            os.chmod(stage, 0o700)
        except OSError:
            fail("bootstrap-stage-repair-failed")
        _sync_directory(stage.parent)


def _cleanup_published_bootstrap_stage(
    stage: Path, *, package_id: str, package_public: Ed25519PublicKey
) -> None:
    if not stage.exists():
        return
    _validate_bootstrap_stage_directory(stage)
    try:
        entries = {item.name for item in stage.iterdir()}
    except OSError:
        fail("bootstrap-stage-read-failed")
    if entries == {"bootstrap-intent.v2.json"}:
        intent = stage / "bootstrap-intent.v2.json"
        _validate_bootstrap_intent(
            _read_bootstrap_stage_file(intent),
            package_id=package_id,
            package_public=package_public,
        )
        intent.unlink()
        _sync_directory(stage)
        entries = set()
    if entries:
        fail("bootstrap-published-stage-invalid")
    try:
        stage.rmdir()
    except OSError:
        fail("bootstrap-stage-cleanup-failed")
    _sync_directory(stage.parent)


def _stage_receipt_private_key(authority_stage: Path) -> Ed25519PrivateKey:
    final = authority_stage / "receipt-authority-v2.key"
    pending = authority_stage / ".receipt-authority-v2.key.pending"
    final_exists = final.exists()
    pending_exists = pending.exists()
    if final_exists and pending_exists:
        fail("bootstrap-key-stage-collision")
    if final_exists:
        raw = _read_bootstrap_stage_file(final, maximum=119)
        try:
            private = serialization.load_pem_private_key(raw, password=None)
        except (TypeError, ValueError):
            fail("bootstrap-staged-key-invalid")
        if (
            not isinstance(private, Ed25519PrivateKey)
            or _key_material(private)[0] != raw
        ):
            fail("bootstrap-staged-key-invalid")
        return private
    if pending_exists:
        try:
            metadata = pending.lstat()
        except OSError:
            fail("bootstrap-key-pending-unavailable")
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & ~0o400
            or metadata.st_uid != os.geteuid()
            or metadata.st_gid != os.getegid()
            or metadata.st_nlink != 1
            or not 0 <= metadata.st_size <= 119
        ):
            fail("bootstrap-key-pending-metadata-invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o400:
            try:
                os.chmod(pending, 0o400)
            except OSError:
                fail("bootstrap-key-pending-mode-repair-failed")
            _sync_directory(authority_stage)
        raw = _read_bootstrap_stage_file(
            pending, allow_empty=True, maximum=119
        )
        try:
            candidate = serialization.load_pem_private_key(raw, password=None)
        except (TypeError, ValueError):
            candidate = None
        if not isinstance(candidate, Ed25519PrivateKey) or _key_material(candidate)[0] != raw:
            try:
                pending.unlink()
            except OSError:
                fail("bootstrap-key-pending-remove-failed")
            _sync_directory(authority_stage)
            pending_exists = False
        else:
            _rename_noreplace(pending, final)
            _sync_directory(authority_stage)
            _bootstrap_checkpoint("after-key-promote")
            return candidate
    if not pending_exists:
        private = Ed25519PrivateKey.generate()
        private_raw, _anchor, _key_id = _key_material(private)
        _write_private_file_noreplace(pending, private_raw)
        _bootstrap_checkpoint("after-key-pending")
        _rename_noreplace(pending, final)
        _sync_directory(authority_stage)
        _bootstrap_checkpoint("after-key-promote")
        return private
    fail("bootstrap-key-stage-invalid")


def _resume_bootstrap_authority(
    *,
    stage: Path,
    created: int,
    package_private: Ed25519PrivateKey,
    package_public: Ed25519PublicKey,
    package_anchor: bytes,
    package_id: str,
) -> str:
    intent_path = stage / "bootstrap-intent.v2.json"
    authority_stage = stage / "authority"
    try:
        entries = {item.name for item in stage.iterdir()}
    except OSError:
        fail("bootstrap-stage-read-failed")
    if entries - {"bootstrap-intent.v2.json", "authority"}:
        fail("bootstrap-stage-entry-invalid")
    if intent_path.exists():
        try:
            created = _validate_bootstrap_intent(
                _read_bootstrap_stage_file(
                    intent_path, allow_empty=True
                ),
                package_id=package_id,
                package_public=package_public,
            )
        except MaterializeFailure:
            if entries != {"bootstrap-intent.v2.json"}:
                raise
            try:
                metadata = intent_path.lstat()
            except OSError:
                fail("bootstrap-intent-repair-invalid")
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o400
                or metadata.st_uid != os.geteuid()
                or metadata.st_gid != os.getegid()
                or metadata.st_nlink != 1
                or not 0 <= metadata.st_size <= package.MAX_JSON_BYTES
            ):
                fail("bootstrap-intent-repair-invalid")
            intent_path.unlink()
            _sync_directory(stage)
            _write_private_file_noreplace(
                intent_path,
                _bootstrap_intent(
                    created=created,
                    package_id=package_id,
                    package_private=package_private,
                ),
            )
            _bootstrap_checkpoint("after-intent")
    else:
        if entries:
            fail("bootstrap-intent-missing")
        _write_private_file_noreplace(
            intent_path,
            _bootstrap_intent(
                created=created,
                package_id=package_id,
                package_private=package_private,
            ),
        )
        _bootstrap_checkpoint("after-intent")
    if not authority_stage.exists():
        try:
            authority_stage.mkdir(mode=0o700)
            _bootstrap_checkpoint("after-authority-stage-mkdir")
            os.chmod(authority_stage, 0o700)
            _bootstrap_checkpoint("after-authority-stage-chmod")
        except OSError:
            fail("bootstrap-authority-stage-create-failed")
        _sync_directory(stage)
        _bootstrap_checkpoint("after-authority-stage-directory")
    _validate_bootstrap_stage_directory(authority_stage)
    try:
        authority_entries = {item.name for item in authority_stage.iterdir()}
    except OSError:
        fail("bootstrap-authority-stage-read-failed")
    if authority_entries - (set(AUTHORITY_FILES) | {".receipt-authority-v2.key.pending"}):
        fail("bootstrap-authority-stage-entry-invalid")
    receipt_private = _stage_receipt_private_key(authority_stage)
    receipt_key, receipt_anchor, receipt_id = _key_material(receipt_private)
    if receipt_id == package_id:
        fail("authority-key-role-collision")
    receipt = {
        "created_at_epoch": created,
        "package_authority_key_id": package_id,
        "package_authority_private_sha256": CANONICAL_PACKAGE_PRIVATE_SHA256,
        "package_authority_public_sha256": _digest(package_anchor),
        "package_authority_source": os.fspath(CANONICAL_AUTHORITY_ROOT),
        "receipt_authority_key_id": receipt_id,
        "receipt_authority_public_sha256": _digest(receipt_anchor),
        "schema": AUTHORITY_SCHEMA,
        "version": 2,
    }
    receipt_raw = package.canonical_json(receipt)
    signature = package_private.sign(
        _framed(AUTHORITY_SIGNATURE_DOMAIN, receipt_raw)
    )
    expected = {
        "authority-bootstrap.v2.json": receipt_raw,
        "authority-bootstrap.v2.sig": signature,
        "receipt-authority-v2.key": receipt_key,
        "receipt-authority-v2.pem": receipt_anchor,
    }
    for name in sorted(expected):
        path = authority_stage / name
        if path.exists():
            existing = _read_bootstrap_stage_file(
                path, allow_empty=True
            )
            if existing != expected[name]:
                if name == "receipt-authority-v2.key":
                    fail("bootstrap-authority-stage-binding-invalid")
                path.unlink()
                _sync_directory(authority_stage)
                _write_private_file_noreplace(path, expected[name])
        else:
            _write_private_file_noreplace(path, expected[name])
        _bootstrap_checkpoint(f"after-authority-file-{name}")
    try:
        final_entries = {item.name for item in authority_stage.iterdir()}
    except OSError:
        fail("bootstrap-authority-stage-read-failed")
    if final_entries != set(AUTHORITY_FILES):
        fail("bootstrap-authority-stage-incomplete")
    _sync_directory(authority_stage)
    _bootstrap_checkpoint("before-authority-promote")
    _rename_noreplace(authority_stage, PRODUCTION_RECEIPT_AUTHORITY_ROOT)
    _sync_directory(PRODUCTION_RECEIPT_AUTHORITY_ROOT.parent)
    _bootstrap_checkpoint("after-authority-promote")
    _cleanup_published_bootstrap_stage(
        stage, package_id=package_id, package_public=package_public
    )
    return receipt_id


def bootstrap_authority(
    *, authority_root: str, now: int | None = None
) -> dict[str, Any]:
    _require_production_authority_root(authority_root)
    target, stage, lock_path = _bootstrap_stage_paths()
    if os.fspath(target) != authority_root:
        fail("production-receipt-authority-root-invalid")
    _controlled_parent(target)
    lock = _acquire_bootstrap_lock(lock_path)
    try:
        package_private, package_public, package_anchor, package_id = (
            _load_canonical_package_authority()
        )
        if target.exists():
            (
                _package_anchor,
                _package_private,
                _package_public,
                _receipt_private,
                _receipt_public,
                loaded_package_id,
                receipt_id,
            ) = _load_authority(authority_root)
            if loaded_package_id != package_id:
                fail("authority-package-key-rebound")
            _cleanup_published_bootstrap_stage(
                stage, package_id=package_id, package_public=package_public
            )
            created_authority = False
        else:
            if not stage.exists():
                try:
                    stage.mkdir(mode=0o700)
                    _bootstrap_checkpoint("after-stage-mkdir")
                    os.chmod(stage, 0o700)
                    _bootstrap_checkpoint("after-stage-chmod")
                except OSError:
                    fail("bootstrap-stage-create-failed")
                _sync_directory(stage.parent)
                _bootstrap_checkpoint("after-stage-directory")
            _validate_bootstrap_stage_directory(stage)
            created = int(time.time()) if now is None else now
            if type(created) is not int or created < 1:
                fail("bootstrap-created-at-invalid")
            receipt_id = _resume_bootstrap_authority(
                stage=stage,
                created=created,
                package_private=package_private,
                package_public=package_public,
                package_anchor=package_anchor,
                package_id=package_id,
            )
            created_authority = True
        return {
            "authority_created": created_authority,
            "authority_root": authority_root,
            "installed_state_inspected": False,
            "package_authority_key_id": package_id,
            "receipt_authority_key_id": receipt_id,
            "schema": "propertyquarry.release-control.single-host-production-authority-bootstrap-result.v2",
            "version": 2,
        }
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)


def _load_authority(
    authority_root: str,
) -> tuple[bytes, Ed25519PrivateKey, Ed25519PublicKey, Ed25519PrivateKey, Ed25519PublicKey, str, str]:
    _require_production_authority_root(authority_root)
    package_private, package_public, package_anchor, package_id = (
        _load_canonical_package_authority()
    )
    files = _read_exact_private_directory(authority_root, AUTHORITY_FILES)
    try:
        receipt_private = serialization.load_pem_private_key(
            files["receipt-authority-v2.key"], password=None
        )
        receipt_public = serialization.load_pem_public_key(
            files["receipt-authority-v2.pem"]
        )
    except (TypeError, ValueError):
        fail("authority-key-invalid")
    if not all(
        (
            isinstance(receipt_private, Ed25519PrivateKey),
            isinstance(receipt_public, Ed25519PublicKey),
        )
    ):
        fail("authority-key-type-invalid")
    receipt_der, receipt_id = _public_identity(receipt_public)
    if (
        package_id == receipt_id
        or receipt_private.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        != receipt_der
    ):
        fail("authority-key-binding-invalid")
    bootstrap_raw = files["authority-bootstrap.v2.json"]
    try:
        bootstrap = package.parse_strict_json(bootstrap_raw, "authority-bootstrap")
        package_public.verify(
            files["authority-bootstrap.v2.sig"],
            _framed(AUTHORITY_SIGNATURE_DOMAIN, bootstrap_raw),
        )
    except (InvalidSignature, package.PackageFailure):
        fail("authority-bootstrap-invalid")
    if bootstrap != {
        "created_at_epoch": bootstrap.get("created_at_epoch"),
        "package_authority_key_id": package_id,
        "package_authority_private_sha256": CANONICAL_PACKAGE_PRIVATE_SHA256,
        "package_authority_public_sha256": _digest(package_anchor),
        "package_authority_source": os.fspath(CANONICAL_AUTHORITY_ROOT),
        "receipt_authority_key_id": receipt_id,
        "receipt_authority_public_sha256": _digest(files["receipt-authority-v2.pem"]),
        "schema": AUTHORITY_SCHEMA,
        "version": 2,
    } or type(bootstrap.get("created_at_epoch")) is not int or bootstrap["created_at_epoch"] < 1:
        fail("authority-bootstrap-binding-invalid")
    return (
        package_anchor,
        package_private,
        package_public,
        receipt_private,
        receipt_public,
        package_id,
        receipt_id,
    )


def _runner_metadata(path: Path, *, directory: bool, mode: int) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        fail("runner-reservation-path-unavailable")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not expected(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_uid != 1000
        or metadata.st_gid != 1000
        or path.resolve() != path
        or (not directory and metadata.st_nlink != 1)
    ):
        fail("runner-reservation-path-metadata-invalid")
    return metadata


def _acquire_runner_reservation_lock() -> int:
    _runner_metadata(RUNNER_RESERVATION_PARENT, directory=True, mode=0o700)
    try:
        descriptor = os.open(
            RUNNER_RESERVATION_LOCK,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
    except OSError:
        fail("runner-reservation-lock-unavailable")
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != 1000
            or metadata.st_gid != 1000
            or metadata.st_nlink != 1
        ):
            fail("runner-reservation-lock-invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail("runner-reservation-busy")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_runner_reservation_directory(directory: Path) -> bytes:
    _runner_metadata(directory, directory=True, mode=0o700)
    try:
        names = {item.name for item in directory.iterdir()}
    except OSError:
        fail("runner-reservation-read-failed")
    if names != {RUNNER_RESERVATION_NAME}:
        fail("runner-reservation-file-set-invalid")
    path = directory / RUNNER_RESERVATION_NAME
    before = _runner_metadata(path, directory=False, mode=0o600)
    if not 1 <= before.st_size <= package.MAX_JSON_BYTES:
        fail("runner-reservation-size-invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        fail("runner-reservation-read-failed")
    try:
        opened = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while total <= package.MAX_JSON_BYTES:
            chunk = os.read(
                descriptor, min(65_536, package.MAX_JSON_BYTES + 1 - total)
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(raw) != before.st_size
        or identity
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        fail("runner-reservation-mutated")
    return raw


def _checkout_git(checkout: Path, *arguments: str) -> bytes:
    try:
        git_metadata = os.stat("/usr/bin/git", follow_symlinks=False)
    except OSError:
        fail("runner-reservation-git-invalid")
    if (
        os.path.realpath("/usr/bin/git") != "/usr/bin/git"
        or not stat.S_ISREG(git_metadata.st_mode)
        or stat.S_IMODE(git_metadata.st_mode) != 0o755
        or git_metadata.st_uid != 0
        or git_metadata.st_gid != 0
    ):
        fail("runner-reservation-git-invalid")
    return _run_observation_command(
        [
            "/usr/bin/git",
            "-C",
            os.fspath(checkout),
            *arguments,
        ]
    )


def _runner_checkout_identity(
    checkout: Path, metadata: os.stat_result, workflow_sha: str
) -> str:
    return package.sha256(
        package.canonical_json(
            {
                "device": metadata.st_dev,
                "gid": 1000,
                "inode": metadata.st_ino,
                "mode": "0700",
                "path": os.fspath(checkout),
                "uid": 1000,
                "workflow_sha": workflow_sha,
            }
        )
    )


def _validate_runner_checkout(payload: dict[str, Any]) -> None:
    workflow_sha = payload["workflow_sha"]
    expected_path = RUNNER_RELEASE_CHECKOUT_ROOT / workflow_sha
    if payload.get("source_checkout_path") != os.fspath(expected_path):
        fail("runner-reservation-checkout-path-invalid")
    _runner_metadata(RUNNER_RELEASE_CHECKOUT_ROOT, directory=True, mode=0o700)
    metadata = _runner_metadata(expected_path, directory=True, mode=0o700)
    git_directory = expected_path / ".git"
    git_metadata = _runner_metadata(git_directory, directory=True, mode=0o700)
    if git_metadata.st_nlink < 2:
        fail("runner-reservation-checkout-git-invalid")
    try:
        top = _checkout_git(expected_path, "rev-parse", "--show-toplevel").decode(
            "ascii", "strict"
        ).strip()
        head = _checkout_git(expected_path, "rev-parse", "--verify", "HEAD").decode(
            "ascii", "strict"
        ).strip()
        branch = _checkout_git(expected_path, "rev-parse", "--abbrev-ref", "HEAD").decode(
            "ascii", "strict"
        ).strip()
        origin = _checkout_git(expected_path, "remote", "get-url", "origin").decode(
            "ascii", "strict"
        ).strip()
        inside = _checkout_git(
            expected_path, "rev-parse", "--is-inside-work-tree"
        ).decode("ascii", "strict").strip()
        shallow = _checkout_git(
            expected_path, "rev-parse", "--is-shallow-repository"
        ).decode("ascii", "strict").strip()
        configuration = _checkout_git(
            expected_path, "config", "--includes", "--list", "--name-only"
        ).decode("ascii", "strict").splitlines()
        remote_head = _checkout_git(
            expected_path, "rev-parse", "--verify", "refs/remotes/origin/main"
        ).decode("ascii", "strict").strip()
        dirty = _checkout_git(
            expected_path, "status", "--porcelain=v1", "--untracked-files=all"
        )
        tree = _checkout_git(expected_path, "ls-tree", "-r", "--full-tree", "-z", "HEAD")
    except UnicodeError:
        fail("runner-reservation-checkout-invalid")
    dangerous_configuration = {key.lower() for key in configuration if key}
    if any(
        key.startswith("url.")
        and (key.endswith(".insteadof") or key.endswith(".pushinsteadof"))
        or key.startswith("credential.")
        or key.startswith("include.")
        or key.startswith("includeif.")
        or key.startswith("filter.")
        or key.startswith("submodule.")
        or key.startswith("alias.")
        or key.startswith("protocol.")
        or key.startswith("merge.")
        and key.endswith(".driver")
        or key.startswith("diff.")
        and (key.endswith(".command") or key.endswith(".textconv"))
        or "proxy" in key
        or key
        in {
            "core.askpass",
            "core.fsmonitor",
            "core.hookspath",
            "core.sshcommand",
            "http.extraheader",
            "remote.origin.pushurl",
            "remote.origin.uploadpack",
        }
        for key in dangerous_configuration
    ):
        fail("runner-reservation-git-configuration-invalid")
    if not tree or not tree.endswith(b"\0"):
        fail("runner-reservation-checkout-tree-invalid")
    for record in tree.removesuffix(b"\0").split(b"\0"):
        if not (record.startswith(b"100644 ") or record.startswith(b"100755 ")):
            fail("runner-reservation-checkout-tree-mode-invalid")
    if (
        top != os.fspath(expected_path)
        or head != workflow_sha
        or remote_head != workflow_sha
        or branch != "HEAD"
        or inside != "true"
        or shallow != "false"
        or origin != "https://github.com/ArchonMegalon/propertyquarry.git"
        or dirty
        or payload.get("source_tree_sha256") != package.sha256(tree)
        or payload.get("source_checkout_identity_sha256")
        != _runner_checkout_identity(expected_path, metadata, workflow_sha)
    ):
        fail("runner-reservation-checkout-binding-invalid")
    try:
        _checkout_git(
            expected_path,
            "cat-file",
            "-e",
            f"{workflow_sha}:{SMOKE_WORKFLOW_PATH}",
        )
        rechecked_head = _checkout_git(
            expected_path, "rev-parse", "--verify", "HEAD"
        ).decode("ascii", "strict").strip()
    except (MaterializeFailure, UnicodeError):
        fail("runner-reservation-checkout-workflow-invalid")
    if (
        rechecked_head != workflow_sha
        or _checkout_git(
            expected_path, "status", "--porcelain=v1", "--untracked-files=all"
        )
        or _runner_checkout_identity(expected_path, expected_path.lstat(), workflow_sha)
        != payload.get("source_checkout_identity_sha256")
    ):
        fail("runner-reservation-checkout-mutated")


def _validate_runner_reservation(
    raw: bytes,
    *,
    receipt_public: Ed25519PublicKey,
    receipt_id: str,
    workflow_sha: str,
    current: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        wrapper = package.parse_strict_json(raw, "runner-reservation")
    except package.PackageFailure:
        fail("runner-reservation-wire-invalid")
    payload = wrapper.get("payload") if isinstance(wrapper, dict) else None
    signature_text = wrapper.get("signature") if isinstance(wrapper, dict) else None
    if (
        not isinstance(wrapper, dict)
        or set(wrapper) != {"payload", "signature", "signature_key_id"}
        or type(payload) is not dict
        or type(signature_text) is not str
        or wrapper.get("signature_key_id") != receipt_id
    ):
        fail("runner-reservation-wrapper-invalid")
    try:
        signature = base64.b64decode(
            signature_text.encode("ascii") + b"=" * (-len(signature_text) % 4),
            altchars=b"-_",
            validate=True,
        )
        canonical = package.canonical_json(payload)
        receipt_public.verify(
            signature, _framed(RUNNER_RESERVATION_SIGNATURE_DOMAIN, canonical)
        )
    except (UnicodeEncodeError, ValueError, InvalidSignature):
        fail("runner-reservation-signature-invalid")
    if (
        len(signature) != 64
        or signature_text
        != base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    ):
        fail("runner-reservation-signature-encoding-invalid")
    expected_keys = {
        "authority_profile",
        "created_at_epoch",
        "environment",
        "expires_at_epoch",
        "receipt_authority_key_id",
        "release_job",
        "repository",
        "repository_id",
        "repository_owner_id",
        "reservation_nonce",
        "runner_label",
        "runner_label_nonce",
        "schema",
        "source_checkout_identity_sha256",
        "source_checkout_path",
        "source_tree_sha256",
        "version",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
    }
    nonce = payload.get("reservation_nonce")
    label = payload.get("runner_label")
    created = payload.get("created_at_epoch")
    expires = payload.get("expires_at_epoch")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != RUNNER_RESERVATION_SCHEMA
        or payload.get("version") != 2
        or payload.get("authority_profile") != package.PROFILE
        or payload.get("environment") != package.ENVIRONMENT
        or payload.get("repository") != package.REPOSITORY
        or payload.get("repository_id") != package.REPOSITORY_ID
        or payload.get("repository_owner_id") != package.REPOSITORY_OWNER_ID
        or payload.get("workflow_path") != ".github/workflows/smoke-runtime.yml"
        or payload.get("workflow_ref") != package.WORKFLOW_REF
        or payload.get("workflow_sha") != workflow_sha
        or payload.get("release_job") != package.RELEASE_JOB
        or payload.get("receipt_authority_key_id") != receipt_id
        or type(nonce) is not str
        or not HEX64.fullmatch(nonce)
        or type(label) is not str
        or re.fullmatch(r"pqrelease-[0-9a-f]{32}", label) is None
        or payload.get("runner_label_nonce") != label.removeprefix("pqrelease-")
        or type(created) is not int
        or isinstance(created, bool)
        or type(expires) is not int
        or isinstance(expires, bool)
        or created < 1
        or expires - created != RUNNER_RESERVATION_TTL_SECONDS
        or current < created
        or current > expires
        or not isinstance(payload.get("source_checkout_identity_sha256"), str)
        or not package.SHA256_PATTERN.fullmatch(
            payload["source_checkout_identity_sha256"]
        )
        or not isinstance(payload.get("source_tree_sha256"), str)
        or not package.SHA256_PATTERN.fullmatch(payload["source_tree_sha256"])
    ):
        fail("runner-reservation-payload-invalid")
    derived = hashlib.sha256(
        RUNNER_LABEL_DERIVATION_DOMAIN + bytes.fromhex(nonce)
    ).hexdigest()[:32]
    if label != "pqrelease-" + derived:
        fail("runner-reservation-label-binding-invalid")
    _validate_runner_checkout(payload)
    binding = {
        "reservation_sha256": package.sha256(raw),
        "runner_label": label,
        "reservation_nonce": nonce,
        "source_checkout_identity_sha256": payload[
            "source_checkout_identity_sha256"
        ],
        "source_checkout_path": payload["source_checkout_path"],
        "source_tree_sha256": payload["source_tree_sha256"],
    }
    return payload, binding


def _runner_terminal_path(raw: bytes) -> Path:
    return RUNNER_RESERVATION_TERMINAL_ROOT / (
        package.sha256(raw).removeprefix("sha256:") + ".bound.v2"
    )


def _runner_claim_path(raw: bytes) -> Path:
    return RUNNER_RESERVATION_TERMINAL_ROOT / (
        package.sha256(raw).removeprefix("sha256:") + ".claim.v2"
    )


def _ensure_runner_terminal_root() -> None:
    if not RUNNER_RESERVATION_TERMINAL_ROOT.exists():
        try:
            os.mkdir(RUNNER_RESERVATION_TERMINAL_ROOT, 0o700)
            os.chmod(RUNNER_RESERVATION_TERMINAL_ROOT, 0o700)
            _sync_directory(RUNNER_RESERVATION_PARENT)
        except OSError:
            fail("runner-reservation-terminal-create-failed")
    _runner_metadata(
        RUNNER_RESERVATION_TERMINAL_ROOT, directory=True, mode=0o700
    )


def _read_runner_terminal_file(path: Path) -> bytes:
    before = _runner_metadata(path, directory=False, mode=0o600)
    if not 1 <= before.st_size <= package.MAX_JSON_BYTES:
        fail("runner-terminal-file-size-invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        fail("runner-terminal-file-read-failed")
    try:
        opened = os.fstat(descriptor)
        raw = b""
        while len(raw) <= package.MAX_JSON_BYTES:
            chunk = os.read(
                descriptor,
                min(65_536, package.MAX_JSON_BYTES + 1 - len(raw)),
            )
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (
        len(raw) != before.st_size
        or identity
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        or identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        fail("runner-terminal-file-mutated")
    return raw


def _signed_runner_record(
    payload: dict[str, Any],
    *,
    private: Ed25519PrivateKey,
    key_id: str,
    domain: bytes,
) -> bytes:
    canonical = package.canonical_json(payload)
    signature = private.sign(_framed(domain, canonical))
    return package.canonical_json(
        {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature)
            .rstrip(b"=")
            .decode("ascii"),
            "signature_key_id": key_id,
        }
    )


def _validate_signed_runner_record(
    raw: bytes,
    *,
    public: Ed25519PublicKey,
    key_id: str,
    domain: bytes,
    label: str,
) -> dict[str, Any]:
    try:
        wrapper = package.parse_strict_json(raw, label)
    except package.PackageFailure:
        fail(f"{label}-wire-invalid")
    payload = wrapper.get("payload") if isinstance(wrapper, dict) else None
    signature_text = wrapper.get("signature") if isinstance(wrapper, dict) else None
    if (
        not isinstance(wrapper, dict)
        or set(wrapper) != {"payload", "signature", "signature_key_id"}
        or type(payload) is not dict
        or type(signature_text) is not str
        or wrapper.get("signature_key_id") != key_id
    ):
        fail(f"{label}-wrapper-invalid")
    try:
        signature = base64.b64decode(
            signature_text.encode("ascii") + b"=" * (-len(signature_text) % 4),
            altchars=b"-_",
            validate=True,
        )
        canonical = package.canonical_json(payload)
        public.verify(signature, _framed(domain, canonical))
    except (UnicodeEncodeError, ValueError, InvalidSignature):
        fail(f"{label}-signature-invalid")
    if (
        len(signature) != 64
        or signature_text
        != base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        or package.canonical_json(wrapper) != raw
    ):
        fail(f"{label}-encoding-invalid")
    return payload


def _runner_prerequisite_paths(reservation_raw: bytes) -> tuple[Path, Path]:
    identity = package.sha256(reservation_raw).removeprefix("sha256:")
    return (
        RUNNER_PREREQUISITE_APPROVAL_ROOT
        / f"{identity}.intent.v2.json",
        RUNNER_PREREQUISITE_APPROVAL_ROOT
        / f"{identity}.approved.v2.json",
    )


def _read_runner_prerequisite_records(
    reservation_raw: bytes,
) -> tuple[bytes, bytes]:
    _runner_metadata(
        RUNNER_PREREQUISITE_APPROVAL_ROOT, directory=True, mode=0o700
    )
    intent_path, approval_path = _runner_prerequisite_paths(reservation_raw)
    if not intent_path.exists() or not approval_path.exists():
        fail("runner-prerequisite-record-missing")
    return (
        _read_runner_terminal_file(intent_path),
        _read_runner_terminal_file(approval_path),
    )


def _validate_runner_prerequisite_records(
    *,
    intent_raw: bytes,
    approval_raw: bytes,
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    receipt_public: Ed25519PublicKey,
    receipt_id: str,
    current: int,
) -> dict[str, Any]:
    intent = _validate_signed_runner_record(
        intent_raw,
        public=receipt_public,
        key_id=receipt_id,
        domain=RUNNER_PREREQUISITE_INTENT_SIGNATURE_DOMAIN,
        label="runner-prerequisite-intent",
    )
    intent_keys = {
        "authority_profile",
        "comment",
        "discovered_at_epoch",
        "environment_id",
        "environment_name",
        "initial_jobs_sha256",
        "initial_pending_deployments_sha256",
        "initial_runs_index_sha256",
        "prerequisite_job_id",
        "prerequisite_job_name",
        "receipt_authority_key_id",
        "release_job",
        "repository",
        "repository_id",
        "repository_owner_id",
        "reservation_expires_at_epoch",
        "reservation_sha256",
        "run_attempt",
        "run_id",
        "runner_label",
        "schema",
        "version",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
    }
    reservation_sha256 = package.sha256(reservation_raw)
    discovered = intent.get("discovered_at_epoch")
    if (
        set(intent) != intent_keys
        or intent.get("schema") != RUNNER_PREREQUISITE_INTENT_SCHEMA
        or intent.get("version") != 2
        or intent.get("authority_profile") != package.PROFILE
        or intent.get("receipt_authority_key_id") != receipt_id
        or intent.get("repository") != package.REPOSITORY
        or intent.get("repository_id") != package.REPOSITORY_ID
        or intent.get("repository_owner_id") != package.REPOSITORY_OWNER_ID
        or intent.get("workflow_path") != SMOKE_WORKFLOW_PATH
        or intent.get("workflow_ref") != package.WORKFLOW_REF
        or intent.get("workflow_sha") != reservation_payload["workflow_sha"]
        or intent.get("release_job") != package.RELEASE_JOB
        or intent.get("environment_name") != RUNNER_PREREQUISITE_ENVIRONMENT
        or not _numeric_id(intent.get("environment_id"))
        or intent.get("prerequisite_job_name") != RUNNER_PREREQUISITE_JOB
        or not _numeric_id(intent.get("prerequisite_job_id"))
        or intent.get("reservation_sha256") != reservation_sha256
        or intent.get("reservation_expires_at_epoch")
        != reservation_payload["expires_at_epoch"]
        or intent.get("runner_label") != reservation_payload["runner_label"]
        or not _numeric_id(intent.get("run_id"))
        or type(intent.get("run_attempt")) is not int
        or isinstance(intent.get("run_attempt"), bool)
        or not 1 <= intent["run_attempt"] < 1 << 31
        or type(discovered) is not int
        or isinstance(discovered, bool)
        or not reservation_payload["created_at_epoch"]
        <= discovered
        <= reservation_payload["expires_at_epoch"]
        or intent.get("comment")
        != "PropertyQuarry governed prerequisite approval " + reservation_sha256
        or any(
            type(intent.get(field)) is not str
            or package.SHA256_PATTERN.fullmatch(intent[field]) is None
            for field in (
                "initial_jobs_sha256",
                "initial_pending_deployments_sha256",
                "initial_runs_index_sha256",
            )
        )
    ):
        fail("runner-prerequisite-intent-binding-invalid")

    approval = _validate_signed_runner_record(
        approval_raw,
        public=receipt_public,
        key_id=receipt_id,
        domain=RUNNER_PREREQUISITE_APPROVAL_SIGNATURE_DOMAIN,
        label="runner-prerequisite-approval",
    )
    approval_keys = {
        "approval_api_disposition",
        "approval_response_sha256",
        "approved_at_epoch",
        "completed_jobs_sha256",
        "environment_id",
        "environment_name",
        "intent_sha256",
        "post_pending_deployments_sha256",
        "prerequisite_conclusion",
        "prerequisite_job_id",
        "prerequisite_job_name",
        "receipt_authority_key_id",
        "release_job",
        "repository",
        "repository_id",
        "repository_owner_id",
        "reservation_expires_at_epoch",
        "reservation_sha256",
        "review_history_sha256",
        "run_attempt",
        "run_id",
        "runner_label",
        "schema",
        "version",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
    }
    approved = approval.get("approved_at_epoch")
    disposition = approval.get("approval_api_disposition")
    response_digest = approval.get("approval_response_sha256")
    if (
        set(approval) != approval_keys
        or approval.get("schema") != RUNNER_PREREQUISITE_APPROVAL_SCHEMA
        or approval.get("version") != 2
        or approval.get("intent_sha256") != package.sha256(intent_raw)
        or approval.get("reservation_sha256") != reservation_sha256
        or approval.get("runner_label") != intent["runner_label"]
        or approval.get("run_id") != intent["run_id"]
        or approval.get("run_attempt") != intent["run_attempt"]
        or approval.get("prerequisite_job_id")
        != intent["prerequisite_job_id"]
        or approval.get("prerequisite_job_name") != RUNNER_PREREQUISITE_JOB
        or approval.get("prerequisite_conclusion") != "success"
        or approval.get("environment_id") != intent["environment_id"]
        or approval.get("environment_name") != RUNNER_PREREQUISITE_ENVIRONMENT
        or approval.get("receipt_authority_key_id") != receipt_id
        or approval.get("repository") != package.REPOSITORY
        or approval.get("repository_id") != package.REPOSITORY_ID
        or approval.get("repository_owner_id") != package.REPOSITORY_OWNER_ID
        or approval.get("workflow_path") != SMOKE_WORKFLOW_PATH
        or approval.get("workflow_ref") != package.WORKFLOW_REF
        or approval.get("workflow_sha") != reservation_payload["workflow_sha"]
        or approval.get("release_job") != package.RELEASE_JOB
        or approval.get("reservation_expires_at_epoch")
        != reservation_payload["expires_at_epoch"]
        or disposition not in {"approved", "post-approved-recovered"}
        or (
            disposition == "approved"
            and (
                type(response_digest) is not str
                or package.SHA256_PATTERN.fullmatch(response_digest) is None
            )
        )
        or (disposition == "post-approved-recovered" and response_digest is not None)
        or any(
            type(approval.get(field)) is not str
            or package.SHA256_PATTERN.fullmatch(approval[field]) is None
            for field in (
                "completed_jobs_sha256",
                "post_pending_deployments_sha256",
                "review_history_sha256",
            )
        )
        or type(approved) is not int
        or isinstance(approved, bool)
        or not discovered <= approved <= reservation_payload["expires_at_epoch"]
        or approved > current
    ):
        fail("runner-prerequisite-approval-binding-invalid")
    return {
        "approval_payload": approval,
        "runner_prerequisite_approval_payload_sha256": package.sha256(
            package.canonical_json(approval)
        ),
        "runner_prerequisite_approval_sha256": package.sha256(approval_raw),
        "runner_prerequisite_intent_sha256": package.sha256(intent_raw),
        "runner_prerequisite_job_id": approval["prerequisite_job_id"],
        "run_attempt": approval["run_attempt"],
        "run_id": approval["run_id"],
        "runner_label": approval["runner_label"],
    }


def _runner_parent_identity(target: Path) -> str:
    _controlled_parent(target)
    metadata = target.parent.lstat()
    return package.sha256(
        package.canonical_json(
            {
                "device": metadata.st_dev,
                "gid": metadata.st_gid,
                "inode": metadata.st_ino,
                "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
                "path": os.fspath(target.parent),
                "uid": metadata.st_uid,
            }
        )
    )


def _runner_output_identity(target: Path) -> str:
    metadata = _runner_metadata(target, directory=True, mode=0o700)
    return package.sha256(
        package.canonical_json(
            {
                "device": metadata.st_dev,
                "gid": metadata.st_gid,
                "inode": metadata.st_ino,
                "mode": "0700",
                "path": os.fspath(target),
                "uid": metadata.st_uid,
            }
        )
    )


def _validate_runner_materialization_claim(
    raw: bytes, *, receipt_public: Ed25519PublicKey, receipt_id: str
) -> dict[str, Any]:
    payload = _validate_signed_runner_record(
        raw,
        public=receipt_public,
        key_id=receipt_id,
        domain=RUNNER_MATERIALIZATION_CLAIM_SIGNATURE_DOMAIN,
        label="runner-materialization-claim",
    )
    expected_keys = {
        "authority_profile",
        "claimed_at_epoch",
        "deployment_id",
        "environment",
        "expires_at_epoch",
        "materialization_parent_identity_sha256",
        "materialization_root",
        "receipt_authority_key_id",
        "release_evidence_sha256",
        "reservation_nonce",
        "reservation_sha256",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256",
        "runner_prerequisite_intent_sha256",
        "runner_prerequisite_job_id",
        "runner_label",
        "runtime_sha",
        "schema",
        "version",
        "workflow_sha",
    }
    claimed = payload.get("claimed_at_epoch")
    expires = payload.get("expires_at_epoch")
    root = payload.get("materialization_root")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != RUNNER_MATERIALIZATION_CLAIM_SCHEMA
        or payload.get("version") != 2
        or payload.get("authority_profile") != package.PROFILE
        or payload.get("environment") != package.ENVIRONMENT
        or payload.get("receipt_authority_key_id") != receipt_id
        or type(claimed) is not int
        or isinstance(claimed, bool)
        or type(expires) is not int
        or isinstance(expires, bool)
        or claimed < 1
        or expires <= claimed
        or expires - claimed > MAX_OBSERVATION_SECONDS
        or type(root) is not str
        or os.fspath(_absolute(root, "runner-claim-output-invalid")) != root
        or type(payload.get("deployment_id")) is not str
        or not HEX64.fullmatch(payload["deployment_id"])
        or type(payload.get("reservation_nonce")) is not str
        or not HEX64.fullmatch(payload["reservation_nonce"])
        or type(payload.get("runner_label")) is not str
        or re.fullmatch(r"pqrelease-[0-9a-f]{32}", payload["runner_label"])
        is None
        or type(payload.get("runtime_sha")) is not str
        or not SHA1.fullmatch(payload["runtime_sha"])
        or type(payload.get("workflow_sha")) is not str
        or not SHA1.fullmatch(payload["workflow_sha"])
        or any(
            type(payload.get(key)) is not str
            or package.SHA256_PATTERN.fullmatch(payload[key]) is None
            for key in (
                "materialization_parent_identity_sha256",
                "release_evidence_sha256",
                "reservation_sha256",
                "runner_prerequisite_approval_payload_sha256",
                "runner_prerequisite_approval_sha256",
                "runner_prerequisite_intent_sha256",
            )
        )
        or not _numeric_id(payload.get("runner_prerequisite_job_id"))
    ):
        fail("runner-materialization-claim-binding-invalid")
    return payload


def _validate_runner_materialization_binding(
    raw: bytes, *, receipt_public: Ed25519PublicKey, receipt_id: str
) -> dict[str, Any]:
    payload = _validate_signed_runner_record(
        raw,
        public=receipt_public,
        key_id=receipt_id,
        domain=RUNNER_MATERIALIZATION_BINDING_SIGNATURE_DOMAIN,
        label="runner-materialization-binding",
    )
    expected_keys = {
        "authority_profile",
        "bound_at_epoch",
        "claim_sha256",
        "config_sha256",
        "config_signature_sha256",
        "deployment_id",
        "environment",
        "job_id",
        "materialization_receipt_sha256",
        "materialization_receipt_signature_sha256",
        "materialization_root",
        "materialization_root_identity_sha256",
        "plan_sha256",
        "receipt_authority_key_id",
        "reservation_sha256",
        "run_attempt",
        "run_id",
        "runner_label",
        "runner_launch_ticket_sha256",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256",
        "runner_prerequisite_intent_sha256",
        "runner_prerequisite_job_id",
        "runtime_sha",
        "schema",
        "version",
        "workflow_sha",
    }
    bound = payload.get("bound_at_epoch")
    attempt = payload.get("run_attempt")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != RUNNER_MATERIALIZATION_BINDING_SCHEMA
        or payload.get("version") != 2
        or payload.get("authority_profile") != package.PROFILE
        or payload.get("environment") != package.ENVIRONMENT
        or payload.get("receipt_authority_key_id") != receipt_id
        or type(bound) is not int
        or isinstance(bound, bool)
        or bound < 1
        or type(attempt) is not int
        or isinstance(attempt, bool)
        or not 1 <= attempt < 1 << 31
        or not _numeric_id(payload.get("run_id"))
        or not _numeric_id(payload.get("job_id"))
        or type(payload.get("deployment_id")) is not str
        or not HEX64.fullmatch(payload["deployment_id"])
        or type(payload.get("runner_label")) is not str
        or re.fullmatch(r"pqrelease-[0-9a-f]{32}", payload["runner_label"])
        is None
        or type(payload.get("runtime_sha")) is not str
        or not SHA1.fullmatch(payload["runtime_sha"])
        or type(payload.get("workflow_sha")) is not str
        or not SHA1.fullmatch(payload["workflow_sha"])
        or type(payload.get("materialization_root")) is not str
        or os.fspath(
            _absolute(payload["materialization_root"], "runner-binding-output-invalid")
        )
        != payload["materialization_root"]
        or any(
            type(payload.get(key)) is not str
            or package.SHA256_PATTERN.fullmatch(payload[key]) is None
            for key in (
                "claim_sha256",
                "config_sha256",
                "config_signature_sha256",
                "materialization_receipt_sha256",
                "materialization_receipt_signature_sha256",
                "materialization_root_identity_sha256",
                "plan_sha256",
                "reservation_sha256",
                "runner_launch_ticket_sha256",
                "runner_prerequisite_approval_payload_sha256",
                "runner_prerequisite_approval_sha256",
                "runner_prerequisite_intent_sha256",
            )
        )
        or not _numeric_id(payload.get("runner_prerequisite_job_id"))
    ):
        fail("runner-materialization-binding-invalid")
    return payload


def _publish_runner_terminal_file(path: Path, raw: bytes) -> str:
    pending = path.with_name(path.name + ".pending")
    if path.exists():
        if _read_runner_terminal_file(path) != raw:
            fail("runner-terminal-record-conflict")
        if pending.exists():
            if _read_runner_terminal_file(pending) != raw:
                fail("runner-terminal-pending-conflict")
            pending.unlink()
            _sync_directory(RUNNER_RESERVATION_TERMINAL_ROOT)
        return "already-published"
    if pending.exists():
        if _read_runner_terminal_file(pending) != raw:
            fail("runner-terminal-pending-conflict")
    else:
        descriptor = os.open(
            pending,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(raw):
                count = os.write(descriptor, raw[written:])
                if count < 1:
                    fail("runner-terminal-write-failed")
                written += count
            os.fsync(descriptor)
            _materialization_checkpoint(
                f"runner-terminal:{path.name}:after-pending-file-fsync"
            )
        finally:
            os.close(descriptor)
        _sync_directory(RUNNER_RESERVATION_TERMINAL_ROOT)
        _materialization_checkpoint(
            f"runner-terminal:{path.name}:after-pending-parent-fsync"
        )
    _materialization_checkpoint(f"runner-terminal:{path.name}:before-promote")
    _rename_noreplace(pending, path)
    _sync_directory(RUNNER_RESERVATION_TERMINAL_ROOT)
    _materialization_checkpoint(f"runner-terminal:{path.name}:after-promote")
    return "published"


def _ensure_runner_materialization_claim(
    *,
    reservation_raw: bytes,
    reservation_payload: dict[str, Any],
    reservation_binding: dict[str, Any],
    prerequisite_binding: dict[str, Any],
    output: str,
    release_evidence: dict[str, str],
    requested_started: int,
    requested_deployment_id: str,
    receipt_private: Ed25519PrivateKey,
    receipt_public: Ed25519PublicKey,
    receipt_id: str,
    enforce_deadline: bool,
) -> tuple[dict[str, Any], bytes, str]:
    _ensure_runner_terminal_root()
    target = _absolute(output, "output-path-invalid")
    fixed = {
        "authority_profile": package.PROFILE,
        "environment": package.ENVIRONMENT,
        "materialization_parent_identity_sha256": _runner_parent_identity(target),
        "materialization_root": output,
        "receipt_authority_key_id": receipt_id,
        "release_evidence_sha256": package.sha256(
            package.canonical_json(release_evidence)
        ),
        "reservation_nonce": reservation_payload["reservation_nonce"],
        "reservation_sha256": reservation_binding["reservation_sha256"],
        "runner_prerequisite_approval_payload_sha256": prerequisite_binding[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": prerequisite_binding[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": prerequisite_binding[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": prerequisite_binding[
            "runner_prerequisite_job_id"
        ],
        "runner_label": reservation_binding["runner_label"],
        "runtime_sha": release_evidence["runtime_sha"],
        "schema": RUNNER_MATERIALIZATION_CLAIM_SCHEMA,
        "version": 2,
        "workflow_sha": release_evidence["workflow_sha"],
    }
    claim_path = _runner_claim_path(reservation_raw)
    pending_claim_path = claim_path.with_name(claim_path.name + ".pending")
    if claim_path.exists() or pending_claim_path.exists():
        claim_raw = _read_runner_terminal_file(
            claim_path if claim_path.exists() else pending_claim_path
        )
        payload = _validate_runner_materialization_claim(
            claim_raw, receipt_public=receipt_public, receipt_id=receipt_id
        )
        for key, value in fixed.items():
            if payload.get(key) != value:
                fail("runner-materialization-claim-conflict")
        reservation_created = reservation_payload.get("created_at_epoch")
        if (
            type(reservation_created) is not int
            or isinstance(reservation_created, bool)
            or payload["claimed_at_epoch"] < reservation_created
            or payload["claimed_at_epoch"] > reservation_payload["expires_at_epoch"]
            or payload["expires_at_epoch"]
            != min(
                payload["claimed_at_epoch"] + MAX_OBSERVATION_SECONDS,
                reservation_payload["expires_at_epoch"],
            )
        ):
            fail("runner-materialization-claim-window-invalid")
        if (
            enforce_deadline
            and (
                requested_started < payload["claimed_at_epoch"]
                or requested_started > payload["expires_at_epoch"]
            )
        ):
            fail("runner-materialization-claim-expired")
        _publish_runner_terminal_file(claim_path, claim_raw)
        return payload, claim_raw, "already-claimed"
    if not RUNNER_RESERVATION_ROOT.exists():
        fail("runner-materialization-claim-active-missing")
    if _read_runner_reservation_directory(RUNNER_RESERVATION_ROOT) != reservation_raw:
        fail("runner-materialization-claim-active-conflict")
    reservation_created = reservation_payload.get("created_at_epoch")
    if (
        type(reservation_created) is not int
        or isinstance(reservation_created, bool)
        or requested_started < reservation_created
    ):
        fail("runner-materialization-claim-window-invalid")
    expires = min(
        requested_started + MAX_OBSERVATION_SECONDS,
        reservation_payload["expires_at_epoch"],
    )
    if expires <= requested_started:
        fail("runner-materialization-claim-window-invalid")
    payload = {
        **fixed,
        "claimed_at_epoch": requested_started,
        "deployment_id": requested_deployment_id,
        "expires_at_epoch": expires,
    }
    claim_raw = _signed_runner_record(
        payload,
        private=receipt_private,
        key_id=receipt_id,
        domain=RUNNER_MATERIALIZATION_CLAIM_SIGNATURE_DOMAIN,
    )
    disposition = _publish_runner_terminal_file(claim_path, claim_raw)
    return payload, claim_raw, (
        "claimed" if disposition == "published" else "already-claimed"
    )


def _remove_converged_active_reservation(raw: bytes) -> bool:
    if not RUNNER_RESERVATION_ROOT.exists():
        return False
    _runner_metadata(RUNNER_RESERVATION_ROOT, directory=True, mode=0o700)
    try:
        names = {item.name for item in RUNNER_RESERVATION_ROOT.iterdir()}
    except OSError:
        fail("runner-reservation-active-read-failed")
    if names == {RUNNER_RESERVATION_NAME}:
        if _read_runner_reservation_directory(RUNNER_RESERVATION_ROOT) != raw:
            fail("runner-reservation-active-bound-conflict")
        (RUNNER_RESERVATION_ROOT / RUNNER_RESERVATION_NAME).unlink()
        _materialization_checkpoint("runner-reservation:after-active-file-unlink")
        _sync_directory(RUNNER_RESERVATION_ROOT)
        _materialization_checkpoint("runner-reservation:after-active-directory-fsync")
    elif names:
        fail("runner-reservation-active-bound-conflict")
    try:
        RUNNER_RESERVATION_ROOT.rmdir()
        _sync_directory(RUNNER_RESERVATION_PARENT)
        _materialization_checkpoint("runner-reservation:after-active-directory-remove")
    except OSError:
        fail("runner-reservation-active-convergence-failed")
    return True


def _consume_runner_reservation(
    raw: bytes,
    binding_raw: bytes,
    *,
    receipt_public: Ed25519PublicKey,
    receipt_id: str,
) -> str:
    _ensure_runner_terminal_root()
    terminal = _runner_terminal_path(raw)
    binding = _validate_runner_materialization_binding(
        binding_raw, receipt_public=receipt_public, receipt_id=receipt_id
    )
    if binding["reservation_sha256"] != package.sha256(raw):
        fail("runner-materialization-binding-reservation-invalid")
    disposition = _publish_runner_terminal_file(terminal, binding_raw)
    active_removed = _remove_converged_active_reservation(raw)
    if disposition == "published":
        return "bound"
    if active_removed:
        return "active-bound-duplicate-converged"
    return "already-bound"


def _materialization_checkpoint(_name: str) -> None:
    """Test-only materialization crash boundary."""


def _step(identifier: str, effect: str, argv: list[str], timeout: int) -> dict[str, Any]:
    return {
        "argv": argv,
        "effect": effect,
        "expected_exit_code": 0,
        "id": identifier,
        "idempotent": True,
        "timeout_seconds": timeout,
    }


def _plan_steps(plan: dict[str, Any]) -> None:
    runtime_sha = plan["runtime_sha"]
    deployment_id = plan["deployment_id"]
    plan["preflight_steps"] = [
        _step(
            package.VERIFY_ISOLATION_INPUTS_STEP_ID,
            "read-only",
            package._expected_isolation_argv(
                plan, "verify-isolation-inputs", receipt=False, pre_purge=True
            ),
            600,
        )
    ]
    release = [
        _step(
            "predeploy-encrypted-backup",
            "mutation",
            [
                package.PREDEPLOY_BACKUP_HELPER_PATH,
                "create",
                "--runtime-sha",
                runtime_sha,
                "--deployment-id",
                deployment_id,
                "--envelope-sha",
                plan["envelope_sha"],
                "--web-image",
                plan["web_image"],
                "--render-image",
                plan["render_image"],
                "--database-image",
                plan["database_image"],
                "--receipt",
                f"{package.BACKUP_RECEIPT_ROOT}/{runtime_sha}/{deployment_id}/create.json",
                "--encryption-key",
                package.BACKUP_ENCRYPTION_KEY_PATH,
            ],
            9600,
        ),
        _step(
            "purge-propertyquarry-legacy-runtime-exposure",
            "mutation",
            package._expected_isolation_argv(
                plan, "purge-legacy-runtime-exposure", receipt=True, pre_purge=True
            ),
            600,
        ),
        _step(
            "retire-stale-propertyquarry-runtime",
            "mutation",
            package._expected_isolation_argv(
                plan,
                "retire-stale-propertyquarry-runtime",
                receipt=True,
                pre_purge=False,
            ),
            600,
        ),
    ]
    for identifier, operation, timeout, receipt_name in (
        ("provision-propertyquarry-database-roles", "provision-roles", 900, "provision-roles.json"),
        ("migrate-propertyquarry-schema", "migrate-schema", 1500, "migrate-schema.json"),
        ("harden-propertyquarry-runtime-acl", "harden-runtime-acl", 900, "harden-runtime-acl.json"),
        ("verify-propertyquarry-schema-readiness", "verify-schema-readiness", 600, "verify-schema-readiness.json"),
    ):
        release.append(
            _step(
                identifier,
                "mutation",
                [
                    package.DATABASE_CONTROL_HELPER_PATH,
                    operation,
                    "--runtime-sha",
                    runtime_sha,
                    "--deployment-id",
                    deployment_id,
                    "--web-image",
                    plan["web_image"],
                    "--database-image",
                    plan["database_image"],
                    "--receipt",
                    f"{package.DATABASE_RECEIPT_ROOT}/{runtime_sha}/{deployment_id}/{receipt_name}",
                ],
                timeout,
            )
        )
    release.append(
        _step(
            "deploy-propertyquarry-runtime",
            "mutation",
            package._expected_runtime_deploy_argv(plan),
            1800,
        )
    )
    plan["release_steps"] = release
    plan["verify_steps"] = [
        _step(
            package.TERMINAL_ISOLATION_VERIFY_STEP_ID,
            "verification",
            package._expected_isolation_argv(
                plan, "verify-runtime-isolation", receipt=True, pre_purge=False
            ),
            600,
        )
    ]
    plan["rollback_steps"] = [
        _step(
            package.ROLLBACK_ISOLATION_STEP_ID,
            "rollback",
            package._expected_isolation_argv(
                plan,
                "restore-legacy-runtime-exposure",
                receipt=True,
                pre_purge=True,
            ),
            600,
        )
    ]


def _build_documents(
    *,
    runtime_sha: str,
    workflow_sha: str,
    envelope_sha: str,
    web_image: str,
    render_image: str,
    cloudflared_image: str,
    deployment_id: str,
    generation: int,
    predecessor: str,
    runner_uid: int,
    runner_gid: int,
    started: int,
    observation: dict[str, Any],
    runner_binding: dict[str, Any],
    package_key_id: str,
    receipt_key_id: str,
) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    pre_inputs = observation.get("pre_purge_runtime_inputs")
    post_inputs = observation.get("runtime_inputs")
    retirement = observation.get("runtime_retirement")
    deploy = observation.get("runtime_deploy")
    substrate = observation.get("database_substrate")
    host_digest = observation.get("host_machine_id_digest")
    plan: dict[str, Any] = {
        "api_container_port": package.API_CONTAINER_PORT,
        "api_host_ip": package.API_HOST_IP,
        "api_host_port": package.API_HOST_PORT,
        "authority_profile": package.PROFILE,
        "backup_max_age_seconds": package.BACKUP_MAX_AGE_SECONDS,
        "cloudflared_image": cloudflared_image,
        "database_image": package.DATABASE_IMAGE,
        "database_substrate": substrate,
        "database_substrate_digest": package._canonical_digest(substrate),
        "deployment_id": deployment_id,
        "envelope_sha": envelope_sha,
        "executables": {
            package.PREDEPLOY_BACKUP_HELPER_PATH: package.PREDEPLOY_BACKUP_HELPER_SHA256,
            package.DATABASE_CONTROL_HELPER_PATH: package.DATABASE_CONTROL_HELPER_SHA256,
            package.RUNTIME_ISOLATION_HELPER_PATH: package.RUNTIME_ISOLATION_HELPER_SHA256,
            package.RUNTIME_DEPLOY_HELPER_PATH: package.RUNTIME_DEPLOY_HELPER_SHA256,
        },
        "github_identity_env_digest": pre_inputs[4]["sha256"],
        "github_identity_env_gid": pre_inputs[4]["gid"],
        "github_identity_env_mode": "0600",
        "github_identity_env_path": package.IDENTITY_ENV_PATH,
        "github_identity_env_uid": pre_inputs[4]["uid"],
        "host_machine_id_digest": host_digest,
        "post_purge_root_env_digest": post_inputs[0]["sha256"],
        "predecessor_runtime_sha": predecessor,
        "pre_purge_root_env_digest": pre_inputs[0]["sha256"],
        "pre_purge_runtime_inputs": pre_inputs,
        "preflight_steps": [],
        "project_name": package.PROJECT_NAME,
        "public_origin": package.PUBLIC_ORIGIN,
        "registration_email_env_digest": pre_inputs[5]["sha256"],
        "registration_email_env_gid": pre_inputs[5]["gid"],
        "registration_email_env_mode": "0600",
        "registration_email_env_path": package.REGISTRATION_EMAIL_ENV_PATH,
        "registration_email_env_uid": pre_inputs[5]["uid"],
        "release_generation": generation,
        "release_steps": [],
        "render_image": render_image,
        "repository": package.REPOSITORY,
        "runner_job_id": runner_binding["job_id"],
        "runner_label": runner_binding["runner_label"],
        "runner_prerequisite_approval_payload_sha256": runner_binding[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": runner_binding[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": runner_binding[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": runner_binding[
            "runner_prerequisite_job_id"
        ],
        "runner_reservation_sha256": runner_binding["reservation_sha256"],
        "runner_run_attempt": runner_binding["run_attempt"],
        "runner_run_id": runner_binding["run_id"],
        "rollback_steps": [],
        "runtime_deploy": deploy,
        "runtime_deploy_digest": package._canonical_digest(deploy),
        "runtime_inputs": post_inputs,
        "runtime_retirement": retirement,
        "runtime_retirement_digest": package._canonical_digest(retirement),
        "runtime_sha": runtime_sha,
        "scene_video_env_digest": pre_inputs[1]["sha256"],
        "scene_video_env_gid": pre_inputs[1]["gid"],
        "scene_video_env_mode": 384,
        "scene_video_env_path": package.SCENE_VIDEO_ENV_PATH,
        "scene_video_env_uid": pre_inputs[1]["uid"],
        "schema": package.PLAN_SCHEMA,
        "transaction_started_at_epoch": started,
        "verify_steps": [],
        "version": 2,
        "web_image": web_image,
        "workflow_sha": workflow_sha,
    }
    _plan_steps(plan)
    plan_raw = package.canonical_json(plan)
    config = {
        "allowed_runner_gid": runner_gid,
        "allowed_runner_uid": runner_uid,
        "api_container_port": package.API_CONTAINER_PORT,
        "api_host_ip": package.API_HOST_IP,
        "api_host_port": package.API_HOST_PORT,
        "authority_profile": package.PROFILE,
        "backup_max_age_seconds": package.BACKUP_MAX_AGE_SECONDS,
        "cloudflared_image": cloudflared_image,
        "database_image": package.DATABASE_IMAGE,
        "database_substrate": substrate,
        "database_substrate_digest": plan["database_substrate_digest"],
        "deployment_id": deployment_id,
        "envelope_sha": envelope_sha,
        "environment": package.ENVIRONMENT,
        "ephemeral_runner_label_prefix": "pqrelease-",
        "github_api_credential_path": (
            "/run/credentials/propertyquarry-release-single-host-v2.service/"
            "github-api-token"
        ),
        "github_identity_env_digest": plan["github_identity_env_digest"],
        "github_identity_env_gid": plan["github_identity_env_gid"],
        "github_identity_env_mode": "0600",
        "github_identity_env_path": package.IDENTITY_ENV_PATH,
        "github_identity_env_uid": plan["github_identity_env_uid"],
        "github_oidc_request_origin": "https://vstoken.actions.githubusercontent.com",
        "host_machine_id_digest": host_digest,
        "package_authority_key_id": package_key_id,
        "plan_digest": package.sha256(plan_raw),
        "post_purge_root_env_digest": plan["post_purge_root_env_digest"],
        "predecessor_runtime_sha": predecessor,
        "pre_purge_root_env_digest": plan["pre_purge_root_env_digest"],
        "pre_purge_runtime_inputs": pre_inputs,
        "preflight_ttl_seconds": 120,
        "project_name": package.PROJECT_NAME,
        "public_origin": package.PUBLIC_ORIGIN,
        "receipt_authority_key_id": receipt_key_id,
        "registration_email_env_digest": plan["registration_email_env_digest"],
        "registration_email_env_gid": plan["registration_email_env_gid"],
        "registration_email_env_mode": "0600",
        "registration_email_env_path": package.REGISTRATION_EMAIL_ENV_PATH,
        "registration_email_env_uid": plan["registration_email_env_uid"],
        "release_generation": generation,
        "release_job": package.RELEASE_JOB,
        "render_image": render_image,
        "repository": package.REPOSITORY,
        "repository_id": package.REPOSITORY_ID,
        "repository_owner_id": package.REPOSITORY_OWNER_ID,
        "runner_job_id": runner_binding["job_id"],
        "runner_label": runner_binding["runner_label"],
        "runner_prerequisite_approval_payload_sha256": runner_binding[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": runner_binding[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": runner_binding[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": runner_binding[
            "runner_prerequisite_job_id"
        ],
        "runner_reservation_sha256": runner_binding["reservation_sha256"],
        "runner_run_attempt": runner_binding["run_attempt"],
        "runner_run_id": runner_binding["run_id"],
        "runtime_deploy": deploy,
        "runtime_deploy_digest": plan["runtime_deploy_digest"],
        "runtime_inputs": post_inputs,
        "runtime_retirement": retirement,
        "runtime_retirement_digest": plan["runtime_retirement_digest"],
        "runtime_sha": runtime_sha,
        "scene_video_env_digest": plan["scene_video_env_digest"],
        "scene_video_env_gid": plan["scene_video_env_gid"],
        "scene_video_env_mode": 384,
        "scene_video_env_path": package.SCENE_VIDEO_ENV_PATH,
        "scene_video_env_uid": plan["scene_video_env_uid"],
        "schema": package.CONFIG_SCHEMA,
        "transaction_started_at_epoch": started,
        "version": 2,
        "web_image": web_image,
        "workflow_ref": package.WORKFLOW_REF,
        "workflow_sha": workflow_sha,
    }
    return config, plan, plan_raw


def _read_release_evidence_file(path_value: str | Path, maximum: int) -> bytes:
    path = Path(path_value)
    if not path.is_absolute() or path.resolve() != path:
        fail("release-evidence-path-invalid")
    try:
        metadata = path.lstat()
    except OSError:
        fail("release-evidence-unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) not in (0o400, 0o600, 0o644)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_nlink != 1
        or not 1 <= metadata.st_size <= maximum
    ):
        fail("release-evidence-metadata-invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        raw = b""
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(raw) != metadata.st_size
        or len(raw) > maximum
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    ):
        fail("release-evidence-mutated")
    return raw


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate-json-key")
        value[key] = item
    return value


def _strict_decode_json(raw: bytes, code: str) -> Any:
    try:
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        fail(code)


def _run_bounded_process(
    argv: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: int,
    stdout_limit: int,
    stderr_limit: int,
    code: str,
) -> subprocess.CompletedProcess[bytes]:
    if (
        not argv
        or not os.path.isabs(argv[0])
        or not 1 <= timeout <= 300
        or not 1 <= stdout_limit <= MAX_REMOTE_BUNDLE_BYTES
        or not 1 <= stderr_limit <= package.MAX_JSON_BYTES
    ):
        fail(code)
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except OSError:
        fail(code)
    if process.stdout is None or process.stderr is None:  # pragma: no cover
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        fail(code)
    output = {process.stdout: bytearray(), process.stderr: bytearray()}
    limits = {process.stdout: stdout_limit, process.stderr: stderr_limit}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    failed = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failed = True
                break
            events = selector.select(min(remaining, 1.0))
            if not events and process.poll() is not None:
                events = [(key, selectors.EVENT_READ) for key in selector.get_map().values()]
            for key, _mask in events:
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), 65_536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    continue
                output[stream].extend(chunk)
                if len(output[stream]) > limits[stream]:
                    failed = True
                    break
            if failed:
                break
        if failed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()
            failed = True
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    if failed:
        fail(code)
    return subprocess.CompletedProcess(
        argv, return_code, bytes(output[process.stdout]), bytes(output[process.stderr])
    )


def _read_release_zip(path_value: str, names: frozenset[str]) -> tuple[bytes, dict[str, bytes]]:
    archive_raw = _read_release_evidence_file(path_value, MAX_RELEASE_ARTIFACT_BYTES)
    try:
        with zipfile.ZipFile(io.BytesIO(archive_raw), "r") as archive:
            entries = archive.infolist()
            if (
                archive.comment
                or len(entries) != len(names)
                or {entry.filename for entry in entries} != names
                or len({entry.filename for entry in entries}) != len(entries)
            ):
                fail("release-artifact-member-set-invalid")
            result: dict[str, bytes] = {}
            total = 0
            for entry in entries:
                unix_mode = (entry.external_attr >> 16) & 0o170000
                if (
                    entry.is_dir()
                    or entry.flag_bits & 0x1
                    or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                    or entry.file_size < 1
                    or entry.file_size > package.MAX_JSON_BYTES
                    or entry.compress_size < 1
                    or entry.compress_size > MAX_RELEASE_ARTIFACT_BYTES
                    or unix_mode not in (0, stat.S_IFREG)
                ):
                    fail("release-artifact-member-invalid")
                total += entry.file_size
                if total > 4 * package.MAX_JSON_BYTES:
                    fail("release-artifact-expanded-size-invalid")
                with archive.open(entry, "r") as member:
                    raw = member.read(package.MAX_JSON_BYTES + 1)
                    if member.read(1):
                        fail("release-artifact-member-size-invalid")
                if len(raw) != entry.file_size:
                    fail("release-artifact-member-size-invalid")
                result[entry.filename] = raw
    except (OSError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        fail("release-artifact-zip-invalid")
    return archive_raw, result


def _hardened_https_get(host: str, target: str, content_type: str, maximum: int) -> bytes:
    if (
        host not in (GITHUB_API_HOST, "tmaproduction.blob.core.windows.net")
        or not target.startswith("/")
        or "#" in target
        or not 1 <= maximum <= MAX_REMOTE_BUNDLE_BYTES
    ):
        fail("release-evidence-network-target-invalid")
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    connection = http.client.HTTPSConnection(host, 443, timeout=30, context=context)
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Accept": content_type,
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": GITHUB_USER_AGENT,
            },
        )
        response = connection.getresponse()
        observed_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        lengths = response.headers.get_all("Content-Length") or []
        if (
            response.status != 200
            or response.reason != "OK"
            or response.getheader("Location") is not None
            or response.getheader("Content-Encoding") is not None
            or observed_type != content_type
            or len(lengths) > 1
            or (lengths and (not lengths[0].isdigit() or int(lengths[0]) > maximum))
        ):
            fail("release-evidence-network-response-invalid")
        raw = response.read(maximum + 1)
        if len(raw) < 1 or len(raw) > maximum or (lengths and len(raw) != int(lengths[0])):
            fail("release-evidence-network-size-invalid")
        return raw
    except (OSError, ssl.SSLError, http.client.HTTPException, TimeoutError):
        fail("release-evidence-network-failed")
    finally:
        connection.close()


HTTPSGetter = Callable[[str, str, str, int], bytes]


def _github_json(path: str, getter: HTTPSGetter) -> Any:
    if not path.startswith(f"/{GITHUB_REPOSITORY_API}/") or "?" in path and not path.endswith("?per_page=100"):
        fail("github-api-path-invalid")
    return _strict_decode_json(
        getter(GITHUB_API_HOST, path, "application/json", package.MAX_JSON_BYTES),
        "github-api-json-invalid",
    )


def _github_timestamp(value: Any, code: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(
        r"20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
    ):
        fail(code)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        fail(code)
    return int(parsed.timestamp())


def _observe_runner_docker_socket() -> dict[str, Any]:
    path = Path("/var/run/docker.sock")
    try:
        metadata = path.lstat()
    except OSError:
        fail("runner-docker-socket-unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISSOCK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o660
        or metadata.st_uid != 0
        or metadata.st_gid != 112
        or metadata.st_nlink != 1
        or metadata.st_dev < 1
        or metadata.st_ino < 1
    ):
        fail("runner-docker-socket-invalid")
    return {
        "device": metadata.st_dev,
        "gid": 112,
        "inode": metadata.st_ino,
        "mode": "0660",
        "nlink": 1,
        "path": "/var/run/docker.sock",
        "uid": 0,
    }


def observe_runner_dispatch(
    reservation: dict[str, Any],
    prerequisite: dict[str, Any],
    getter: HTTPSGetter = _hardened_https_get,
) -> dict[str, Any]:
    runs_index = _github_json(
        f"/{GITHUB_REPOSITORY_API}/actions/workflows/smoke-runtime.yml/runs?per_page=100",
        getter,
    )
    runs = runs_index.get("workflow_runs") if isinstance(runs_index, dict) else None
    if (
        not isinstance(runs, list)
        or type(runs_index.get("total_count")) is not int
        or runs_index["total_count"] < len(runs)
        or not 1 <= len(runs) <= 100
    ):
        fail("runner-dispatch-run-index-invalid")
    matches: list[dict[str, Any]] = []
    for run in runs:
        if not isinstance(run, dict):
            fail("runner-dispatch-run-invalid")
        run_id_value = run.get("id")
        run_attempt = run.get("run_attempt")
        repository = run.get("repository")
        head_repository = run.get("head_repository")
        if (
            type(run_id_value) is not int
            or run_id_value < 1
            or str(run_id_value) != prerequisite["run_id"]
            or type(run_attempt) is not int
            or not 1 <= run_attempt < 1 << 31
            or run_attempt != prerequisite["run_attempt"]
            or run.get("event") != "workflow_dispatch"
            or run.get("head_branch") != "main"
            or run.get("head_sha") != reservation["workflow_sha"]
            or run.get("path") != SMOKE_WORKFLOW_PATH
            or run.get("status") not in {"queued", "in_progress"}
            or run.get("conclusion") is not None
            or not isinstance(repository, dict)
            or repository.get("id") != int(package.REPOSITORY_ID)
            or repository.get("full_name") != package.REPOSITORY
            or not isinstance(repository.get("owner"), dict)
            or repository["owner"].get("id") != int(package.REPOSITORY_OWNER_ID)
            or not isinstance(head_repository, dict)
            or head_repository.get("id") != int(package.REPOSITORY_ID)
        ):
            continue
        created_at = _github_timestamp(
            run.get("created_at"), "runner-dispatch-run-time-invalid"
        )
        if (
            created_at < reservation["created_at_epoch"] - 60
            or created_at > reservation["expires_at_epoch"]
        ):
            continue
        run_id = str(run_id_value)
        jobs_index = _github_json(
            f"/{GITHUB_REPOSITORY_API}/actions/runs/{run_id}/attempts/{run_attempt}/jobs?per_page=100",
            getter,
        )
        jobs = jobs_index.get("jobs") if isinstance(jobs_index, dict) else None
        if (
            not isinstance(jobs, list)
            or jobs_index.get("total_count") != len(jobs)
            or not 1 <= len(jobs) <= 100
        ):
            fail("runner-dispatch-job-index-invalid")
        for job in jobs:
            if not isinstance(job, dict):
                fail("runner-dispatch-job-invalid")
            labels = job.get("labels")
            job_id_value = job.get("id")
            if (
                job.get("name") != package.RELEASE_JOB
                or job.get("status") != "queued"
                or job.get("conclusion") is not None
                or job.get("head_sha") != reservation["workflow_sha"]
                or job.get("run_url")
                != f"https://api.github.com/repos/{package.REPOSITORY}/actions/runs/{run_id}"
                or not isinstance(labels, list)
                or len(labels) != 2
                or set(labels)
                != {
                    "propertyquarry-release-controller-v2",
                    reservation["runner_label"],
                }
                or type(job_id_value) is not int
                or job_id_value < 1
                or job.get("runner_id") not in (None, 0)
                or job.get("runner_name") not in (None, "")
            ):
                continue
            matches.append(
                {
                    "job_id": str(job_id_value),
                    "run_attempt": run_attempt,
                    "run_id": run_id,
                    "runner_label": reservation["runner_label"],
                }
            )
    if len(matches) != 1:
        fail("runner-dispatch-ambiguous-or-missing")
    matches[0]["docker_socket"] = _observe_runner_docker_socket()
    return matches[0]


RunnerObserver = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _validate_runner_observation(
    value: dict[str, Any], reservation_binding: dict[str, Any]
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "docker_socket",
        "job_id",
        "run_attempt",
        "run_id",
        "runner_label",
    }:
        fail("runner-observation-shape-invalid")
    socket = value.get("docker_socket")
    if (
        value.get("runner_label") != reservation_binding["runner_label"]
        or not _numeric_id(value.get("run_id"))
        or not _numeric_id(value.get("job_id"))
        or type(value.get("run_attempt")) is not int
        or isinstance(value.get("run_attempt"), bool)
        or not 1 <= value["run_attempt"] < 1 << 31
        or not isinstance(socket, dict)
        or set(socket)
        != {"device", "gid", "inode", "mode", "nlink", "path", "uid"}
        or socket.get("path") != "/var/run/docker.sock"
        or socket.get("mode") != "0660"
        or socket.get("uid") != 0
        or socket.get("gid") != 112
        or socket.get("nlink") != 1
        or type(socket.get("device")) is not int
        or isinstance(socket.get("device"), bool)
        or socket["device"] < 1
        or type(socket.get("inode")) is not int
        or isinstance(socket.get("inode"), bool)
        or socket["inode"] < 1
    ):
        fail("runner-observation-binding-invalid")
    return value


def _validate_runner_observation_prerequisite(
    value: dict[str, Any], prerequisite_binding: dict[str, Any]
) -> dict[str, Any]:
    if (
        value["run_id"] != prerequisite_binding["run_id"]
        or value["run_attempt"] != prerequisite_binding["run_attempt"]
        or value["job_id"]
        == prerequisite_binding["runner_prerequisite_job_id"]
    ):
        fail("runner-observation-prerequisite-binding-invalid")
    return value


def _runner_launch_ticket(
    *,
    reservation_payload: dict[str, Any],
    runner_binding: dict[str, Any],
    config_raw: bytes,
    plan_raw: bytes,
    runtime_sha: str,
    workflow_sha: str,
    web_image: str,
    receipt_private: Ed25519PrivateKey,
    receipt_id: str,
    bound_at: int,
) -> bytes:
    expires_at = min(
        bound_at + RUNNER_TICKET_TTL_SECONDS,
        reservation_payload["expires_at_epoch"],
    )
    if expires_at <= bound_at:
        fail("runner-launch-ticket-window-invalid")
    payload = {
        "authority_profile": package.PROFILE,
        "bound_at_epoch": bound_at,
        "config_digest": package.sha256(config_raw),
        "dispatch_ticket_sha256": runner_binding["reservation_sha256"],
        "docker_socket": runner_binding["docker_socket"],
        "environment": package.ENVIRONMENT,
        "expires_at_epoch": expires_at,
        "job_id": runner_binding["job_id"],
        "plan_digest": package.sha256(plan_raw),
        "receipt_authority_key_id": receipt_id,
        "release_job": package.RELEASE_JOB,
        "repository": package.REPOSITORY,
        "repository_id": package.REPOSITORY_ID,
        "repository_owner_id": package.REPOSITORY_OWNER_ID,
        "reservation_nonce": reservation_payload["reservation_nonce"],
        "run_attempt": runner_binding["run_attempt"],
        "run_id": runner_binding["run_id"],
        "runner_image": web_image,
        "runner_label": runner_binding["runner_label"],
        "runner_label_nonce": reservation_payload["runner_label_nonce"],
        "runner_prerequisite_approval_payload_sha256": runner_binding[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": runner_binding[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": runner_binding[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": runner_binding[
            "runner_prerequisite_job_id"
        ],
        "runtime_sha": runtime_sha,
        "schema": RUNNER_LAUNCH_TICKET_SCHEMA,
        "version": 2,
        "workflow_path": ".github/workflows/smoke-runtime.yml",
        "workflow_ref": package.WORKFLOW_REF,
        "workflow_sha": workflow_sha,
    }
    canonical = package.canonical_json(payload)
    signature = receipt_private.sign(
        _framed(RUNNER_LAUNCH_TICKET_SIGNATURE_DOMAIN, canonical)
    )
    return package.canonical_json(
        {
            "payload": payload,
            "signature": base64.urlsafe_b64encode(signature)
            .rstrip(b"=")
            .decode("ascii"),
            "signature_key_id": receipt_id,
        }
    )


def _validate_runner_launch_ticket(
    raw: bytes,
    *,
    receipt_public: Ed25519PublicKey,
    receipt_id: str,
    reservation_payload: dict[str, Any],
    reservation_binding: dict[str, Any],
    config: dict[str, Any],
    config_raw: bytes,
    plan_raw: bytes,
    current: int,
    observe_socket: bool,
) -> dict[str, Any]:
    try:
        wrapper = package.parse_strict_json(raw, "runner-launch-ticket")
    except package.PackageFailure:
        fail("runner-launch-ticket-wire-invalid")
    payload = wrapper.get("payload") if isinstance(wrapper, dict) else None
    signature_text = wrapper.get("signature") if isinstance(wrapper, dict) else None
    if (
        not isinstance(wrapper, dict)
        or set(wrapper) != {"payload", "signature", "signature_key_id"}
        or type(payload) is not dict
        or type(signature_text) is not str
        or wrapper.get("signature_key_id") != receipt_id
    ):
        fail("runner-launch-ticket-wrapper-invalid")
    try:
        signature = base64.b64decode(
            signature_text.encode("ascii") + b"=" * (-len(signature_text) % 4),
            altchars=b"-_",
            validate=True,
        )
        canonical = package.canonical_json(payload)
        receipt_public.verify(
            signature, _framed(RUNNER_LAUNCH_TICKET_SIGNATURE_DOMAIN, canonical)
        )
    except (UnicodeEncodeError, ValueError, InvalidSignature):
        fail("runner-launch-ticket-signature-invalid")
    if (
        len(signature) != 64
        or signature_text
        != base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    ):
        fail("runner-launch-ticket-signature-encoding-invalid")
    expected_keys = {
        "authority_profile",
        "bound_at_epoch",
        "config_digest",
        "dispatch_ticket_sha256",
        "docker_socket",
        "environment",
        "expires_at_epoch",
        "job_id",
        "plan_digest",
        "receipt_authority_key_id",
        "release_job",
        "repository",
        "repository_id",
        "repository_owner_id",
        "reservation_nonce",
        "run_attempt",
        "run_id",
        "runner_image",
        "runner_label",
        "runner_label_nonce",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256",
        "runner_prerequisite_intent_sha256",
        "runner_prerequisite_job_id",
        "runtime_sha",
        "schema",
        "version",
        "workflow_path",
        "workflow_ref",
        "workflow_sha",
    }
    bound_at = payload.get("bound_at_epoch")
    expires_at = payload.get("expires_at_epoch")
    if (
        set(payload) != expected_keys
        or payload.get("schema") != RUNNER_LAUNCH_TICKET_SCHEMA
        or payload.get("version") != 2
        or payload.get("authority_profile") != package.PROFILE
        or payload.get("environment") != package.ENVIRONMENT
        or payload.get("repository") != package.REPOSITORY
        or payload.get("repository_id") != package.REPOSITORY_ID
        or payload.get("repository_owner_id") != package.REPOSITORY_OWNER_ID
        or payload.get("workflow_path") != ".github/workflows/smoke-runtime.yml"
        or payload.get("workflow_ref") != package.WORKFLOW_REF
        or payload.get("workflow_sha") != config["workflow_sha"]
        or payload.get("release_job") != package.RELEASE_JOB
        or payload.get("runtime_sha") != config["runtime_sha"]
        or payload.get("config_digest") != package.sha256(config_raw)
        or payload.get("plan_digest") != package.sha256(plan_raw)
        or payload.get("receipt_authority_key_id") != receipt_id
        or payload.get("dispatch_ticket_sha256")
        != reservation_binding["reservation_sha256"]
        or payload.get("reservation_nonce")
        != reservation_payload["reservation_nonce"]
        or payload.get("runner_label") != config["runner_label"]
        or payload.get("runner_label_nonce")
        != reservation_payload["runner_label_nonce"]
        or payload.get("run_id") != config["runner_run_id"]
        or payload.get("run_attempt") != config["runner_run_attempt"]
        or payload.get("job_id") != config["runner_job_id"]
        or payload.get("runner_prerequisite_approval_payload_sha256")
        != config["runner_prerequisite_approval_payload_sha256"]
        or payload.get("runner_prerequisite_approval_sha256")
        != config["runner_prerequisite_approval_sha256"]
        or payload.get("runner_prerequisite_intent_sha256")
        != config["runner_prerequisite_intent_sha256"]
        or payload.get("runner_prerequisite_job_id")
        != config["runner_prerequisite_job_id"]
        or payload.get("runner_image") != config["web_image"]
        or type(bound_at) is not int
        or isinstance(bound_at, bool)
        or type(expires_at) is not int
        or isinstance(expires_at, bool)
        or bound_at < config["transaction_started_at_epoch"]
        or expires_at <= bound_at
        or expires_at - bound_at > RUNNER_TICKET_TTL_SECONDS
        or expires_at > reservation_payload["expires_at_epoch"]
        or current < bound_at
        or current > expires_at
        or not isinstance(payload.get("docker_socket"), dict)
    ):
        fail("runner-launch-ticket-binding-or-freshness-invalid")
    socket_binding = _validate_runner_observation(
        {
            "docker_socket": payload["docker_socket"],
            "job_id": config["runner_job_id"],
            "run_attempt": config["runner_run_attempt"],
            "run_id": config["runner_run_id"],
            "runner_label": config["runner_label"],
        },
        reservation_binding,
    )["docker_socket"]
    if observe_socket and socket_binding != _observe_runner_docker_socket():
        fail("runner-launch-ticket-docker-socket-changed")
    return payload


def _runner_materialization_binding_payload(
    *,
    materialization_root: str,
    claim_raw: bytes,
    reservation_raw: bytes,
    config: dict[str, Any],
    config_raw: bytes,
    config_signature: bytes,
    plan_raw: bytes,
    materialization_receipt_raw: bytes,
    materialization_receipt_signature: bytes,
    runner_ticket_raw: bytes,
    bound_at: int,
    receipt_id: str,
) -> dict[str, Any]:
    target = _absolute(materialization_root, "output-path-invalid")
    return {
        "authority_profile": package.PROFILE,
        "bound_at_epoch": bound_at,
        "claim_sha256": package.sha256(claim_raw),
        "config_sha256": package.sha256(config_raw),
        "config_signature_sha256": package.sha256(config_signature),
        "deployment_id": config["deployment_id"],
        "environment": package.ENVIRONMENT,
        "job_id": config["runner_job_id"],
        "materialization_receipt_sha256": package.sha256(
            materialization_receipt_raw
        ),
        "materialization_receipt_signature_sha256": package.sha256(
            materialization_receipt_signature
        ),
        "materialization_root": materialization_root,
        "materialization_root_identity_sha256": _runner_output_identity(target),
        "plan_sha256": package.sha256(plan_raw),
        "receipt_authority_key_id": receipt_id,
        "reservation_sha256": package.sha256(reservation_raw),
        "run_attempt": config["runner_run_attempt"],
        "run_id": config["runner_run_id"],
        "runner_label": config["runner_label"],
        "runner_launch_ticket_sha256": package.sha256(runner_ticket_raw),
        "runner_prerequisite_approval_payload_sha256": config[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": config[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": config[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": config["runner_prerequisite_job_id"],
        "runtime_sha": config["runtime_sha"],
        "schema": RUNNER_MATERIALIZATION_BINDING_SCHEMA,
        "version": 2,
        "workflow_sha": config["workflow_sha"],
    }


def _snappy_raw_decode(raw: bytes, maximum: int = package.MAX_JSON_BYTES) -> bytes:
    position = 0
    declared = 0
    shift = 0
    while True:
        if position >= len(raw) or shift > 63:
            fail("sigstore-remote-snappy-invalid")
        byte = raw[position]
        position += 1
        declared |= (byte & 0x7F) << shift
        if byte < 0x80:
            break
        shift += 7
    if declared < 1 or declared > maximum:
        fail("sigstore-remote-snappy-size-invalid")
    output = bytearray()
    while position < len(raw) and len(output) < declared:
        tag = raw[position]
        position += 1
        kind = tag & 0x03
        if kind == 0:
            encoded_length = tag >> 2
            if encoded_length < 60:
                length = encoded_length + 1
            else:
                width = encoded_length - 59
                if width < 1 or width > 4 or position + width > len(raw):
                    fail("sigstore-remote-snappy-literal-invalid")
                length = int.from_bytes(raw[position : position + width], "little") + 1
                position += width
            if length < 1 or position + length > len(raw) or len(output) + length > declared:
                fail("sigstore-remote-snappy-literal-invalid")
            output.extend(raw[position : position + length])
            position += length
            continue
        if kind == 1:
            length = 4 + ((tag >> 2) & 0x07)
            if position >= len(raw):
                fail("sigstore-remote-snappy-copy-invalid")
            offset = ((tag & 0xE0) << 3) | raw[position]
            position += 1
        elif kind == 2:
            length = 1 + (tag >> 2)
            if position + 2 > len(raw):
                fail("sigstore-remote-snappy-copy-invalid")
            offset = int.from_bytes(raw[position : position + 2], "little")
            position += 2
        else:
            length = 1 + (tag >> 2)
            if position + 4 > len(raw):
                fail("sigstore-remote-snappy-copy-invalid")
            offset = int.from_bytes(raw[position : position + 4], "little")
            position += 4
        if offset < 1 or offset > len(output) or len(output) + length > declared:
            fail("sigstore-remote-snappy-copy-invalid")
        for _ in range(length):
            output.append(output[-offset])
    if position != len(raw) or len(output) != declared:
        fail("sigstore-remote-snappy-trailing-or-size-invalid")
    return bytes(output)


def _decode_der_utf8(raw: bytes) -> str:
    if len(raw) < 2 or raw[0] != 0x0C:
        fail("sigstore-certificate-extension-invalid")
    first = raw[1]
    offset = 2
    if first < 0x80:
        length = first
    else:
        width = first & 0x7F
        if width < 1 or width > 4 or len(raw) < 2 + width:
            fail("sigstore-certificate-extension-invalid")
        encoded_length = raw[2 : 2 + width]
        if encoded_length[0] == 0:
            fail("sigstore-certificate-extension-invalid")
        length = int.from_bytes(encoded_length, "big")
        if length < 0x80:
            fail("sigstore-certificate-extension-invalid")
        offset += width
    if offset + length != len(raw):
        fail("sigstore-certificate-extension-invalid")
    try:
        return raw[offset:].decode("utf-8", "strict")
    except UnicodeError:
        fail("sigstore-certificate-extension-invalid")


def _verify_certificate_claims(
    bundle: dict[str, Any], *, workflow_sha: str, run_id: str, run_attempt: str
) -> None:
    try:
        encoded = bundle["verificationMaterial"]["certificate"]["rawBytes"]
        certificate = x509.load_der_x509_certificate(base64.b64decode(encoded, validate=True))
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        uris = san.get_values_for_type(x509.UniformResourceIdentifier)
        key_usage = certificate.extensions.get_extension_for_class(x509.KeyUsage).value
        extended = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except (KeyError, TypeError, ValueError, x509.ExtensionNotFound):
        fail("sigstore-certificate-invalid")
    if uris != [PUBLISH_WORKFLOW_IDENTITY] or not key_usage.digital_signature or x509.oid.ExtendedKeyUsageOID.CODE_SIGNING not in extended:
        fail("sigstore-certificate-purpose-invalid")
    plain = {
        1: "https://token.actions.githubusercontent.com",
        2: "workflow_dispatch",
        3: workflow_sha,
        4: "propertyquarry-publish-runtime-images",
        5: package.REPOSITORY,
        6: "refs/heads/main",
    }
    wrapped = {
        8: "https://token.actions.githubusercontent.com",
        9: PUBLISH_WORKFLOW_IDENTITY,
        10: workflow_sha,
        11: "github-hosted",
        12: "https://github.com/ArchonMegalon/propertyquarry",
        13: workflow_sha,
        14: "refs/heads/main",
        15: package.REPOSITORY_ID,
        16: "https://github.com/ArchonMegalon",
        17: package.REPOSITORY_OWNER_ID,
        18: PUBLISH_WORKFLOW_IDENTITY,
        19: workflow_sha,
        20: "workflow_dispatch",
        21: f"https://github.com/{package.REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}",
        22: "public",
        23: package.ENVIRONMENT,
        24: (
            "repo:ArchonMegalon@11421547/propertyquarry@1257593732:"
            "environment:propertyquarry-production"
        ),
    }
    for suffix, expected in plain.items():
        try:
            value = certificate.extensions.get_extension_for_oid(
                x509.ObjectIdentifier(f"1.3.6.1.4.1.57264.1.{suffix}")
            ).value.value
        except (x509.ExtensionNotFound, AttributeError):
            fail("sigstore-certificate-identity-invalid")
        try:
            observed = value.decode("utf-8", "strict")
        except UnicodeError:
            fail("sigstore-certificate-identity-invalid")
        if observed != expected:
            fail("sigstore-certificate-identity-invalid")
    for suffix, expected in wrapped.items():
        try:
            value = certificate.extensions.get_extension_for_oid(
                x509.ObjectIdentifier(f"1.3.6.1.4.1.57264.1.{suffix}")
            ).value.value
        except (x509.ExtensionNotFound, AttributeError):
            fail("sigstore-certificate-identity-invalid")
        if _decode_der_utf8(value) != expected:
            fail("sigstore-certificate-identity-invalid")


def _expected_slsa_statement(
    repository: str, digest_value: str, workflow_sha: str, run_id: str, run_attempt: str
) -> dict[str, Any]:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": repository,
                "digest": {"sha256": digest_value.removeprefix("sha256:")},
            }
        ],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {
                "buildType": "https://actions.github.io/buildtypes/workflow/v1",
                "externalParameters": {
                    "workflow": {
                        "path": PUBLISH_WORKFLOW_PATH,
                        "ref": "refs/heads/main",
                        "repository": "https://github.com/ArchonMegalon/propertyquarry",
                    }
                },
                "internalParameters": {
                    "github": {
                        "event_name": "workflow_dispatch",
                        "repository_id": package.REPOSITORY_ID,
                        "repository_owner_id": package.REPOSITORY_OWNER_ID,
                        "runner_environment": "github-hosted",
                    }
                },
                "resolvedDependencies": [
                    {
                        "uri": "git+https://github.com/ArchonMegalon/propertyquarry@refs/heads/main",
                        "digest": {"gitCommit": workflow_sha},
                    }
                ],
            },
            "runDetails": {
                "builder": {"id": PUBLISH_WORKFLOW_IDENTITY},
                "metadata": {
                    "invocationId": (
                        f"https://github.com/{package.REPOSITORY}/actions/runs/"
                        f"{run_id}/attempts/{run_attempt}"
                    )
                },
            },
        },
    }


def _load_attestation_verifier(root_value: str) -> tuple[Path, Path]:
    root = _absolute(root_value, "attestation-verifier-root-invalid")
    try:
        metadata = root.lstat()
        names = {entry.name for entry in root.iterdir()}
    except OSError:
        fail("attestation-verifier-root-unavailable")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or root.resolve() != root
        or names != {"gh", "trusted_root.jsonl"}
    ):
        fail("attestation-verifier-root-invalid")
    expected = (
        (root / "gh", 0o500, GITHUB_ATTESTATION_VERIFIER_BYTES, GITHUB_ATTESTATION_VERIFIER_SHA256),
        (root / "trusted_root.jsonl", 0o400, 5_748, "sha256:3c2cc7f357dc064ec527fdcd78da6e9245c21a381e1abaa0f2b62b186bcac1a1"),
    )
    for path, mode, size, digest_value in expected:
        raw = _read_pinned_file(
            path,
            mode=mode,
            uid=os.geteuid(),
            gid=os.getegid(),
            size=size,
            expected_digest=digest_value,
        )
        if path.name == "gh":
            del raw
    try:
        completed = subprocess.run(
            [os.fspath(root / "gh"), "version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
            env={"HOME": "/nonexistent", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": "/usr/bin:/bin", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired):
        fail("attestation-verifier-execution-invalid")
    if completed.returncode != 0 or completed.stderr or completed.stdout.splitlines()[:1] != [GITHUB_ATTESTATION_VERIFIER_VERSION.encode("ascii")]:
        fail("attestation-verifier-version-invalid")
    return root / "gh", root / "trusted_root.jsonl"


def _verify_sigstore_bundle(
    raw: bytes,
    *,
    repository: str,
    digest_value: str,
    workflow_sha: str,
    run_id: str,
    run_attempt: str,
    run_started_at: int,
    run_completed_at: int,
    verifier_binary: Path,
    trusted_root: Path,
) -> dict[str, Any]:
    bundle = _strict_decode_json(raw, "sigstore-bundle-json-invalid")
    if (
        not isinstance(bundle, dict)
        or set(bundle) != {"dsseEnvelope", "mediaType", "verificationMaterial"}
        or bundle.get("mediaType") != "application/vnd.dev.sigstore.bundle.v0.3+json"
    ):
        fail("sigstore-bundle-shape-invalid")
    envelope = bundle.get("dsseEnvelope")
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"payload", "payloadType", "signatures"}
        or envelope.get("payloadType") != "application/vnd.in-toto+json"
        or not isinstance(envelope.get("payload"), str)
        or not isinstance(envelope.get("signatures"), list)
        or len(envelope["signatures"]) != 1
        or not isinstance(envelope["signatures"][0], dict)
        or set(envelope["signatures"][0]) != {"sig"}
    ):
        fail("sigstore-envelope-invalid")
    try:
        statement_raw = base64.b64decode(envelope["payload"], validate=True)
    except (TypeError, ValueError):
        fail("sigstore-statement-invalid")
    statement = _strict_decode_json(statement_raw, "sigstore-statement-invalid")
    expected_statement = _expected_slsa_statement(
        repository, digest_value, workflow_sha, run_id, run_attempt
    )
    if statement != expected_statement:
        fail("sigstore-statement-binding-invalid")
    _verify_certificate_claims(
        bundle, workflow_sha=workflow_sha, run_id=run_id, run_attempt=run_attempt
    )
    with tempfile.TemporaryDirectory(prefix="propertyquarry-gh-attestation-") as temporary_value:
        temporary = Path(temporary_value)
        os.chmod(temporary, 0o700)
        for name in ("home", "cache", "config"):
            (temporary / name).mkdir(mode=0o700)
        bundle_path = temporary / "bundle.json"
        descriptor = os.open(bundle_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o400)
        try:
            os.fchmod(descriptor, 0o400)
            written = 0
            while written < len(raw):
                written += os.write(descriptor, raw[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        argv = [
            os.fspath(verifier_binary),
            "attestation",
            "verify",
            f"oci://{repository}@{digest_value}",
            "--bundle",
            os.fspath(bundle_path),
            "--custom-trusted-root",
            os.fspath(trusted_root),
            "--repo",
            package.REPOSITORY,
            "--cert-identity",
            PUBLISH_WORKFLOW_IDENTITY,
            "--cert-oidc-issuer",
            "https://token.actions.githubusercontent.com",
            "--signer-digest",
            workflow_sha,
            "--source-digest",
            workflow_sha,
            "--source-ref",
            "refs/heads/main",
            "--deny-self-hosted-runners",
            "--predicate-type",
            "https://slsa.dev/provenance/v1",
            "--format",
            "json",
        ]
        completed = _run_bounded_process(
            argv,
            timeout=120,
            stdout_limit=MAX_REMOTE_BUNDLE_BYTES,
            stderr_limit=65_536,
            code="sigstore-cryptographic-verification-failed",
            cwd=os.fspath(temporary),
            env={
                "DO_NOT_TRACK": "1",
                "GH_CONFIG_DIR": os.fspath(temporary / "config"),
                "GH_NO_UPDATE_NOTIFIER": "1",
                "GH_PROMPT_DISABLED": "1",
                "GH_SPINNER_DISABLED": "1",
                "GH_TELEMETRY": "false",
                "HOME": os.fspath(temporary / "home"),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "NO_COLOR": "1",
                "PATH": "/usr/bin:/bin",
                "TZ": "UTC",
                "XDG_CACHE_HOME": os.fspath(temporary / "cache"),
            },
        )
    if completed.returncode != 0 or completed.stderr or not 1 <= len(completed.stdout) <= MAX_REMOTE_BUNDLE_BYTES:
        fail("sigstore-cryptographic-verification-failed")
    observed_binary, observed_root = _load_attestation_verifier(
        os.fspath(verifier_binary.parent)
    )
    if observed_binary != verifier_binary or observed_root != trusted_root:
        fail("attestation-verifier-mutated")
    verified = _strict_decode_json(completed.stdout, "sigstore-verifier-output-invalid")
    if not isinstance(verified, list) or len(verified) != 1 or not isinstance(verified[0], dict):
        fail("sigstore-verifier-output-invalid")
    item = verified[0]
    attestation = item.get("attestation")
    result = item.get("verificationResult")
    expected_certificate = {
        "certificateIssuer": "CN=sigstore-intermediate,O=sigstore.dev",
        "subjectAlternativeName": PUBLISH_WORKFLOW_IDENTITY,
        "issuer": "https://token.actions.githubusercontent.com",
        "githubWorkflowTrigger": "workflow_dispatch",
        "githubWorkflowSHA": workflow_sha,
        "githubWorkflowName": "propertyquarry-publish-runtime-images",
        "githubWorkflowRepository": package.REPOSITORY,
        "githubWorkflowRef": "refs/heads/main",
        "buildSignerURI": PUBLISH_WORKFLOW_IDENTITY,
        "buildSignerDigest": workflow_sha,
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryURI": "https://github.com/ArchonMegalon/propertyquarry",
        "sourceRepositoryDigest": workflow_sha,
        "sourceRepositoryRef": "refs/heads/main",
        "sourceRepositoryIdentifier": package.REPOSITORY_ID,
        "sourceRepositoryOwnerURI": "https://github.com/ArchonMegalon",
        "sourceRepositoryOwnerIdentifier": package.REPOSITORY_OWNER_ID,
        "buildConfigURI": PUBLISH_WORKFLOW_IDENTITY,
        "buildConfigDigest": workflow_sha,
        "buildTrigger": "workflow_dispatch",
        "runInvocationURI": f"https://github.com/{package.REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}",
        "sourceRepositoryVisibilityAtSigning": "public",
    }
    timestamps = result.get("verifiedTimestamps") if isinstance(result, dict) else None
    signature = result.get("signature") if isinstance(result, dict) else None
    verified_timestamp = None
    if isinstance(timestamps, list) and len(timestamps) == 1 and isinstance(timestamps[0], dict):
        verified_timestamp = _github_timestamp(
            timestamps[0].get("timestamp"), "sigstore-tlog-timestamp-invalid"
        )
    if (
        set(item) != {"attestation", "verificationResult"}
        or not isinstance(attestation, dict)
        or attestation.get("bundle") != bundle
        or attestation.get("bundle_url") != ""
        or attestation.get("initiator") != ""
        or not isinstance(result, dict)
        or result.get("mediaType") != "application/vnd.dev.sigstore.verificationresult+json;version=0.1"
        or not isinstance(signature, dict)
        or signature.get("certificate") != expected_certificate
        or result.get("statement") != expected_statement
        or not isinstance(timestamps, list)
        or len(timestamps) != 1
        or not isinstance(timestamps[0], dict)
        or timestamps[0].get("type") != "Tlog"
        or timestamps[0].get("uri") != "https://rekor.sigstore.dev"
        or verified_timestamp is None
        or verified_timestamp < run_started_at - 60
        or verified_timestamp > run_completed_at + 60
    ):
        fail("sigstore-verifier-binding-invalid")
    return bundle


def _verify_remote_bundle(
    *, bundle: dict[str, Any], attestation_id: str, digest_value: str, getter: HTTPSGetter
) -> None:
    response = _github_json(
        f"/{GITHUB_REPOSITORY_API}/attestations/{digest_value}?per_page=100", getter
    )
    attestations = response.get("attestations") if isinstance(response, dict) else None
    if not isinstance(attestations, list) or not 1 <= len(attestations) <= 100:
        fail("github-attestation-index-invalid")
    matching: list[str] = []
    for item in attestations:
        if not isinstance(item, dict) or set(item) != {"bundle_url", "initiator", "repository_id"}:
            fail("github-attestation-index-invalid")
        url = item.get("bundle_url")
        if (
            item.get("repository_id") != int(package.REPOSITORY_ID)
            or item.get("initiator") != "user"
            or not isinstance(url, str)
        ):
            fail("github-attestation-index-invalid")
        parts = urllib.parse.urlsplit(url)
        path_pattern = rf"/attestations/{package.REPOSITORY_ID}/20[0-9]{{2}}/[0-9]{{2}}/[0-9]{{2}}/{attestation_id}\.json\.sn"
        if (
            parts.scheme != "https"
            or parts.hostname != "tmaproduction.blob.core.windows.net"
            or parts.port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or not re.fullmatch(path_pattern, parts.path)
        ):
            continue
        try:
            query = urllib.parse.parse_qsl(parts.query, keep_blank_values=False, strict_parsing=True)
        except ValueError:
            fail("github-attestation-bundle-url-invalid")
        query_value = dict(query)
        if (
            len(query) != len(query_value)
            or set(query_value) != {"se", "sig", "ske", "skoid", "sks", "skt", "sktid", "skv", "sp", "spr", "sr", "st", "sv"}
            or query_value.get("sp") != "r"
            or query_value.get("spr") != "https"
            or query_value.get("sr") != "b"
        ):
            fail("github-attestation-bundle-url-invalid")
        matching.append(parts.path + "?" + parts.query)
    if len(matching) != 1:
        fail("github-attestation-id-ambiguous-or-missing")
    compressed = getter(
        "tmaproduction.blob.core.windows.net",
        matching[0],
        "application/x-snappy",
        MAX_REMOTE_BUNDLE_BYTES,
    )
    remote = _strict_decode_json(
        _snappy_raw_decode(compressed), "github-attestation-remote-bundle-invalid"
    )
    if remote != bundle:
        fail("github-attestation-remote-bundle-mismatch")


def _validate_artifact_metadata(
    artifact: dict[str, Any], *, name: str, archive_raw: bytes, run_id: str, workflow_sha: str
) -> str:
    artifact_id = artifact.get("id")
    workflow_run = artifact.get("workflow_run")
    if (
        type(artifact_id) is not int
        or artifact_id < 1
        or artifact.get("name") != name
        or artifact.get("expired") is not False
        or artifact.get("size_in_bytes") != len(archive_raw)
        or artifact.get("digest") != _digest(archive_raw)
        or artifact.get("url") != f"https://api.github.com/{GITHUB_REPOSITORY_API}/actions/artifacts/{artifact_id}"
        or artifact.get("archive_download_url") != f"https://api.github.com/{GITHUB_REPOSITORY_API}/actions/artifacts/{artifact_id}/zip"
        or not isinstance(workflow_run, dict)
        or workflow_run.get("id") != int(run_id)
        or workflow_run.get("repository_id") != int(package.REPOSITORY_ID)
        or workflow_run.get("head_repository_id") != int(package.REPOSITORY_ID)
        or workflow_run.get("head_branch") != "main"
        or workflow_run.get("head_sha") != workflow_sha
    ):
        fail("github-artifact-metadata-binding-invalid")
    return str(artifact_id)


def load_release_evidence(
    *,
    final_artifact_path: str,
    preflight_artifact_path: str,
    attestation_verifier_root: str,
    getter: HTTPSGetter = _hardened_https_get,
    now: int | None = None,
) -> dict[str, str]:
    final_archive_raw, final_files = _read_release_zip(final_artifact_path, FINAL_ARTIFACT_MEMBERS)
    preflight_archive_raw, preflight_files = _read_release_zip(preflight_artifact_path, PREFLIGHT_ARTIFACT_MEMBERS)
    receipt_raw = final_files["receipt.json"]
    receipt = _strict_decode_json(receipt_raw, "image-publication-receipt-invalid")
    expected_top = {
        "branch",
        "deployment_performed",
        "environment_variables_mutated",
        "github",
        "images",
        "product",
        "promotion_authority_granted",
        "release_hygiene_receipt_sha256",
        "repository",
        "runtime_commit_sha",
        "schema",
        "status",
        "workflow_envelope_sha",
    }
    runtime_sha = receipt.get("runtime_commit_sha") if isinstance(receipt, dict) else None
    workflow_sha = receipt.get("workflow_envelope_sha") if isinstance(receipt, dict) else None
    github = receipt.get("github") if isinstance(receipt, dict) else None
    images = receipt.get("images") if isinstance(receipt, dict) else None
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected_top
        or receipt.get("schema") != "propertyquarry.image_publish_receipt.v1"
        or receipt.get("status") != "pass"
        or receipt.get("product") != "PropertyQuarry"
        or receipt.get("repository") != package.REPOSITORY
        or receipt.get("branch") != "main"
        or receipt.get("deployment_performed") is not False
        or receipt.get("environment_variables_mutated") is not False
        or receipt.get("promotion_authority_granted") is not False
        or not isinstance(receipt.get("release_hygiene_receipt_sha256"), str)
        or not HEX64.fullmatch(receipt["release_hygiene_receipt_sha256"])
        or not isinstance(runtime_sha, str)
        or not SHA1.fullmatch(runtime_sha)
        or not isinstance(workflow_sha, str)
        or not SHA1.fullmatch(workflow_sha)
        or runtime_sha == workflow_sha
        or not isinstance(github, dict)
        or set(github) != {"run_attempt", "run_id", "workflow_ref"}
        or not isinstance(github.get("run_id"), str)
        or not _numeric_id(github["run_id"])
        or not isinstance(github.get("run_attempt"), str)
        or not _numeric_id(github["run_attempt"])
        or github.get("workflow_ref") != PUBLISH_WORKFLOW_REF
        or not isinstance(images, dict)
        or set(images) != {"render", "web"}
    ):
        fail("image-publication-receipt-binding-invalid")
    run = _github_json(f"/{GITHUB_REPOSITORY_API}/actions/runs/{github['run_id']}", getter)
    repository_value = run.get("repository") if isinstance(run, dict) else None
    head_repository = run.get("head_repository") if isinstance(run, dict) else None
    current = int(time.time()) if now is None else now
    run_created_at = _github_timestamp(run.get("created_at") if isinstance(run, dict) else None, "github-image-run-time-invalid")
    run_started_at = _github_timestamp(run.get("run_started_at") if isinstance(run, dict) else None, "github-image-run-time-invalid")
    run_completed_at = _github_timestamp(run.get("updated_at") if isinstance(run, dict) else None, "github-image-run-time-invalid")
    if (
        not isinstance(run, dict)
        or run.get("id") != int(github["run_id"])
        or run.get("head_sha") != workflow_sha
        or run.get("head_branch") != "main"
        or run.get("event") != "workflow_dispatch"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("run_attempt") != int(github["run_attempt"])
        or run.get("path") != PUBLISH_WORKFLOW_PATH
        or not isinstance(repository_value, dict)
        or repository_value.get("id") != int(package.REPOSITORY_ID)
        or repository_value.get("full_name") != package.REPOSITORY
        or not isinstance(repository_value.get("owner"), dict)
        or repository_value["owner"].get("id") != int(package.REPOSITORY_OWNER_ID)
        or not isinstance(head_repository, dict)
        or head_repository.get("id") != int(package.REPOSITORY_ID)
        or type(current) is not int
        or run_created_at > run_started_at
        or run_started_at > run_completed_at
        or run_completed_at > current + 60
        or current - run_completed_at > MAX_PUBLICATION_EVIDENCE_AGE_SECONDS
    ):
        fail("github-image-run-binding-invalid")
    artifact_index = _github_json(
        f"/{GITHUB_REPOSITORY_API}/actions/runs/{github['run_id']}/artifacts?per_page=100",
        getter,
    )
    artifacts = artifact_index.get("artifacts") if isinstance(artifact_index, dict) else None
    if (
        not isinstance(artifacts, list)
        or artifact_index.get("total_count") != len(artifacts)
        or not 2 <= len(artifacts) <= 100
    ):
        fail("github-artifact-index-invalid")
    final_name = f"propertyquarry-image-publish-receipt-{github['run_id']}-{github['run_attempt']}"
    preflight_name = f"propertyquarry-image-publish-preflight-{github['run_id']}-{github['run_attempt']}"
    final_candidates = [item for item in artifacts if isinstance(item, dict) and item.get("name") == final_name]
    preflight_candidates = [item for item in artifacts if isinstance(item, dict) and item.get("name") == preflight_name]
    if len(final_candidates) != 1 or len(preflight_candidates) != 1:
        fail("github-release-artifact-ambiguous-or-missing")
    final_artifact_id = _validate_artifact_metadata(
        final_candidates[0], name=final_name, archive_raw=final_archive_raw, run_id=github["run_id"], workflow_sha=workflow_sha
    )
    preflight_artifact_id = _validate_artifact_metadata(
        preflight_candidates[0], name=preflight_name, archive_raw=preflight_archive_raw, run_id=github["run_id"], workflow_sha=workflow_sha
    )
    hygiene_raw = preflight_files["release-hygiene.json"]
    hygiene_sha = hashlib.sha256(hygiene_raw).hexdigest()
    if preflight_files["hygiene.sha256"] != (hygiene_sha + "\n").encode("ascii") or hygiene_sha != receipt["release_hygiene_receipt_sha256"]:
        fail("release-hygiene-digest-binding-invalid")
    preflight = _strict_decode_json(preflight_files["preflight.json"], "image-publication-preflight-invalid")
    if preflight != {
        "approved_image_repositories": {
            "render": "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime",
            "web": "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime",
        },
        "branch": "main",
        "deployment_performed": False,
        "environment_variables_mutated": False,
        "product": "PropertyQuarry",
        "release_hygiene_receipt_sha256": hygiene_sha,
        "repository": package.REPOSITORY,
        "repository_visibility": "public",
        "runtime_commit_sha": runtime_sha,
        "schema": "propertyquarry.image_publish_preflight.v1",
        "status": "pass",
        "workflow_envelope_sha": workflow_sha,
    }:
        fail("image-publication-preflight-binding-invalid")
    hygiene = _strict_decode_json(hygiene_raw, "release-hygiene-receipt-invalid")
    if (
        not isinstance(hygiene, dict)
        or hygiene.get("schema") != "propertyquarry.release_hygiene_receipt.v1"
        or hygiene.get("status") != "pass"
        or hygiene.get("failure_count") != 0
        or hygiene.get("failures") != []
        or hygiene.get("head_commit") != workflow_sha
        or hygiene.get("parent_commit") != runtime_sha
        or hygiene.get("manifest_runtime_commit") != runtime_sha
        or hygiene.get("manifest_metadata_only_ancestor") is not True
        or hygiene.get("tracked_dirty_path_count") != 0
        or hygiene.get("untracked_release_source_count") != 0
        or hygiene.get("manifest_descendant_paths")
        != [
            ".codex-design/product/EA_FLAGSHIP_RELEASE_GATE.generated.json",
            ".codex-design/product/WEEKLY_PRODUCT_PULSE.generated.json",
            ".codex-studio/published/EA_BROWSER_WORKFLOW_PROOF.generated.json",
            "docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
        ]
        or hygiene.get("required_checks")
        != [
            "release_manifest_runtime_commit_matches_head_parent_or_metadata_only_ancestor",
            "tracked_worktree_clean",
            "no_untracked_release_source_files",
            "no_tracked_live_env_files",
            "no_tracked_audit_scratch_paths",
            "no_tracked_audit_artifacts",
            "no_hardcoded_local_api_token_marker",
            "no_raw_local_bridge_host_refs",
            "no_hardcoded_bearer_authorization",
        ]
        or not isinstance(hygiene.get("generated_at"), str)
        or not isinstance(hygiene.get("note"), str)
    ):
        fail("release-hygiene-receipt-binding-invalid")
    verifier_binary, trusted_root = _load_attestation_verifier(attestation_verifier_root)
    resolved: dict[str, str] = {}
    attestation_ids: dict[str, str] = {}
    for component, repository in (
        ("web", "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime"),
        ("render", "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime"),
    ):
        value = images[component]
        if not isinstance(value, dict) or set(value) != {
            "attestations", "compose_build_mapping_verified", "compose_service", "digest",
            "image_repository", "immutable_ref", "input_hashes", "labels", "platform",
            "platform_manifest_digest", "remote_verification", "sigstore_provenance", "tagged_ref",
        }:
            fail("image-publication-component-invalid")
        digest_value = value.get("digest")
        immutable = value.get("immutable_ref")
        labels = value.get("labels")
        sigstore = value.get("sigstore_provenance")
        remote = value.get("remote_verification")
        expected_service = "propertyquarry-api" if component == "web" else "propertyquarry-render-tools"
        if (
            value.get("image_repository") != repository
            or value.get("attestations")
            != {"predicates": ["https://slsa.dev/provenance/v1", "https://spdx.dev/Document"], "provenance": "mode=max", "sbom": "spdx"}
            or value.get("compose_service") != expected_service
            or value.get("compose_build_mapping_verified") is not True
            or value.get("platform") != "linux/amd64"
            or not isinstance(digest_value, str)
            or not package.SHA256_PATTERN.fullmatch(digest_value)
            or immutable != f"{repository}@{digest_value}"
            or not isinstance(value.get("platform_manifest_digest"), str)
            or not package.SHA256_PATTERN.fullmatch(value["platform_manifest_digest"])
            or labels
            != {
                "com.propertyquarry.release.runtime-sha": runtime_sha,
                "com.propertyquarry.release.workflow-envelope-sha": workflow_sha,
                "org.opencontainers.image.revision": workflow_sha,
                "org.opencontainers.image.version": runtime_sha,
            }
            or remote
            != {
                "digest_ref_manifest_matches": True,
                "labels_match": True,
                "tag_manifest_matches": True,
            }
            or not isinstance(sigstore, dict)
            or sigstore.get("action")
            != "actions/attest@a1948c3f048ba23858d222213b7c278aabede763"
            or sigstore.get("subject_name") != repository
            or sigstore.get("subject_digest") != digest_value
            or sigstore.get("sigstore_instance") != "public-good"
            or sigstore.get("push_to_registry") is not True
            or sigstore.get("create_storage_record") is not False
            or set(sigstore) != {
                "action", "attestation_id", "attestation_url", "bundle_artifact_path",
                "bundle_sha256", "create_storage_record", "push_to_registry", "sigstore_instance",
                "subject_digest", "subject_name",
            }
            or not isinstance(sigstore.get("attestation_id"), str)
            or not _numeric_id(sigstore["attestation_id"])
            or sigstore.get("attestation_url")
            != f"https://github.com/{package.REPOSITORY}/attestations/{sigstore.get('attestation_id')}"
            or value.get("tagged_ref")
            != f"{repository}:runtime-{runtime_sha}-envelope-{workflow_sha}-run-{github['run_id']}-{github['run_attempt']}"
        ):
            fail("image-publication-component-binding-invalid")
        expected_relative = f"sigstore/{component}-bundle.json"
        if sigstore.get("bundle_artifact_path") != expected_relative:
            fail("sigstore-bundle-path-invalid")
        bundle_raw = final_files[f"sigstore/{component}-bundle.json"]
        bundle_digest = sigstore.get("bundle_sha256")
        if (
            not isinstance(bundle_digest, str)
            or not HEX64.fullmatch(bundle_digest)
            or hashlib.sha256(bundle_raw).hexdigest() != bundle_digest
        ):
            fail("sigstore-bundle-digest-invalid")
        bundle = _verify_sigstore_bundle(
            bundle_raw,
            repository=repository,
            digest_value=digest_value,
            workflow_sha=workflow_sha,
            run_id=github["run_id"],
            run_attempt=github["run_attempt"],
            run_started_at=run_started_at,
            run_completed_at=run_completed_at,
            verifier_binary=verifier_binary,
            trusted_root=trusted_root,
        )
        _verify_remote_bundle(
            bundle=bundle,
            attestation_id=sigstore["attestation_id"],
            digest_value=digest_value,
            getter=getter,
        )
        resolved[component] = immutable
        attestation_ids[component] = sigstore["attestation_id"]
    return {
        "envelope_sha": hashlib.sha256(receipt_raw).hexdigest(),
        "final_artifact_id": final_artifact_id,
        "final_artifact_sha256": hashlib.sha256(final_archive_raw).hexdigest(),
        "github_run_attempt": github["run_attempt"],
        "github_run_completed_at_epoch": str(run_completed_at),
        "github_run_id": github["run_id"],
        "preflight_artifact_id": preflight_artifact_id,
        "preflight_artifact_sha256": hashlib.sha256(preflight_archive_raw).hexdigest(),
        "release_hygiene_sha256": hygiene_sha,
        "render_attestation_id": attestation_ids["render"],
        "render_image": resolved["render"],
        "runtime_sha": runtime_sha,
        "web_attestation_id": attestation_ids["web"],
        "web_image": resolved["web"],
        "workflow_sha": workflow_sha,
    }


def _load_live_modules() -> tuple[Any, Any, Any]:
    expected = (
        (
            REPOSITORY_ROOT / "scripts/propertyquarry_predeploy_backup_v2.py",
            package.PREDEPLOY_BACKUP_HELPER_SHA256,
            package.PREDEPLOY_BACKUP_HELPER_BYTES,
            "predeploy-backup-helper",
        ),
        (
            REPOSITORY_ROOT / "scripts/propertyquarry_runtime_isolation_v2.py",
            package.RUNTIME_ISOLATION_HELPER_SHA256,
            package.RUNTIME_ISOLATION_HELPER_BYTES,
            "runtime-isolation-helper",
        ),
        (
            REPOSITORY_ROOT / "scripts/propertyquarry_runtime_deploy_v2.py",
            package.RUNTIME_DEPLOY_HELPER_SHA256,
            package.RUNTIME_DEPLOY_HELPER_BYTES,
            "runtime-deploy-helper",
        ),
    )
    for path, digest, size, label in expected:
        package.validate_sealed_helper(
            package.read_regular(path, package.MAX_JSON_BYTES),
            expected_sha256=digest,
            expected_bytes=size,
            label=label,
        )
    sys.dont_write_bytecode = True
    if os.fspath(REPOSITORY_ROOT) not in sys.path:
        sys.path.insert(0, os.fspath(REPOSITORY_ROOT))
    from scripts import propertyquarry_predeploy_backup_v2 as backup
    from scripts import propertyquarry_runtime_deploy_v2 as deploy
    from scripts import propertyquarry_runtime_isolation_v2 as isolation

    return backup, deploy, isolation


def _run_observation_command(argv: list[str], maximum: int = package.MAX_JSON_BYTES) -> bytes:
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
            env={
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "HOME": "/nonexistent",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "TZ": "UTC",
            },
        )
    except (OSError, subprocess.TimeoutExpired):
        fail("release-observation-command-failed")
    if completed.returncode != 0 or len(completed.stdout) > maximum:
        fail("release-observation-command-failed")
    return completed.stdout


def _verify_release_checkout_and_images(release: dict[str, str]) -> None:
    runtime_sha = release["runtime_sha"]
    workflow_sha = release["workflow_sha"]
    git = [
        "/usr/bin/git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        os.fspath(REPOSITORY_ROOT),
    ]
    top = _run_observation_command(
        [*git, "rev-parse", "--show-toplevel"]
    ).decode("ascii", "strict").strip()
    head = _run_observation_command(
        [*git, "rev-parse", "HEAD"]
    ).decode("ascii", "strict").strip()
    dirty = _run_observation_command(
        [
            *git,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ]
    )
    _run_observation_command(
        [
            *git,
            "cat-file",
            "-e",
            f"{runtime_sha}^{{commit}}",
        ]
    )
    _run_observation_command(
        [
            *git,
            "merge-base",
            "--is-ancestor",
            runtime_sha,
            workflow_sha,
        ]
    )
    if top != os.fspath(REPOSITORY_ROOT) or head != workflow_sha or dirty:
        fail("release-checkout-binding-invalid")
    manifest_raw = _run_observation_command(
        [
            *git,
            "show",
            f"{workflow_sha}:docs/PROPERTYQUARRY_RELEASE_MANIFEST.md",
        ]
    )
    try:
        manifest_text = manifest_raw.decode("utf-8", "strict")
    except UnicodeError:
        fail("release-manifest-invalid")
    if (
        manifest_text.count("<!-- propertyquarry-release-manifest-json:start -->") != 1
        or manifest_text.count("<!-- propertyquarry-release-manifest-json:end -->") != 1
    ):
        fail("release-manifest-binding-invalid")
    manifest_match = re.fullmatch(
        r"(?s).*?<!-- propertyquarry-release-manifest-json:start -->\n```json\n(\{.*?\})\n```\n<!-- propertyquarry-release-manifest-json:end -->.*",
        manifest_text,
    )
    if manifest_match is None:
        fail("release-manifest-binding-invalid")
    manifest = _strict_decode_json(
        manifest_match.group(1).encode("utf-8"), "release-manifest-invalid"
    )
    if (
        not isinstance(manifest, dict)
        or manifest.get("release_commit_sha") != runtime_sha
        or manifest.get("release_repository") != package.REPOSITORY
        or manifest.get("release_branch") != "main"
        or manifest.get("release_product") != "PropertyQuarry"
        or manifest.get("release_manifest_schema") != "propertyquarry.release_manifest.v1"
    ):
        fail("release-manifest-binding-invalid")


def _require_runner_identity_free() -> None:
    for lookup, value in (
        (pwd.getpwuid, 1999),
        (grp.getgrgid, 1999),
        (pwd.getpwnam, "propertyquarry-runner-v2"),
        (grp.getgrnam, "propertyquarry-release-v2"),
    ):
        try:
            lookup(value)
        except KeyError:
            continue
        fail("runner-identity-not-free")


def observe_live(release: dict[str, str], deployment_id: str) -> dict[str, Any]:
    _verify_release_checkout_and_images(release)
    runtime_sha = release["runtime_sha"]
    backup, deploy, isolation = _load_live_modules()
    if isolation.CLOUDFLARED_IMAGE != FIXED_CLOUDFLARED_IMAGE:
        fail("cloudflared-sealed-binding-invalid")
    _require_runner_identity_free()
    pre_inputs = deploy._runtime_input_observations()  # noqa: SLF001
    root_raw = isolation._read_regular(  # noqa: SLF001
        isolation.ROOT_ENV,
        max_bytes=isolation.MAX_ENV_BYTES,
        mode=0o600,
        uid=1000,
        gid=1000,
    )
    if (
        not isinstance(pre_inputs, list)
        or len(pre_inputs) != len(package.RUNTIME_INPUT_PATHS)
        or pre_inputs[0].get("sha256") != _digest(root_raw)
        or pre_inputs[0].get("size") != len(root_raw)
    ):
        fail("live-root-env-observation-mismatch")
    post_raw, removed = isolation._filtered_root_env(root_raw)  # noqa: SLF001
    if removed not in {
        len(isolation.LEGACY_MAIL_KEYS),
        len(isolation.MAIL_KEYS),
    } or len(post_raw) >= len(root_raw):
        fail("live-root-env-transition-invalid")
    post_inputs = [dict(item) for item in pre_inputs]
    post_inputs[0]["sha256"] = _digest(post_raw)
    post_inputs[0]["size"] = len(post_raw)
    observations = deploy._observations()  # noqa: SLF001
    runtime_deploy = {
        "compose_argv": package.expected_compose_argv(),
        "compose_files": observations["compose_files"],
        "compose_plugin": observations["compose_plugin"],
        "deployment_id": deployment_id,
        "docker_executable": observations["docker_executable"],
        "env_files": list(package.RUNTIME_INPUT_PATHS),
        "operation": "deploy-runtime",
        "receipt_path": (
            f"{package.RUNTIME_DEPLOY_RECEIPT_ROOT}/{runtime_sha}/{deployment_id}/"
            "deploy-runtime.json"
        ),
    }
    all_containers = isolation._all_containers()  # noqa: SLF001
    desired = set(package.DESIRED_RUNTIME_CONTAINER_ALLOWLIST)
    matching = {
        name
        for name, value in all_containers.items()
        if isolation._is_propertyquarry_container_match(name, value)  # noqa: SLF001
    }
    stale = sorted(matching - desired)
    retirement = {
        "containers": [
            isolation._retirement_observation(name, all_containers[name])  # noqa: SLF001
            for name in stale
        ],
        "deployment_id": deployment_id,
        "desired_live_allowlist": list(package.DESIRED_RUNTIME_CONTAINER_ALLOWLIST),
        "operation": "retire-stale-propertyquarry-runtime",
        "preserve_volumes": True,
        "receipt_path": (
            f"{package.RUNTIME_ISOLATION_RECEIPT_ROOT}/{runtime_sha}/{deployment_id}/"
            "retire-stale-propertyquarry-runtime.json"
        ),
    }
    machine = package.read_regular(
        "/etc/machine-id", 64, expected_modes=(0o444, 0o644)
    ).strip()
    if not re.fullmatch(rb"[0-9a-f]{32}", machine):
        fail("host-machine-id-invalid")
    substrate = backup._verify_database_substrate(package.DATABASE_IMAGE)  # noqa: SLF001
    if (
        deploy._runtime_input_observations() != pre_inputs  # noqa: SLF001
        or deploy._observations() != observations  # noqa: SLF001
        or isolation._all_containers() != all_containers  # noqa: SLF001
        or package.read_regular("/etc/machine-id", 64, expected_modes=(0o444, 0o644)).strip()
        != machine
        or backup._verify_database_substrate(package.DATABASE_IMAGE)  # noqa: SLF001
        != substrate
    ):
        fail("live-observation-changed-during-materialization")
    return {
        "database_substrate": substrate,
        "host_machine_id_digest": _digest(machine),
        "pre_purge_runtime_inputs": pre_inputs,
        "runtime_deploy": runtime_deploy,
        "runtime_inputs": post_inputs,
        "runtime_retirement": retirement,
        "observed_at_epoch": int(time.time()),
    }


Observer = Callable[[dict[str, str], str], dict[str, Any]]


def _validate_release_evidence(release_evidence: dict[str, str]) -> None:
    if set(release_evidence) != {
        "envelope_sha",
        "final_artifact_id",
        "final_artifact_sha256",
        "github_run_attempt",
        "github_run_completed_at_epoch",
        "github_run_id",
        "preflight_artifact_id",
        "preflight_artifact_sha256",
        "release_hygiene_sha256",
        "render_attestation_id",
        "render_image",
        "runtime_sha",
        "web_attestation_id",
        "web_image",
        "workflow_sha",
    }:
        fail("release-evidence-shape-invalid")
    runtime_sha = release_evidence["runtime_sha"]
    workflow_sha = release_evidence["workflow_sha"]
    envelope_sha = release_evidence["envelope_sha"]
    web_image = release_evidence["web_image"]
    render_image = release_evidence["render_image"]
    if (
        not SHA1.fullmatch(runtime_sha)
        or not SHA1.fullmatch(workflow_sha)
        or runtime_sha == workflow_sha
        or not HEX64.fullmatch(envelope_sha)
        or not package.IMAGE_PATTERN.fullmatch(web_image)
        or not web_image.startswith(
            "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:"
        )
        or not package.IMAGE_PATTERN.fullmatch(render_image)
        or not render_image.startswith(
            "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:"
        )
        or web_image == render_image
        or not _numeric_id(release_evidence["github_run_id"])
        or not _numeric_id(release_evidence["github_run_attempt"])
        or not _numeric_id(release_evidence["github_run_completed_at_epoch"])
        or not _numeric_id(release_evidence["final_artifact_id"])
        or not _numeric_id(release_evidence["preflight_artifact_id"])
        or not _numeric_id(release_evidence["render_attestation_id"])
        or not _numeric_id(release_evidence["web_attestation_id"])
        or not HEX64.fullmatch(release_evidence["final_artifact_sha256"])
        or not HEX64.fullmatch(release_evidence["preflight_artifact_sha256"])
        or not HEX64.fullmatch(release_evidence["release_hygiene_sha256"])
    ):
        fail("materialization-input-invalid")


def _release_evidence_from_materialization(
    config: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, str]:
    return {
        "envelope_sha": config["envelope_sha"],
        "final_artifact_id": receipt["final_artifact_id"],
        "final_artifact_sha256": receipt["final_artifact_sha256"],
        "github_run_attempt": receipt["image_publication_run_attempt"],
        "github_run_completed_at_epoch": str(
            receipt["image_publication_run_completed_at_epoch"]
        ),
        "github_run_id": receipt["image_publication_run_id"],
        "preflight_artifact_id": receipt["preflight_artifact_id"],
        "preflight_artifact_sha256": receipt["preflight_artifact_sha256"],
        "release_hygiene_sha256": receipt["release_hygiene_sha256"],
        "render_attestation_id": receipt["render_attestation_id"],
        "render_image": config["render_image"],
        "runtime_sha": config["runtime_sha"],
        "web_attestation_id": receipt["web_attestation_id"],
        "web_image": config["web_image"],
        "workflow_sha": config["workflow_sha"],
    }


def _materialize_locked(
    *,
    authority_root: str,
    output: str,
    release_evidence: dict[str, str],
    now: int | None = None,
    observer: Observer = observe_live,
    runner_observer: RunnerObserver = observe_runner_dispatch,
) -> dict[str, Any]:
    _validate_release_evidence(release_evidence)
    runtime_sha = release_evidence["runtime_sha"]
    workflow_sha = release_evidence["workflow_sha"]
    envelope_sha = release_evidence["envelope_sha"]
    web_image = release_evidence["web_image"]
    render_image = release_evidence["render_image"]
    (
        _package_anchor,
        package_private,
        package_public,
        receipt_private,
        receipt_public,
        package_id,
        receipt_id,
    ) = _load_authority(authority_root)
    generation, predecessor = 1, "genesis"
    requested_deployment_id = secrets.token_hex(32)
    requested_started = int(time.time()) if now is None else now
    if type(requested_started) is not int or requested_started < 1:
        fail("materialization-time-invalid")
    if not RUNNER_RESERVATION_ROOT.exists():
        fail("runner-reservation-active-missing")
    reservation_raw = _read_runner_reservation_directory(RUNNER_RESERVATION_ROOT)
    reservation_payload, runner_binding = _validate_runner_reservation(
        reservation_raw,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
        workflow_sha=workflow_sha,
        current=requested_started,
    )
    prerequisite_intent_raw, prerequisite_approval_raw = (
        _read_runner_prerequisite_records(reservation_raw)
    )
    prerequisite_binding = _validate_runner_prerequisite_records(
        intent_raw=prerequisite_intent_raw,
        approval_raw=prerequisite_approval_raw,
        reservation_raw=reservation_raw,
        reservation_payload=reservation_payload,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
        current=requested_started,
    )
    runner_binding.update(
        {
            key: prerequisite_binding[key]
            for key in (
                "runner_prerequisite_approval_payload_sha256",
                "runner_prerequisite_approval_sha256",
                "runner_prerequisite_intent_sha256",
                "runner_prerequisite_job_id",
            )
        }
    )
    claim_payload, claim_raw, claim_disposition = (
        _ensure_runner_materialization_claim(
            reservation_raw=reservation_raw,
            reservation_payload=reservation_payload,
            reservation_binding=runner_binding,
            prerequisite_binding=prerequisite_binding,
            output=output,
            release_evidence=release_evidence,
            requested_started=requested_started,
            requested_deployment_id=requested_deployment_id,
            receipt_private=receipt_private,
            receipt_public=receipt_public,
            receipt_id=receipt_id,
            enforce_deadline=True,
        )
    )
    deployment_id = claim_payload["deployment_id"]
    started = claim_payload["claimed_at_epoch"]
    _materialization_checkpoint("after-runner-materialization-claim")
    observation = observer(release_evidence, deployment_id)
    observed_at = observation.get("observed_at_epoch")
    finished = int(time.time()) if now is None else observed_at
    if (
        type(observed_at) is not int
        or type(finished) is not int
        or observed_at < started
        or finished < observed_at
        or observed_at - started > MAX_OBSERVATION_SECONDS
        or finished - started > MAX_OBSERVATION_SECONDS
    ):
        fail("materialization-observation-time-invalid")
    initial_runner_observation = _validate_runner_observation(
        runner_observer(
            reservation_payload, prerequisite_binding["approval_payload"]
        ),
        runner_binding,
    )
    _validate_runner_observation_prerequisite(
        initial_runner_observation, prerequisite_binding
    )
    runner_binding.update(initial_runner_observation)
    config, plan, plan_raw = _build_documents(
        runtime_sha=runtime_sha,
        workflow_sha=workflow_sha,
        envelope_sha=envelope_sha,
        web_image=web_image,
        render_image=render_image,
        cloudflared_image=FIXED_CLOUDFLARED_IMAGE,
        deployment_id=deployment_id,
        generation=generation,
        predecessor=predecessor,
        runner_uid=1999,
        runner_gid=1999,
        started=started,
        observation=observation,
        runner_binding=runner_binding,
        package_key_id=package_id,
        receipt_key_id=receipt_id,
    )
    config_raw = package.canonical_json(config)
    config_signature = package_private.sign(
        package.framed(package.CONFIG_SIGNATURE_DOMAIN, config_raw)
    )
    package.validate_config_and_plan(
        config_raw,
        config_signature,
        plan_raw,
        package_public,
        package_id,
        receipt_public,
    )
    final_runner_observation = _validate_runner_observation(
        runner_observer(
            reservation_payload, prerequisite_binding["approval_payload"]
        ),
        runner_binding,
    )
    if final_runner_observation != initial_runner_observation:
        fail("runner-observation-changed-during-materialization")
    if _read_runner_prerequisite_records(reservation_raw) != (
        prerequisite_intent_raw,
        prerequisite_approval_raw,
    ):
        fail("runner-prerequisite-record-changed-during-materialization")
    bound_at = int(time.time()) if now is None else finished
    if bound_at < finished or bound_at - started > MAX_OBSERVATION_SECONDS:
        fail("runner-observation-time-invalid")
    runner_ticket_raw = _runner_launch_ticket(
        reservation_payload=reservation_payload,
        runner_binding=runner_binding,
        config_raw=config_raw,
        plan_raw=plan_raw,
        runtime_sha=runtime_sha,
        workflow_sha=workflow_sha,
        web_image=web_image,
        receipt_private=receipt_private,
        receipt_id=receipt_id,
        bound_at=bound_at,
    )
    receipt = {
        "authoritative": False,
        "config_sha256": package.sha256(config_raw),
        "deployment_id": deployment_id,
        "final_artifact_id": release_evidence["final_artifact_id"],
        "final_artifact_sha256": release_evidence["final_artifact_sha256"],
        "image_publication_run_attempt": release_evidence["github_run_attempt"],
        "image_publication_run_completed_at_epoch": int(
            release_evidence["github_run_completed_at_epoch"]
        ),
        "image_publication_run_id": release_evidence["github_run_id"],
        "installed_state_absence_proven": False,
        "materialized_at_epoch": started,
        "observation_completed_at_epoch": bound_at,
        "package_authority_key_id": package_id,
        "plan_sha256": package.sha256(plan_raw),
        "preflight_artifact_id": release_evidence["preflight_artifact_id"],
        "preflight_artifact_sha256": release_evidence["preflight_artifact_sha256"],
        "production_ready": False,
        "receipt_authority_key_id": receipt_id,
        "release_hygiene_sha256": release_evidence["release_hygiene_sha256"],
        "release_generation": generation,
        "render_attestation_id": release_evidence["render_attestation_id"],
        "root_helper_authorization_required": True,
        "runner_launch_ticket_sha256": package.sha256(runner_ticket_raw),
        "runner_prerequisite_approval_payload_sha256": runner_binding[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": runner_binding[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": runner_binding[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": runner_binding[
            "runner_prerequisite_job_id"
        ],
        "runner_source_checkout_identity_sha256": runner_binding[
            "source_checkout_identity_sha256"
        ],
        "runner_source_checkout_path": runner_binding["source_checkout_path"],
        "runner_source_tree_sha256": runner_binding["source_tree_sha256"],
        "runtime_sha": runtime_sha,
        "schema": MATERIAL_SCHEMA,
        "valid_until_epoch": started + package.BACKUP_MAX_AGE_SECONDS,
        "version": 2,
        "web_attestation_id": release_evidence["web_attestation_id"],
        "workflow_sha": workflow_sha,
    }
    receipt_raw = package.canonical_json(receipt)
    receipt_signature = package_private.sign(
        _framed(MATERIAL_SIGNATURE_DOMAIN, receipt_raw)
    )
    _publish_private_directory(
        output,
        {
            "authority.v2.json": config_raw,
            "authority.v2.sig": config_signature,
            "materialization-receipt.v2.json": receipt_raw,
            "materialization-receipt.v2.sig": receipt_signature,
            "runner-launch-ticket.v2.json": runner_ticket_raw,
            "runner-prerequisite-approval.v2.json": prerequisite_approval_raw,
            "runner-prerequisite-intent.v2.json": prerequisite_intent_raw,
            "runner-reservation.v2.json": reservation_raw,
            "transaction-plan.v2.json": plan_raw,
        },
    )
    _materialization_checkpoint("after-materialization-publish")
    binding_payload = _runner_materialization_binding_payload(
        materialization_root=output,
        claim_raw=claim_raw,
        reservation_raw=reservation_raw,
        config=config,
        config_raw=config_raw,
        config_signature=config_signature,
        plan_raw=plan_raw,
        materialization_receipt_raw=receipt_raw,
        materialization_receipt_signature=receipt_signature,
        runner_ticket_raw=runner_ticket_raw,
        bound_at=bound_at,
        receipt_id=receipt_id,
    )
    binding_raw = _signed_runner_record(
        binding_payload,
        private=receipt_private,
        key_id=receipt_id,
        domain=RUNNER_MATERIALIZATION_BINDING_SIGNATURE_DOMAIN,
    )
    reservation_disposition = _consume_runner_reservation(
        reservation_raw,
        binding_raw,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
    )
    _materialization_checkpoint("after-runner-reservation-bind")
    return {
        "authority_root": authority_root,
        "authoritative": False,
        "config_sha256": receipt["config_sha256"],
        "deployment_id": deployment_id,
        "materialization_root": output,
        "plan_sha256": receipt["plan_sha256"],
        "release_generation": generation,
        "root_helper_authorization_required": True,
        "runner_launch_ticket_sha256": receipt["runner_launch_ticket_sha256"],
        "runner_prerequisite_approval_payload_sha256": receipt[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": receipt[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": receipt[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": receipt["runner_prerequisite_job_id"],
        "runner_materialization_claim_disposition": claim_disposition,
        "runner_materialization_claim_sha256": package.sha256(claim_raw),
        "runner_materialization_binding_sha256": package.sha256(binding_raw),
        "runner_reservation_disposition": reservation_disposition,
        "runner_reservation_sha256": runner_binding["reservation_sha256"],
        "runtime_sha": runtime_sha,
        "schema": "propertyquarry.release-control.single-host-production-materialization-result.v2",
        "valid_until_epoch": receipt["valid_until_epoch"],
        "version": 2,
        "workflow_sha": workflow_sha,
    }


def materialize(
    *,
    authority_root: str,
    output: str,
    release_evidence: dict[str, str],
    now: int | None = None,
    observer: Observer = observe_live,
    runner_observer: RunnerObserver = observe_runner_dispatch,
) -> dict[str, Any]:
    lock = _acquire_runner_reservation_lock()
    try:
        target = _absolute(output, "output-path-invalid")
        if target.exists() or target.is_symlink():
            return _recover_published_materialization(
                authority_root=authority_root,
                output=output,
                release_evidence=release_evidence,
                now=now,
            )
        return _materialize_locked(
            authority_root=authority_root,
            output=output,
            release_evidence=release_evidence,
            now=now,
            observer=observer,
            runner_observer=runner_observer,
        )
    finally:
        os.close(lock)


def _validated_materialization(
    *,
    authority_root: str,
    materialization_root: str,
    current: int,
    require_bound_terminal: bool,
    observe_socket: bool,
) -> dict[str, Any]:
    if type(current) is not int or isinstance(current, bool) or current < 1:
        fail("materialization-current-time-invalid")
    (
        _authority_files,
        _package_private,
        package_public,
        _receipt_private,
        receipt_public,
        package_id,
        receipt_id,
    ) = _load_authority(authority_root)
    files = _read_exact_private_directory(materialization_root, MATERIAL_FILES)
    try:
        receipt = package.parse_strict_json(
            files["materialization-receipt.v2.json"], "materialization-receipt"
        )
        package_public.verify(
            files["materialization-receipt.v2.sig"],
            _framed(
                MATERIAL_SIGNATURE_DOMAIN,
                files["materialization-receipt.v2.json"],
            ),
        )
    except (InvalidSignature, package.PackageFailure):
        fail("materialization-receipt-invalid")
    config, plan, observed_receipt_id = package.validate_config_and_plan(
        files["authority.v2.json"],
        files["authority.v2.sig"],
        files["transaction-plan.v2.json"],
        package_public,
        package_id,
        receipt_public,
    )
    reservation_raw = files["runner-reservation.v2.json"]
    reservation_payload, reservation_binding = _validate_runner_reservation(
        reservation_raw,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
        workflow_sha=config["workflow_sha"],
        current=current,
    )
    prerequisite_intent_raw = files["runner-prerequisite-intent.v2.json"]
    prerequisite_approval_raw = files["runner-prerequisite-approval.v2.json"]
    prerequisite_binding = _validate_runner_prerequisite_records(
        intent_raw=prerequisite_intent_raw,
        approval_raw=prerequisite_approval_raw,
        reservation_raw=reservation_raw,
        reservation_payload=reservation_payload,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
        current=current,
    )
    reservation_binding.update(
        {
            key: prerequisite_binding[key]
            for key in (
                "runner_prerequisite_approval_payload_sha256",
                "runner_prerequisite_approval_sha256",
                "runner_prerequisite_intent_sha256",
                "runner_prerequisite_job_id",
            )
        }
    )
    if (
        config["runner_reservation_sha256"]
        != reservation_binding["reservation_sha256"]
        or config["runner_label"] != reservation_binding["runner_label"]
        or config["runner_run_id"] != prerequisite_binding["run_id"]
        or config["runner_run_attempt"] != prerequisite_binding["run_attempt"]
        or config["runner_job_id"]
        == prerequisite_binding["runner_prerequisite_job_id"]
        or any(
            config[key] != prerequisite_binding[key]
            for key in (
                "runner_prerequisite_approval_payload_sha256",
                "runner_prerequisite_approval_sha256",
                "runner_prerequisite_intent_sha256",
                "runner_prerequisite_job_id",
            )
        )
    ):
        fail("materialization-runner-reservation-binding-invalid")
    ticket_raw = files["runner-launch-ticket.v2.json"]
    ticket_payload = _validate_runner_launch_ticket(
        ticket_raw,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
        reservation_payload=reservation_payload,
        reservation_binding=reservation_binding,
        config=config,
        config_raw=files["authority.v2.json"],
        plan_raw=files["transaction-plan.v2.json"],
        current=current,
        observe_socket=observe_socket,
    )
    expected = {
        "authoritative": False,
        "config_sha256": package.sha256(files["authority.v2.json"]),
        "deployment_id": config["deployment_id"],
        "final_artifact_id": receipt.get("final_artifact_id"),
        "final_artifact_sha256": receipt.get("final_artifact_sha256"),
        "image_publication_run_attempt": receipt.get("image_publication_run_attempt"),
        "image_publication_run_completed_at_epoch": receipt.get(
            "image_publication_run_completed_at_epoch"
        ),
        "image_publication_run_id": receipt.get("image_publication_run_id"),
        "installed_state_absence_proven": False,
        "materialized_at_epoch": config["transaction_started_at_epoch"],
        "observation_completed_at_epoch": receipt.get("observation_completed_at_epoch"),
        "package_authority_key_id": package_id,
        "plan_sha256": package.sha256(files["transaction-plan.v2.json"]),
        "preflight_artifact_id": receipt.get("preflight_artifact_id"),
        "preflight_artifact_sha256": receipt.get("preflight_artifact_sha256"),
        "production_ready": False,
        "receipt_authority_key_id": receipt_id,
        "release_hygiene_sha256": receipt.get("release_hygiene_sha256"),
        "release_generation": config["release_generation"],
        "render_attestation_id": receipt.get("render_attestation_id"),
        "root_helper_authorization_required": True,
        "runner_launch_ticket_sha256": package.sha256(ticket_raw),
        "runner_prerequisite_approval_payload_sha256": prerequisite_binding[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": prerequisite_binding[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": prerequisite_binding[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": prerequisite_binding[
            "runner_prerequisite_job_id"
        ],
        "runner_source_checkout_identity_sha256": reservation_binding[
            "source_checkout_identity_sha256"
        ],
        "runner_source_checkout_path": reservation_binding["source_checkout_path"],
        "runner_source_tree_sha256": reservation_binding["source_tree_sha256"],
        "runtime_sha": config["runtime_sha"],
        "schema": MATERIAL_SCHEMA,
        "valid_until_epoch": config["transaction_started_at_epoch"]
        + package.BACKUP_MAX_AGE_SECONDS,
        "version": 2,
        "web_attestation_id": receipt.get("web_attestation_id"),
        "workflow_sha": config["workflow_sha"],
    }
    if (
        receipt != expected
        or observed_receipt_id != receipt_id
        or plan["runtime_sha"] != config["runtime_sha"]
        or config["release_generation"] != 1
        or config["predecessor_runtime_sha"] != "genesis"
        or not isinstance(receipt["image_publication_run_id"], str)
        or not _numeric_id(receipt["image_publication_run_id"])
        or not isinstance(receipt["image_publication_run_attempt"], str)
        or not _numeric_id(receipt["image_publication_run_attempt"])
        or type(receipt["image_publication_run_completed_at_epoch"]) is not int
        or receipt["image_publication_run_completed_at_epoch"]
        > receipt["materialized_at_epoch"] + 60
        or receipt["materialized_at_epoch"]
        - receipt["image_publication_run_completed_at_epoch"]
        > MAX_PUBLICATION_EVIDENCE_AGE_SECONDS
        or not isinstance(receipt["final_artifact_id"], str)
        or not _numeric_id(receipt["final_artifact_id"])
        or not isinstance(receipt["preflight_artifact_id"], str)
        or not _numeric_id(receipt["preflight_artifact_id"])
        or not isinstance(receipt["render_attestation_id"], str)
        or not _numeric_id(receipt["render_attestation_id"])
        or not isinstance(receipt["web_attestation_id"], str)
        or not _numeric_id(receipt["web_attestation_id"])
        or not isinstance(receipt["final_artifact_sha256"], str)
        or not HEX64.fullmatch(receipt["final_artifact_sha256"])
        or not isinstance(receipt["preflight_artifact_sha256"], str)
        or not HEX64.fullmatch(receipt["preflight_artifact_sha256"])
        or not isinstance(receipt["release_hygiene_sha256"], str)
        or not HEX64.fullmatch(receipt["release_hygiene_sha256"])
        or type(receipt["observation_completed_at_epoch"]) is not int
        or receipt["observation_completed_at_epoch"]
        < receipt["materialized_at_epoch"]
        or receipt["observation_completed_at_epoch"]
        - receipt["materialized_at_epoch"]
        > MAX_OBSERVATION_SECONDS
        or current < receipt["materialized_at_epoch"]
        or current > receipt["valid_until_epoch"]
    ):
        fail("materialization-binding-or-freshness-invalid")
    release_evidence = _release_evidence_from_materialization(config, receipt)
    _validate_release_evidence(release_evidence)
    claim_path = _runner_claim_path(reservation_raw)
    if not claim_path.exists():
        fail("materialization-runner-claim-missing")
    claim_raw = _read_runner_terminal_file(claim_path)
    claim_payload = _validate_runner_materialization_claim(
        claim_raw, receipt_public=receipt_public, receipt_id=receipt_id
    )
    expected_claim = {
        "authority_profile": package.PROFILE,
        "claimed_at_epoch": config["transaction_started_at_epoch"],
        "deployment_id": config["deployment_id"],
        "environment": package.ENVIRONMENT,
        "expires_at_epoch": min(
            config["transaction_started_at_epoch"] + MAX_OBSERVATION_SECONDS,
            reservation_payload["expires_at_epoch"],
        ),
        "materialization_parent_identity_sha256": _runner_parent_identity(
            _absolute(materialization_root, "private-root-invalid")
        ),
        "materialization_root": materialization_root,
        "receipt_authority_key_id": receipt_id,
        "release_evidence_sha256": package.sha256(
            package.canonical_json(release_evidence)
        ),
        "reservation_nonce": reservation_payload["reservation_nonce"],
        "reservation_sha256": reservation_binding["reservation_sha256"],
        "runner_prerequisite_approval_payload_sha256": prerequisite_binding[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": prerequisite_binding[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": prerequisite_binding[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": prerequisite_binding[
            "runner_prerequisite_job_id"
        ],
        "runner_label": reservation_binding["runner_label"],
        "runtime_sha": config["runtime_sha"],
        "schema": RUNNER_MATERIALIZATION_CLAIM_SCHEMA,
        "version": 2,
        "workflow_sha": config["workflow_sha"],
    }
    if claim_payload != expected_claim:
        fail("materialization-runner-claim-binding-invalid")
    expected_binding = _runner_materialization_binding_payload(
        materialization_root=materialization_root,
        claim_raw=claim_raw,
        reservation_raw=reservation_raw,
        config=config,
        config_raw=files["authority.v2.json"],
        config_signature=files["authority.v2.sig"],
        plan_raw=files["transaction-plan.v2.json"],
        materialization_receipt_raw=files["materialization-receipt.v2.json"],
        materialization_receipt_signature=files["materialization-receipt.v2.sig"],
        runner_ticket_raw=ticket_raw,
        bound_at=ticket_payload["bound_at_epoch"],
        receipt_id=receipt_id,
    )
    terminal = _runner_terminal_path(reservation_raw)
    terminal_raw: bytes | None = None
    if terminal.exists():
        terminal_raw = _read_runner_terminal_file(terminal)
        terminal_payload = _validate_runner_materialization_binding(
            terminal_raw, receipt_public=receipt_public, receipt_id=receipt_id
        )
        if terminal_payload != expected_binding:
            fail("materialization-runner-bound-terminal-conflict")
    elif require_bound_terminal:
        fail("materialization-runner-bound-terminal-invalid")
    return {
        "config": config,
        "files": files,
        "plan": plan,
        "receipt": receipt,
        "claim_payload": claim_payload,
        "claim_raw": claim_raw,
        "expected_binding": expected_binding,
        "prerequisite_binding": prerequisite_binding,
        "prerequisite_approval_raw": prerequisite_approval_raw,
        "prerequisite_intent_raw": prerequisite_intent_raw,
        "reservation_binding": reservation_binding,
        "reservation_payload": reservation_payload,
        "reservation_raw": reservation_raw,
        "ticket_raw": ticket_raw,
        "terminal_raw": terminal_raw,
    }


def _recover_published_materialization(
    *,
    authority_root: str,
    output: str,
    release_evidence: dict[str, str],
    now: int | None,
) -> dict[str, Any]:
    _validate_release_evidence(release_evidence)
    current = int(time.time()) if now is None else now
    validated = _validated_materialization(
        authority_root=authority_root,
        materialization_root=output,
        current=current,
        require_bound_terminal=False,
        observe_socket=False,
    )
    config = validated["config"]
    receipt = validated["receipt"]
    if (
        config["runtime_sha"] != release_evidence["runtime_sha"]
        or config["workflow_sha"] != release_evidence["workflow_sha"]
        or config["envelope_sha"] != release_evidence["envelope_sha"]
        or config["web_image"] != release_evidence["web_image"]
        or config["render_image"] != release_evidence["render_image"]
        or receipt["image_publication_run_id"]
        != release_evidence["github_run_id"]
        or receipt["image_publication_run_attempt"]
        != release_evidence["github_run_attempt"]
        or receipt["image_publication_run_completed_at_epoch"]
        != int(release_evidence["github_run_completed_at_epoch"])
        or receipt["final_artifact_id"] != release_evidence["final_artifact_id"]
        or receipt["final_artifact_sha256"]
        != release_evidence["final_artifact_sha256"]
        or receipt["preflight_artifact_id"]
        != release_evidence["preflight_artifact_id"]
        or receipt["preflight_artifact_sha256"]
        != release_evidence["preflight_artifact_sha256"]
        or receipt["release_hygiene_sha256"]
        != release_evidence["release_hygiene_sha256"]
        or receipt["render_attestation_id"]
        != release_evidence["render_attestation_id"]
        or receipt["web_attestation_id"]
        != release_evidence["web_attestation_id"]
    ):
        fail("published-materialization-release-evidence-conflict")
    (
        _package_anchor,
        _package_private,
        _package_public,
        receipt_private,
        receipt_public,
        _package_id,
        receipt_id,
    ) = _load_authority(authority_root)
    binding_raw = _signed_runner_record(
        validated["expected_binding"],
        private=receipt_private,
        key_id=receipt_id,
        domain=RUNNER_MATERIALIZATION_BINDING_SIGNATURE_DOMAIN,
    )
    reservation_disposition = _consume_runner_reservation(
        validated["reservation_raw"],
        binding_raw,
        receipt_public=receipt_public,
        receipt_id=receipt_id,
    )
    return {
        "authority_root": authority_root,
        "authoritative": False,
        "config_sha256": receipt["config_sha256"],
        "deployment_id": config["deployment_id"],
        "materialization_root": output,
        "plan_sha256": receipt["plan_sha256"],
        "release_generation": config["release_generation"],
        "root_helper_authorization_required": True,
        "runner_launch_ticket_sha256": receipt["runner_launch_ticket_sha256"],
        "runner_prerequisite_approval_payload_sha256": receipt[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": receipt[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": receipt[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": receipt["runner_prerequisite_job_id"],
        "runner_materialization_claim_disposition": "already-claimed",
        "runner_materialization_claim_sha256": package.sha256(
            validated["claim_raw"]
        ),
        "runner_materialization_binding_sha256": package.sha256(binding_raw),
        "runner_reservation_disposition": reservation_disposition,
        "runner_reservation_sha256": validated["reservation_binding"][
            "reservation_sha256"
        ],
        "runtime_sha": config["runtime_sha"],
        "schema": "propertyquarry.release-control.single-host-production-materialization-result.v2",
        "valid_until_epoch": receipt["valid_until_epoch"],
        "version": 2,
        "workflow_sha": config["workflow_sha"],
    }


def verify_materialization(
    *, authority_root: str, materialization_root: str, now: int | None = None
) -> dict[str, Any]:
    current = int(time.time()) if now is None else now
    validated = _validated_materialization(
        authority_root=authority_root,
        materialization_root=materialization_root,
        current=current,
        require_bound_terminal=True,
        observe_socket=True,
    )
    receipt = validated["receipt"]
    return {
        "config_sha256": receipt["config_sha256"],
        "authoritative": False,
        "fresh": True,
        "materialization_root": materialization_root,
        "plan_sha256": receipt["plan_sha256"],
        "runtime_sha": receipt["runtime_sha"],
        "root_helper_authorization_required": True,
        "runner_launch_ticket_sha256": receipt["runner_launch_ticket_sha256"],
        "runner_prerequisite_approval_payload_sha256": receipt[
            "runner_prerequisite_approval_payload_sha256"
        ],
        "runner_prerequisite_approval_sha256": receipt[
            "runner_prerequisite_approval_sha256"
        ],
        "runner_prerequisite_intent_sha256": receipt[
            "runner_prerequisite_intent_sha256"
        ],
        "runner_prerequisite_job_id": receipt["runner_prerequisite_job_id"],
        "runner_materialization_binding_sha256": package.sha256(
            validated["terminal_raw"]
        ),
        "runner_materialization_claim_sha256": package.sha256(
            validated["claim_raw"]
        ),
        "runner_reservation_sha256": validated["reservation_binding"][
            "reservation_sha256"
        ],
        "schema": "propertyquarry.release-control.single-host-production-materialization-verify-result.v2",
        "valid_until_epoch": receipt["valid_until_epoch"],
        "version": 2,
        "workflow_sha": receipt["workflow_sha"],
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("bootstrap-authority")
    build = commands.add_parser("materialize")
    build.add_argument("--output", required=True)
    build.add_argument("--final-artifact", required=True)
    build.add_argument("--preflight-artifact", required=True)
    build.add_argument("--attestation-verifier-root", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--materialization-root", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "bootstrap-authority":
            result = bootstrap_authority(
                authority_root=os.fspath(PRODUCTION_RECEIPT_AUTHORITY_ROOT),
            )
        elif arguments.command == "materialize":
            started = int(time.time())
            release_evidence = load_release_evidence(
                final_artifact_path=arguments.final_artifact,
                preflight_artifact_path=arguments.preflight_artifact,
                attestation_verifier_root=arguments.attestation_verifier_root,
                now=started,
            )
            result = materialize(
                authority_root=os.fspath(PRODUCTION_RECEIPT_AUTHORITY_ROOT),
                output=arguments.output,
                release_evidence=release_evidence,
                now=started,
            )
        else:
            result = verify_materialization(
                authority_root=os.fspath(PRODUCTION_RECEIPT_AUTHORITY_ROOT),
                materialization_root=arguments.materialization_root,
            )
        sys.stdout.buffer.write(package.canonical_json(result) + b"\n")
        return 0
    except (MaterializeFailure, package.PackageFailure) as error:
        sys.stderr.write(f"propertyquarry-materialization-rejected:{error}\n")
        return 50
    except KeyboardInterrupt:
        sys.stderr.write("propertyquarry-materialization-rejected:interrupted\n")
        return 50
    except Exception:
        sys.stderr.write("propertyquarry-materialization-rejected:internal-failure\n")
        return 50


if __name__ == "__main__":
    raise SystemExit(main())
