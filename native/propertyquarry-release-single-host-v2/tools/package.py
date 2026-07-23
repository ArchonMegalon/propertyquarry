#!/usr/bin/env python3
"""Build, verify, and non-authoritatively stage the single-host v2 package.

The archive is a transport artifact.  Neither a successful build nor a successful
stage operation grants release authority.  A separately trusted root installer
must verify the package again and atomically install it before the controller can
be authoritative.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


SCHEMA = "propertyquarry.release-control.single-host-package.v2"
PROFILE = "single-host-production-v2"
ARCHIVE_FORMAT = "ustar-v1"
NON_AUTHORITATIVE_UNTIL = "independent-root-helper-reverification-and-atomic-install"
MANIFEST_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-package-manifest-signature.v2\x00"
)
CONFIG_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-profile-signature.v2\x00"
)
CONFIG_SCHEMA = "propertyquarry.release-control.single-host-profile.v2"
PLAN_SCHEMA = "propertyquarry.release-control.single-host-transaction-plan.v2"
BUILD_RECEIPT_SCHEMA = (
    "propertyquarry.release-control.single-host-native-build-receipt.v2"
)
MATERIALIZATION_RECEIPT_SCHEMA = (
    "propertyquarry.release-control.single-host-production-materialization.v2"
)
MATERIALIZATION_SIGNATURE_DOMAIN = MATERIALIZATION_RECEIPT_SCHEMA.encode("ascii") + b"\0"
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
RUNNER_LABEL_DERIVATION_DOMAIN = (
    b"propertyquarry.release-control.single-host-runner-label.v2\0"
)
RUNNER_RELEASE_CHECKOUT_ROOT = (
    "/docker/property/state/runtime/propertyquarry-release-authority-v2.private/"
    "single-host-v2-release-checkouts"
)
RUNNER_RESERVATION_TTL_SECONDS = 6 * 60 * 60
RUNNER_TICKET_TTL_SECONDS = 30 * 60
MAX_MATERIALIZATION_OBSERVATION_SECONDS = 900
MAX_PUBLICATION_EVIDENCE_AGE_SECONDS = 21_600
REPOSITORY = "ArchonMegalon/propertyquarry"
REPOSITORY_ID = "1257593732"
REPOSITORY_OWNER_ID = "11421547"
WORKFLOW_REF = (
    "ArchonMegalon/propertyquarry/.github/workflows/"
    "smoke-runtime.yml@refs/heads/main"
)
RELEASE_JOB = "propertyquarry-release-v2"
RUNNER_PREREQUISITE_JOB = "propertyquarry-protected-dispatch-inputs"
ENVIRONMENT = "propertyquarry-production"
PROJECT_NAME = "property"
PUBLIC_ORIGIN = "https://propertyquarry.com"
API_HOST_IP = "127.0.0.1"
API_HOST_PORT = 8097
API_CONTAINER_PORT = 8090
DATABASE_IMAGE = (
    "postgres:16-alpine@sha256:"
    "16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
)
IDENTITY_ENV_PATH = (
    "/docker/property/state/runtime/propertyquarry_google_identity.env"
)
REGISTRATION_EMAIL_ENV_PATH = (
    "/docker/property/state/runtime/propertyquarry_registration_email.env"
)
SCENE_VIDEO_ENV_PATH = (
    "/docker/property/state/runtime/property_scene_video_shared.env"
)
BASE_ENV_PATH = "/docker/property/.env"
DATABASE_ROLES_ENV_PATH = (
    "/docker/property/state/runtime/propertyquarry_database_roles.env"
)
ADMISSION_ENV_PATH = (
    "/docker/property/state/runtime/propertyquarry_admission.env"
)
MANIFEST_INSTALL_PATH = (
    "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.json"
)
MANIFEST_SIGNATURE_INSTALL_PATH = (
    "/etc/propertyquarry-release-single-host-v2/package-manifest.v2.sig"
)
RUNNER_LIFECYCLE_INSTALL_PATH = (
    "/usr/libexec/propertyquarry-release-control/"
    "run-propertyquarry-ephemeral-runner-lifecycle-v2"
)
TEMPLATE_DIRECTORY = Path(__file__).resolve().parent.parent / "packaging" / "templates"
MODULE_DIRECTORY = Path(__file__).resolve().parent.parent

MAX_JSON_BYTES = 1_048_576
MAX_BINARY_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_BYTES = 320 * 1024 * 1024
MAX_MEMBERS = 64
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
DEPLOYMENT_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ENVELOPE_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
NUMERIC_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,19}$")
IMAGE_PATTERN = re.compile(
    r"^ghcr\.io/archonmegalon/propertyquarry-standalone-"
    r"(web|render)-runtime@sha256:[0-9a-f]{64}$"
)
CLOUDFLARED_IMAGE_PATTERN = re.compile(
    r"^cloudflare/cloudflared@sha256:[0-9a-f]{64}$"
)
EXECUTABLE_PATTERN = re.compile(
    r"^/(usr/(bin|sbin|libexec/propertyquarry-release-control)|bin|sbin)/"
    r"[A-Za-z0-9._/+:-]+$"
)
STEP_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
REQUIRED_RELEASE_STEP_IDS = (
    "predeploy-encrypted-backup",
    "purge-propertyquarry-legacy-runtime-exposure",
    "retire-stale-propertyquarry-runtime",
    "provision-propertyquarry-database-roles",
    "migrate-propertyquarry-schema",
    "harden-propertyquarry-runtime-acl",
    "verify-propertyquarry-schema-readiness",
    "deploy-propertyquarry-runtime",
)
VERIFY_ISOLATION_INPUTS_STEP_ID = "verify-propertyquarry-isolation-inputs"
TERMINAL_ISOLATION_VERIFY_STEP_ID = "verify-propertyquarry-runtime-isolation"
ROLLBACK_ISOLATION_STEP_ID = "restore-propertyquarry-legacy-runtime-exposure"
BACKUP_MAX_AGE_SECONDS = 3600
PREDEPLOY_BACKUP_HELPER_PATH = (
    "/usr/libexec/propertyquarry-release-control/propertyquarry-predeploy-backup-v2"
)
PREDEPLOY_BACKUP_HELPER_SHA256 = (
    "sha256:a7a877b6aae97628892f9c603eddc8267625689676a0daf4685de65613be56d3"
)
PREDEPLOY_BACKUP_HELPER_BYTES = 91_482
DATABASE_CONTROL_HELPER_PATH = (
    "/usr/libexec/propertyquarry-release-control/propertyquarry-database-control-v2"
)
DATABASE_CONTROL_HELPER_SHA256 = (
    "sha256:9bdebcd2bae867ef9ac4e38374e964dc81752b2a572eb8a0568f3bb45d5bfe18"
)
DATABASE_CONTROL_HELPER_BYTES = 60_449
RUNTIME_DATABASE_HELPER_PATH = (
    "/usr/libexec/propertyquarry-release-control/"
    "provision_propertyquarry_runtime_database.py"
)
RUNTIME_DATABASE_HELPER_SHA256 = (
    "sha256:bc987570cfce12c734cb80b33d7e13199b346c8a8b5406f3ebce88bb15e71a63"
)
RUNTIME_DATABASE_HELPER_BYTES = 50_770
DATABASE_RECEIPT_ROOT = (
    "/var/lib/propertyquarry-release-single-host-v2/database-receipts"
)
RUNTIME_ISOLATION_HELPER_PATH = (
    "/usr/libexec/propertyquarry-release-control/"
    "propertyquarry-runtime-isolation-v2"
)
RUNTIME_ISOLATION_HELPER_SHA256 = (
    "sha256:a441c978b1fec877d27828f264f35a5dfa203999a8b1260b06ee12fb6f45c413"
)
RUNTIME_ISOLATION_HELPER_BYTES = 161_070
RUNTIME_ISOLATION_RECEIPT_ROOT = (
    "/var/lib/propertyquarry-release-single-host-v2/isolation-receipts"
)
RUNTIME_DEPLOY_HELPER_PATH = (
    "/usr/libexec/propertyquarry-release-control/propertyquarry-runtime-deploy-v2"
)
RUNTIME_DEPLOY_HELPER_SHA256 = (
    "sha256:a762c418ffa83aac86b8b503dbd6e9c0ccf41cbc37cd72b21931a9781090691c"
)
RUNTIME_DEPLOY_HELPER_BYTES = 82_995
RUNTIME_DEPLOY_RECEIPT_ROOT = (
    "/var/lib/propertyquarry-release-single-host-v2/deploy-receipts"
)
DOCKER_EXECUTABLE_PATH = "/usr/bin/docker"
DOCKER_COMPOSE_PLUGIN_PATH = "/usr/libexec/docker/cli-plugins/docker-compose"
PROPERTY_COMPOSE_PATH = "/docker/property/docker-compose.property.yml"
CLOUDFLARED_COMPOSE_PATH = "/docker/property/docker-compose.cloudflared.yml"
BACKUP_ENCRYPTION_KEY_PATH = (
    "/home/tibor/.local/share/propertyquarry-backup-keys/"
    "propertyquarry-predeploy-backup-v2.key"
)
BACKUP_RECEIPT_ROOT = (
    "/var/lib/propertyquarry-release-single-host-v2/backup-receipts"
)
DATABASE_RUNTIME_DIRECTORY = "/docker/property/state/runtime"
RUNTIME_INPUT_PATHS = (
    BASE_ENV_PATH,
    SCENE_VIDEO_ENV_PATH,
    DATABASE_ROLES_ENV_PATH,
    ADMISSION_ENV_PATH,
    IDENTITY_ENV_PATH,
    REGISTRATION_EMAIL_ENV_PATH,
)
DESIRED_RUNTIME_CONTAINER_ALLOWLIST = (
    "propertyquarry-api-live",
    "propertyquarry-cloudflared-live",
    "propertyquarry-db-live",
    "propertyquarry-migrate-live",
    "propertyquarry-render-live",
    "propertyquarry-scheduler-live",
    "propertyquarry-worker-live",
)
DATABASE_CONTAINER_NAME = "propertyquarry-db-live"
DATABASE_NAME = "propertyquarry"
DATABASE_PGDATA_VOLUME_NAME = "property_propertyquarry_pgdata"
DATABASE_PGDATA_VOLUME_MOUNTPOINT = (
    "/var/lib/docker/volumes/property_propertyquarry_pgdata/_data"
)
RUNTIME_CONTAINER_NAME_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
)
RUNTIME_CREATED_AT_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z?$")
RUNTIME_VOLUME_CREATED_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+ -]+Z?$"
)
STRING_MAP_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}$")
LEGACY_RUNTIME_NAME_PATTERNS = (
    re.compile(r"^propertyquarry-(?:api|cloudflared|migrate|render-tools|scheduler|worker)$"),
    re.compile(r"^propertyquarry-(?:api|db|migrate|render)-[0-9a-f]{8}$"),
    re.compile(r"^propertyquarry-admission-audit-[0-9a-f]{8}$"),
    re.compile(r"^propertyquarry-release-pin-(?:render|web)-[0-9]+$"),
    re.compile(r"^pq-ai-panorama-(?:canonical-strict|prater-preflight)$"),
)


class PackageFailure(Exception):
    """A deliberately terse, non-secret-bearing package error."""


@dataclass(frozen=True)
class Payload:
    install_path: str
    purpose: str
    mode: int
    data: bytes

    @property
    def package_path(self) -> str:
        return "payload" + self.install_path


@dataclass(frozen=True)
class VerifiedPackage:
    archive_sha256: str
    manifest_sha256: str
    manifest: dict[str, Any]
    members: dict[str, bytes]
    modes: dict[str, int]


PAYLOAD_LAYOUT: dict[str, tuple[str, int]] = {
    "/usr/libexec/propertyquarry-release-control/"
    "propertyquarry-release-single-host-v2": ("controller-binary", 0o755),
    "/etc/propertyquarry-release-single-host-v2/native-build-receipt.v2.json": (
        "native-build-receipt",
        0o444,
    ),
    "/etc/propertyquarry-release-single-host-v2/authority.v2.json": (
        "signed-authority-profile",
        0o400,
    ),
    "/etc/propertyquarry-release-single-host-v2/authority.v2.sig": (
        "authority-profile-signature",
        0o444,
    ),
    "/etc/propertyquarry-release-single-host-v2/transaction-plan.v2.json": (
        "signed-by-profile-transaction-plan",
        0o444,
    ),
    "/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.json": (
        "package-authority-signed-materialization-receipt",
        0o444,
    ),
    "/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.sig": (
        "materialization-receipt-signature",
        0o444,
    ),
    "/etc/propertyquarry-release-single-host-v2/package-authority-v2.pem": (
        "package-authority-anchor",
        0o444,
    ),
    "/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key": (
        "receipt-authority-private-key",
        0o400,
    ),
    "/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem": (
        "receipt-authority-anchor",
        0o444,
    ),
    "/var/lib/propertyquarry-release-single-host-v2/runner-launch-ticket.v2.json": (
        "ephemeral-runner-launch-ticket",
        0o400,
    ),
    "/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json": (
        "ephemeral-runner-reservation",
        0o400,
    ),
    "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-intent.v2.json": (
        "ephemeral-runner-prerequisite-approval-intent",
        0o400,
    ),
    "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-approval.v2.json": (
        "ephemeral-runner-prerequisite-approval-proof",
        0o400,
    ),
    "/usr/lib/systemd/system/propertyquarry-release-single-host-v2.socket": (
        "systemd-socket-unit",
        0o444,
    ),
    "/usr/lib/systemd/system/propertyquarry-release-single-host-v2@.service": (
        "systemd-service-template",
        0o444,
    ),
    "/usr/lib/systemd/system/"
    "propertyquarry-release-single-host-v2-activation-canary.service": (
        "systemd-activation-canary-unit",
        0o444,
    ),
    "/usr/lib/sysusers.d/propertyquarry-release-single-host-v2.conf": (
        "sysusers-definition",
        0o444,
    ),
    "/usr/lib/tmpfiles.d/propertyquarry-release-single-host-v2.conf": (
        "tmpfiles-definition",
        0o444,
    ),
    "/usr/lib/propertyquarry-release-runner-v2/runner.lock.json": (
        "ephemeral-runner-lock",
        0o444,
    ),
    "/usr/libexec/propertyquarry-release-control/"
    "run-propertyquarry-ephemeral-runner-v2": (
        "ephemeral-runner-launcher",
        0o555,
    ),
    RUNNER_LIFECYCLE_INSTALL_PATH: (
        "ephemeral-runner-root-lifecycle",
        0o555,
    ),
    PREDEPLOY_BACKUP_HELPER_PATH: (
        "predeploy-backup-helper",
        0o755,
    ),
    DATABASE_CONTROL_HELPER_PATH: ("database-control-helper", 0o755),
    RUNTIME_DATABASE_HELPER_PATH: ("runtime-database-helper", 0o755),
    RUNTIME_ISOLATION_HELPER_PATH: ("runtime-isolation-helper", 0o755),
    RUNTIME_DEPLOY_HELPER_PATH: ("runtime-deploy-helper", 0o755),
}


def fail(code: str) -> None:
    raise PackageFailure(code)


def sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def validate_sealed_helper(
    raw: bytes,
    *,
    expected_sha256: str,
    expected_bytes: int,
    label: str,
) -> None:
    if len(raw) != expected_bytes or sha256(raw) != expected_sha256:
        fail(f"{label}-sealed-bytes-invalid")


def framed(domain: bytes, raw: bytes) -> bytes:
    return domain + len(raw).to_bytes(8, "big") + raw


def read_regular(
    path_value: str | Path,
    maximum: int,
    *,
    private: bool = False,
    expected_modes: tuple[int, ...] | None = None,
) -> bytes:
    path = os.fspath(path_value)
    try:
        before = os.lstat(path)
    except OSError:
        fail("input-unavailable")
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > maximum
    ):
        fail("input-metadata-invalid")
    if expected_modes is not None and stat.S_IMODE(before.st_mode) not in expected_modes:
        fail("input-mode-invalid")
    if private and stat.S_IMODE(before.st_mode) not in (0o400, 0o600):
        fail("private-input-mode-invalid")
    flags = os.O_RDONLY | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        fail("input-open-failed")
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            fail("input-changed")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                fail("input-short-read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            fail("input-changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            fail("json-duplicate-key")
        result[key] = value
    return result


def _reject_float(_: str) -> Any:
    fail("json-number-invalid")


def _reject_constant(_: str) -> Any:
    fail("json-number-invalid")


def _json_string(value: str) -> str:
    output: list[str] = ['"']
    short = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\f": "\\f",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    for character in value:
        codepoint = ord(character)
        if 0xD800 <= codepoint <= 0xDFFF:
            fail("json-string-invalid")
        if character in short:
            output.append(short[character])
        elif codepoint < 0x20 or codepoint > 0x7E:
            if codepoint > 0xFFFF:
                adjusted = codepoint - 0x10000
                output.append(f"\\u{0xD800 + (adjusted >> 10):04x}")
                output.append(f"\\u{0xDC00 + (adjusted & 0x3FF):04x}")
                continue
            output.append(f"\\u{codepoint:04x}")
        else:
            output.append(character)
    output.append('"')
    return "".join(output)


def _canonical_fragment(value: Any, depth: int = 0) -> str:
    if depth > 32:
        fail("json-depth-invalid")
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0 or value > (1 << 63) - 1:
            fail("json-number-invalid")
        return str(value)
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, list):
        return "[" + ",".join(
            _canonical_fragment(child, depth + 1) for child in value
        ) + "]"
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            fail("json-key-invalid")
        return "{" + ",".join(
            _json_string(key) + ":" + _canonical_fragment(value[key], depth + 1)
            for key in sorted(value)
        ) + "}"
    fail("json-type-invalid")


def canonical_json(value: Any) -> bytes:
    return _canonical_fragment(value).encode("utf-8")


def parse_strict_json(
    raw: bytes,
    label: str,
    *,
    trailing_newline: bool = False,
) -> dict[str, Any]:
    if not raw or len(raw) > MAX_JSON_BYTES:
        fail(f"{label}-size-invalid")
    material = raw
    if trailing_newline:
        if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
            fail(f"{label}-newline-invalid")
        material = raw[:-1]
    try:
        text = material.decode("utf-8", "strict")
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_int=int,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except PackageFailure:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        fail(f"{label}-json-invalid")
    if not isinstance(value, dict):
        fail(f"{label}-object-required")
    expected = canonical_json(value) + (b"\n" if trailing_newline else b"")
    if expected != raw:
        fail(f"{label}-not-canonical")
    return value


def _require_keys(value: dict[str, Any], required: Iterable[str], label: str) -> None:
    if set(value) != set(required):
        fail(f"{label}-shape-invalid")


def _string(value: dict[str, Any], key: str, label: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        fail(f"{label}-{key}-invalid")
    return result


def _integer(
    value: dict[str, Any], key: str, minimum: int, maximum: int, label: str
) -> int:
    result = value.get(key)
    if (
        not isinstance(result, int)
        or isinstance(result, bool)
        or result < minimum
        or result > maximum
    ):
        fail(f"{label}-{key}-invalid")
    return result


def load_public_key(raw: bytes, label: str) -> tuple[Ed25519PublicKey, bytes, str]:
    try:
        key = serialization.load_pem_public_key(raw)
    except (TypeError, ValueError, UnsupportedAlgorithm):
        fail(f"{label}-invalid")
    if not isinstance(key, Ed25519PublicKey):
        fail(f"{label}-type-invalid")
    canonical = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if canonical != raw:
        fail(f"{label}-not-canonical")
    der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, canonical, sha256(der)


def load_private_key(raw: bytes, label: str) -> tuple[Ed25519PrivateKey, bytes]:
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, ValueError, UnsupportedAlgorithm):
        fail(f"{label}-invalid")
    if not isinstance(key, Ed25519PrivateKey):
        fail(f"{label}-type-invalid")
    canonical = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    if canonical != raw:
        fail(f"{label}-not-canonical")
    return key, canonical


def validate_static_elf(binary: bytes) -> None:
    if len(binary) < 64 or len(binary) > MAX_BINARY_BYTES:
        fail("binary-size-invalid")
    if binary[:16] != b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8:
        fail("binary-elf-ident-invalid")
    try:
        header = struct.unpack("<HHIQQQIHHHHHH", binary[16:64])
    except struct.error:
        fail("binary-elf-header-invalid")
    (
        elf_type,
        machine,
        version,
        entry,
        program_offset,
        _section_offset,
        _flags,
        header_size,
        program_entry_size,
        program_count,
        _section_entry_size,
        _section_count,
        _section_names,
    ) = header
    if (
        elf_type != 2
        or machine != 62
        or version != 1
        or entry == 0
        or header_size != 64
        or program_entry_size != 56
        or program_count < 1
        or program_count > 256
        or program_offset < 64
        or program_offset + program_entry_size * program_count > len(binary)
    ):
        fail("binary-elf-contract-invalid")
    found_load = False
    found_stack = False
    for index in range(program_count):
        offset = program_offset + index * program_entry_size
        try:
            program = struct.unpack("<IIQQQQQQ", binary[offset : offset + 56])
        except struct.error:
            fail("binary-elf-program-header-invalid")
        program_type, program_flags = program[0], program[1]
        if program_type in (2, 3):
            fail("binary-not-static")
        if program_type == 1:
            found_load = True
        if program_type == 0x6474E551:
            found_stack = True
            if program_flags & 1:
                fail("binary-executable-stack")
    if not found_load or not found_stack:
        fail("binary-elf-segments-invalid")


def validate_build_receipt(
    receipt: dict[str, Any], binary: bytes, package_key_id: str
) -> None:
    required = {
        "authoritative",
        "binary_mode",
        "binary_sha256",
        "binary_size",
        "build_flags",
        "go_tests_passed_in_both_builds",
        "host_network_namespace_isolated",
        "independent_toolchain_extractions",
        "installer_binary_mode",
        "installer_binary_sha256",
        "installer_binary_size",
        "installer_package_authority_bound",
        "installer_package_authority_key_id",
        "module_network_resolution_disabled",
        "package_signature_verified",
        "performs_release_effects",
        "production_ready",
        "receipt_published_last",
        "reproducible_double_build",
        "root_install_performed",
        "schema",
        "scratch_execution_contract",
        "source_manifest_digest",
        "static_elf_verified_in_both_builds",
        "toolchain",
        "toolchain_archive_bytes",
        "toolchain_archive_sha256",
        "version",
    }
    if set(receipt) != required:
        fail("build-receipt-shape-invalid")
    if (
        receipt["schema"] != BUILD_RECEIPT_SCHEMA
        or receipt["version"] != 2
        or receipt["authoritative"] is not False
        or receipt["production_ready"] is not False
        or receipt["performs_release_effects"] is not False
        or receipt["package_signature_verified"] is not False
        or receipt["root_install_performed"] is not False
        or receipt["reproducible_double_build"] is not True
        or receipt["independent_toolchain_extractions"] is not True
        or receipt["module_network_resolution_disabled"] is not True
        or receipt["go_tests_passed_in_both_builds"] is not True
        or receipt["static_elf_verified_in_both_builds"] is not True
        or receipt["receipt_published_last"] is not True
        or receipt["host_network_namespace_isolated"] is not False
        or receipt["binary_mode"] != "0755"
        or receipt["binary_size"] != len(binary)
        or receipt["binary_sha256"] != sha256(binary)
        or not isinstance(receipt["source_manifest_digest"], str)
        or not SHA256_PATTERN.fullmatch(receipt["source_manifest_digest"])
        or receipt["scratch_execution_contract"]
        != "linux-amd64-static-et-exec-v1"
        or receipt["toolchain"] != "go1.26.5 linux/amd64"
        or receipt["toolchain_archive_bytes"] != 66879095
        or receipt["toolchain_archive_sha256"]
        != "5c2c3b16caefa1d968a94c1daca04a7ca301a496d9b086e17ad77bb81393f053"
        or receipt["build_flags"]
        != ["-mod=readonly", "-trimpath", "-buildvcs=false", "-buildmode=exe"]
    ):
        fail("build-receipt-binding-invalid")
    installer_sha256 = receipt["installer_binary_sha256"]
    installer_size = receipt["installer_binary_size"]
    installer_bound = receipt["installer_package_authority_bound"]
    installer_key_id = receipt["installer_package_authority_key_id"]
    if (
        receipt["installer_binary_mode"] != "0555"
        or not isinstance(installer_sha256, str)
        or not SHA256_PATTERN.fullmatch(installer_sha256)
        or not isinstance(installer_size, int)
        or isinstance(installer_size, bool)
        or not 1 <= installer_size <= MAX_BINARY_BYTES
        or not isinstance(installer_bound, bool)
        or not isinstance(installer_key_id, str)
        or (
            installer_bound
            and (
                not SHA256_PATTERN.fullmatch(installer_key_id)
                or installer_key_id != package_key_id
            )
        )
        or (not installer_bound and installer_key_id != "unbound")
    ):
        fail("build-receipt-installer-binding-invalid")
    validate_static_elf(binary)


def _forbidden_argument_text(value: str) -> bool:
    return "\x00" in value or "\n" in value or "\r" in value


def _canonical_digest(value: Any) -> str:
    return sha256(canonical_json(value))


def validate_signed_runtime_inputs(
    pre_value: Any, post_value: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    def parse(value: Any, label: str) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) != len(RUNTIME_INPUT_PATHS):
            fail(f"{label}-shape-invalid")
        observations: list[dict[str, Any]] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                fail(f"{label}-entry-invalid")
            _require_keys(
                item,
                {"gid", "mode", "path", "sha256", "size", "uid"},
                f"{label}-entry",
            )
            if (
                _string(item, "path", label) != RUNTIME_INPUT_PATHS[index]
                or _integer(item, "mode", 384, 384, label) != 384
                or _integer(item, "uid", 1000, 1000, label) != 1000
                or _integer(item, "gid", 1000, 1000, label) != 1000
                or not SHA256_PATTERN.fullmatch(_string(item, "sha256", label))
            ):
                fail(f"{label}-binding-invalid")
            _integer(item, "size", 1, 256 * 1024, label)
            observations.append(item)
        return observations

    pre = parse(pre_value, "pre-purge-runtime-inputs")
    post = parse(post_value, "runtime-inputs")
    if (
        pre[0]["path"] != BASE_ENV_PATH
        or post[0]["path"] != BASE_ENV_PATH
        or pre[0]["mode"] != post[0]["mode"]
        or pre[0]["uid"] != post[0]["uid"]
        or pre[0]["gid"] != post[0]["gid"]
        or pre[0]["sha256"] == post[0]["sha256"]
        or post[0]["size"] >= pre[0]["size"]
    ):
        fail("runtime-input-root-transition-invalid")
    if any(pre[index] != post[index] for index in range(1, len(pre))):
        fail("runtime-input-transition-invalid")
    return pre, post


def _valid_sorted_unique_strings(value: Any, maximum_length: int) -> bool:
    if not isinstance(value, list) or len(value) > 64:
        return False
    strings: list[str] = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or len(item) > maximum_length
            or _forbidden_argument_text(item)
        ):
            return False
        strings.append(item)
    return strings == sorted(set(strings))


def _valid_retirement_mounts(value: Any) -> bool:
    if not isinstance(value, list) or len(value) > 64:
        return False
    previous = b""
    for mount in value:
        if not isinstance(mount, dict) or set(mount) != {
            "destination",
            "driver",
            "mode",
            "name",
            "propagation",
            "rw",
            "source",
            "type",
        }:
            return False
        destination = mount.get("destination")
        kind = mount.get("type")
        if (
            not isinstance(destination, str)
            or not destination
            or not destination.startswith("/")
            or not isinstance(kind, str)
            or not kind
            or kind not in ("bind", "tmpfs", "volume")
            or type(mount.get("rw")) is not bool
        ):
            return False
        for key in ("driver", "mode", "name", "propagation", "source"):
            if not isinstance(mount.get(key), str):
                return False
        name = mount["name"]
        source = mount["source"]
        if (
            (kind == "volume" and (not name or source != name))
            or (kind != "volume" and name != "")
            or (kind != "volume" and not source.startswith("/"))
        ):
            return False
        for text in (
            destination,
            mount["driver"],
            mount["mode"],
            name,
            mount["propagation"],
            source,
            kind,
        ):
            if len(text) > 4096 or _forbidden_argument_text(text):
                return False
        raw = canonical_json(mount)
        if previous and raw <= previous:
            return False
        previous = raw
    return True


def validate_runtime_retirement_contract(
    value: Any, runtime_sha: str, deployment_id: str
) -> str:
    if not isinstance(value, dict):
        fail("runtime-retirement-contract-invalid")
    _require_keys(
        value,
        {
            "containers",
            "deployment_id",
            "desired_live_allowlist",
            "operation",
            "preserve_volumes",
            "receipt_path",
        },
        "runtime-retirement-contract",
    )
    operation = "retire-stale-propertyquarry-runtime"
    expected_receipt = (
        f"{RUNTIME_ISOLATION_RECEIPT_ROOT}/{runtime_sha}/{deployment_id}/"
        f"{operation}.json"
    )
    if (
        value.get("operation") != operation
        or value.get("deployment_id") != deployment_id
        or value.get("preserve_volumes") is not True
        or value.get("receipt_path") != expected_receipt
        or value.get("desired_live_allowlist")
        != list(DESIRED_RUNTIME_CONTAINER_ALLOWLIST)
    ):
        fail("runtime-retirement-contract-invalid")
    containers = value.get("containers")
    if not isinstance(containers, list) or len(containers) > 64:
        fail("runtime-retirement-containers-invalid")
    previous_name = ""
    seen: set[str] = set()
    for container in containers:
        if not isinstance(container, dict) or set(container) != {
            "compose_project",
            "compose_service",
            "container_id",
            "created_at",
            "image",
            "image_id",
            "mounts",
            "name",
            "networks",
        }:
            fail("runtime-retirement-container-invalid")
        name = container.get("name")
        container_id = container.get("container_id")
        created_at = container.get("created_at")
        image = container.get("image")
        image_id = container.get("image_id")
        project = container.get("compose_project")
        service = container.get("compose_service")
        if (
            not isinstance(name, str)
            or not RUNTIME_CONTAINER_NAME_PATTERN.fullmatch(name)
            or name <= previous_name
            or name in seen
            or name in DESIRED_RUNTIME_CONTAINER_ALLOWLIST
            or not any(pattern.fullmatch(name) for pattern in LEGACY_RUNTIME_NAME_PATTERNS)
            or not isinstance(container_id, str)
            or not DEPLOYMENT_ID_PATTERN.fullmatch(container_id)
            or not isinstance(created_at, str)
            or not RUNTIME_CREATED_AT_PATTERN.fullmatch(created_at)
            or not isinstance(image, str)
            or not image
            or len(image) > 512
            or not isinstance(image_id, str)
            or not SHA256_PATTERN.fullmatch(image_id)
            or not isinstance(project, str)
            or len(project) > 255
            or _forbidden_argument_text(project)
            or not isinstance(service, str)
            or len(service) > 255
            or _forbidden_argument_text(service)
            or not _valid_sorted_unique_strings(container.get("networks"), 255)
            or not _valid_retirement_mounts(container.get("mounts"))
        ):
            fail("runtime-retirement-container-invalid")
        seen.add(name)
        previous_name = name
    return _canonical_digest(value)


def expected_compose_argv() -> list[str]:
    return [
        DOCKER_EXECUTABLE_PATH,
        "compose",
        "--ansi",
        "never",
        "--progress",
        "quiet",
        "--project-name",
        PROJECT_NAME,
        "--project-directory",
        "/docker/property",
        "--env-file",
        BASE_ENV_PATH,
        "--env-file",
        SCENE_VIDEO_ENV_PATH,
        "--env-file",
        DATABASE_ROLES_ENV_PATH,
        "--env-file",
        ADMISSION_ENV_PATH,
        "--env-file",
        IDENTITY_ENV_PATH,
        "--env-file",
        REGISTRATION_EMAIL_ENV_PATH,
        "--file",
        PROPERTY_COMPOSE_PATH,
        "--file",
        CLOUDFLARED_COMPOSE_PATH,
        "up",
        "--detach",
        "--pull",
        "always",
        "--quiet-pull",
        "--no-build",
        "--timeout",
        "120",
        "--wait",
        "--wait-timeout",
        "900",
    ]


def _valid_runtime_file_observation(
    value: Any, expected_path: str, executable: bool
) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "gid",
        "mode",
        "path",
        "sha256",
        "size",
        "uid",
    }:
        return False
    digest_value = value.get("sha256")
    mode = value.get("mode")
    uid = value.get("uid")
    gid = value.get("gid")
    size = value.get("size")
    if (
        value.get("path") != expected_path
        or not isinstance(digest_value, str)
        or not SHA256_PATTERN.fullmatch(digest_value)
        or not isinstance(mode, str)
        or not re.fullmatch(r"^0[4567][0-7]{2}$", mode)
        or not isinstance(uid, int)
        or isinstance(uid, bool)
        or not 0 <= uid <= (1 << 31) - 1
        or not isinstance(gid, int)
        or isinstance(gid, bool)
        or not 0 <= gid <= (1 << 31) - 1
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not 1 <= size <= 256 * 1024 * 1024
    ):
        return False
    if executable:
        return mode == "0755" and uid == 0 and gid == 0
    return (
        mode in ("0400", "0444", "0600", "0644")
        and uid in (0, 1000)
        and gid in (0, 1000)
    )


def validate_runtime_deploy_contract(
    value: Any, runtime_sha: str, deployment_id: str
) -> str:
    if not isinstance(value, dict):
        fail("runtime-deploy-contract-invalid")
    _require_keys(
        value,
        {
            "compose_argv",
            "compose_files",
            "compose_plugin",
            "deployment_id",
            "docker_executable",
            "env_files",
            "operation",
            "receipt_path",
        },
        "runtime-deploy-contract",
    )
    expected_receipt = (
        f"{RUNTIME_DEPLOY_RECEIPT_ROOT}/{runtime_sha}/{deployment_id}/"
        "deploy-runtime.json"
    )
    if (
        value.get("operation") != "deploy-runtime"
        or value.get("deployment_id") != deployment_id
        or value.get("receipt_path") != expected_receipt
        or value.get("env_files") != list(RUNTIME_INPUT_PATHS)
        or value.get("compose_argv") != expected_compose_argv()
    ):
        fail("runtime-deploy-contract-invalid")
    if not _valid_runtime_file_observation(
        value.get("docker_executable"), DOCKER_EXECUTABLE_PATH, True
    ) or not _valid_runtime_file_observation(
        value.get("compose_plugin"), DOCKER_COMPOSE_PLUGIN_PATH, True
    ):
        fail("runtime-deploy-executable-invalid")
    compose_files = value.get("compose_files")
    if (
        not isinstance(compose_files, list)
        or len(compose_files) != 2
        or not _valid_runtime_file_observation(
            compose_files[0], PROPERTY_COMPOSE_PATH, False
        )
        or not _valid_runtime_file_observation(
            compose_files[1], CLOUDFLARED_COMPOSE_PATH, False
        )
    ):
        fail("runtime-deploy-compose-files-invalid")
    return _canonical_digest(value)


def _valid_string_map(value: Any) -> bool:
    if not isinstance(value, dict) or len(value) > 64:
        return False
    for key, raw in value.items():
        if (
            not isinstance(key, str)
            or not STRING_MAP_KEY_PATTERN.fullmatch(key)
            or not isinstance(raw, str)
            or len(raw) > 4096
            or _forbidden_argument_text(raw)
        ):
            return False
    return True


def validate_database_substrate(value: Any, database_image: str) -> str:
    if not isinstance(value, dict) or not isinstance(database_image, str):
        fail("database-substrate-shape-invalid")
    _require_keys(
        value,
        {
            "container_id",
            "container_name",
            "database",
            "database_oid",
            "image",
            "image_id",
            "pgdata_volume",
            "repo_digest",
        },
        "database-substrate",
    )
    container_id = value.get("container_id")
    image_id = value.get("image_id")
    expected_repo_digest = "postgres@sha256:" + database_image.rsplit(
        "@sha256:", 1
    )[-1]
    volume = value.get("pgdata_volume")
    if (
        not isinstance(container_id, str)
        or not DEPLOYMENT_ID_PATTERN.fullmatch(container_id)
        or value.get("container_name") != DATABASE_CONTAINER_NAME
        or value.get("database") != DATABASE_NAME
        or _integer(value, "database_oid", 1, 1 << 62, "database-substrate") < 1
        or value.get("image") != database_image
        or not isinstance(image_id, str)
        or not SHA256_PATTERN.fullmatch(image_id)
        or value.get("repo_digest") != expected_repo_digest
        or not isinstance(volume, dict)
    ):
        fail("database-substrate-binding-invalid")
    _require_keys(
        volume,
        {"created_at", "driver", "labels", "mountpoint", "name", "options", "scope"},
        "database-pgdata-volume",
    )
    created_at = volume.get("created_at")
    if (
        not isinstance(created_at, str)
        or not RUNTIME_VOLUME_CREATED_PATTERN.fullmatch(created_at)
        or volume.get("driver") != "local"
        or volume.get("mountpoint") != DATABASE_PGDATA_VOLUME_MOUNTPOINT
        or volume.get("name") != DATABASE_PGDATA_VOLUME_NAME
        or volume.get("scope") != "local"
        or not _valid_string_map(volume.get("labels"))
        or volume.get("labels", {}).get("com.docker.compose.project")
        != PROJECT_NAME
        or volume.get("labels", {}).get("com.docker.compose.volume")
        != "propertyquarry_pgdata"
        or volume.get("options") != {}
    ):
        fail("database-substrate-binding-invalid")
    return _canonical_digest(value)


def validate_config_and_plan(
    config_raw: bytes,
    config_signature: bytes,
    plan_raw: bytes,
    package_public: Ed25519PublicKey,
    package_key_id: str,
    receipt_public: Ed25519PublicKey,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    if len(config_signature) != 64:
        fail("config-signature-size-invalid")
    try:
        package_public.verify(
            config_signature, framed(CONFIG_SIGNATURE_DOMAIN, config_raw)
        )
    except InvalidSignature:
        fail("config-signature-invalid")
    config = parse_strict_json(config_raw, "config")
    config_required = {
        "allowed_runner_gid",
        "allowed_runner_uid",
        "api_container_port",
        "api_host_ip",
        "api_host_port",
        "authority_profile",
        "backup_max_age_seconds",
        "cloudflared_image",
        "database_image",
        "database_substrate",
        "database_substrate_digest",
        "deployment_id",
        "envelope_sha",
        "environment",
        "ephemeral_runner_label_prefix",
        "github_api_credential_path",
        "github_identity_env_digest",
        "github_identity_env_gid",
        "github_identity_env_mode",
        "github_identity_env_path",
        "github_identity_env_uid",
        "github_oidc_request_origin",
        "host_machine_id_digest",
        "package_authority_key_id",
        "plan_digest",
        "post_purge_root_env_digest",
        "pre_purge_root_env_digest",
        "pre_purge_runtime_inputs",
        "predecessor_runtime_sha",
        "preflight_ttl_seconds",
        "project_name",
        "public_origin",
        "receipt_authority_key_id",
        "registration_email_env_digest",
        "registration_email_env_gid",
        "registration_email_env_mode",
        "registration_email_env_path",
        "registration_email_env_uid",
        "release_generation",
        "release_job",
        "render_image",
        "repository",
        "repository_id",
        "repository_owner_id",
        "runner_job_id",
        "runner_label",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256",
        "runner_prerequisite_intent_sha256",
        "runner_prerequisite_job_id",
        "runner_reservation_sha256",
        "runner_run_attempt",
        "runner_run_id",
        "runtime_deploy",
        "runtime_deploy_digest",
        "runtime_inputs",
        "runtime_retirement",
        "runtime_retirement_digest",
        "runtime_sha",
        "scene_video_env_digest",
        "scene_video_env_gid",
        "scene_video_env_mode",
        "scene_video_env_path",
        "scene_video_env_uid",
        "schema",
        "transaction_started_at_epoch",
        "version",
        "web_image",
        "workflow_ref",
        "workflow_sha",
    }
    _require_keys(config, config_required, "config")
    static_bindings = {
        "schema": CONFIG_SCHEMA,
        "authority_profile": PROFILE,
        "repository": REPOSITORY,
        "workflow_ref": WORKFLOW_REF,
        "release_job": RELEASE_JOB,
        "environment": ENVIRONMENT,
        "project_name": PROJECT_NAME,
        "public_origin": PUBLIC_ORIGIN,
        "api_host_ip": API_HOST_IP,
        "database_image": DATABASE_IMAGE,
        "github_identity_env_path": IDENTITY_ENV_PATH,
        "registration_email_env_path": REGISTRATION_EMAIL_ENV_PATH,
        "scene_video_env_path": SCENE_VIDEO_ENV_PATH,
    }
    for key, expected in static_bindings.items():
        if _string(config, key, "config") != expected:
            fail(f"config-{key}-binding-invalid")
    _integer(config, "version", 2, 2, "config")
    _integer(config, "allowed_runner_uid", 1, (1 << 31) - 1, "config")
    _integer(config, "allowed_runner_gid", 1, (1 << 31) - 1, "config")
    _integer(config, "preflight_ttl_seconds", 30, 900, "config")
    _integer(config, "release_generation", 1, 1 << 62, "config")
    _integer(config, "transaction_started_at_epoch", 1, 1 << 62, "config")
    _integer(
        config,
        "backup_max_age_seconds",
        BACKUP_MAX_AGE_SECONDS,
        BACKUP_MAX_AGE_SECONDS,
        "config",
    )
    _integer(config, "api_host_port", API_HOST_PORT, API_HOST_PORT, "config")
    _integer(
        config,
        "api_container_port",
        API_CONTAINER_PORT,
        API_CONTAINER_PORT,
        "config",
    )
    _integer(config, "github_identity_env_uid", 0, (1 << 31) - 1, "config")
    _integer(config, "github_identity_env_gid", 0, (1 << 31) - 1, "config")
    _integer(config, "registration_email_env_uid", 0, (1 << 31) - 1, "config")
    _integer(config, "registration_email_env_gid", 0, (1 << 31) - 1, "config")
    _integer(config, "scene_video_env_mode", 384, 384, "config")
    _integer(config, "scene_video_env_uid", 1000, 1000, "config")
    _integer(config, "scene_video_env_gid", 1000, 1000, "config")
    _integer(config, "runner_run_attempt", 1, (1 << 31) - 1, "config")
    if config.get("github_identity_env_mode") != "0600":
        fail("config-github_identity_env_mode-invalid")
    if config.get("registration_email_env_mode") != "0600":
        fail("config-registration_email_env_mode-invalid")
    for key in (
        "host_machine_id_digest",
        "plan_digest",
        "pre_purge_root_env_digest",
        "post_purge_root_env_digest",
        "runtime_retirement_digest",
        "runtime_deploy_digest",
        "database_substrate_digest",
        "package_authority_key_id",
        "receipt_authority_key_id",
        "github_identity_env_digest",
        "registration_email_env_digest",
        "scene_video_env_digest",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256",
        "runner_prerequisite_intent_sha256",
        "runner_reservation_sha256",
    ):
        if not SHA256_PATTERN.fullmatch(_string(config, key, "config")):
            fail(f"config-{key}-invalid")
    deployment_id = _string(config, "deployment_id", "config")
    if not DEPLOYMENT_ID_PATTERN.fullmatch(deployment_id):
        fail("config-deployment_id-invalid")
    if config["package_authority_key_id"] != package_key_id:
        fail("config-package-key-binding-invalid")
    receipt_der = receipt_public.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    receipt_key_id = sha256(receipt_der)
    if receipt_key_id == package_key_id:
        fail("authority-key-role-collision")
    if config["receipt_authority_key_id"] != receipt_key_id:
        fail("config-receipt-key-binding-invalid")
    if not GIT_SHA_PATTERN.fullmatch(_string(config, "runtime_sha", "config")):
        fail("config-runtime_sha-invalid")
    if (
        not GIT_SHA_PATTERN.fullmatch(_string(config, "workflow_sha", "config"))
        or config["workflow_sha"] == config["runtime_sha"]
    ):
        fail("config-workflow_sha-invalid")
    if not ENVELOPE_SHA_PATTERN.fullmatch(
        _string(config, "envelope_sha", "config")
    ):
        fail("config-envelope_sha-invalid")
    predecessor = _string(config, "predecessor_runtime_sha", "config")
    if predecessor != "genesis" and not GIT_SHA_PATTERN.fullmatch(predecessor):
        fail("config-predecessor-runtime-invalid")
    web_image = _string(config, "web_image", "config")
    render_image = _string(config, "render_image", "config")
    cloudflared_image = _string(config, "cloudflared_image", "config")
    if (
        not IMAGE_PATTERN.fullmatch(web_image)
        or not web_image.startswith(
            "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:"
        )
        or not IMAGE_PATTERN.fullmatch(render_image)
        or not render_image.startswith(
            "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:"
        )
        or web_image == render_image
        or not CLOUDFLARED_IMAGE_PATTERN.fullmatch(cloudflared_image)
    ):
        fail("config-image-binding-invalid")
    credential_path = _string(config, "github_api_credential_path", "config")
    if credential_path != (
        "/run/credentials/propertyquarry-release-single-host-v2.service/"
        "github-api-token"
    ):
        fail("config-github-credential-path-invalid")
    if (
        _string(config, "github_oidc_request_origin", "config")
        != "https://vstoken.actions.githubusercontent.com"
        or _string(config, "ephemeral_runner_label_prefix", "config")
        != "pqrelease-"
    ):
        fail("config-github-binding-invalid")
    if (
        _string(config, "repository_id", "config") != REPOSITORY_ID
        or _string(config, "repository_owner_id", "config")
        != REPOSITORY_OWNER_ID
    ):
        fail("config-repository-identity-invalid")
    if (
        not re.fullmatch(r"pqrelease-[0-9a-f]{32}", _string(config, "runner_label", "config"))
        or not NUMERIC_ID_PATTERN.fullmatch(_string(config, "runner_run_id", "config"))
        or not NUMERIC_ID_PATTERN.fullmatch(_string(config, "runner_job_id", "config"))
        or not NUMERIC_ID_PATTERN.fullmatch(
            _string(config, "runner_prerequisite_job_id", "config")
        )
        or config["runner_job_id"] == config["runner_prerequisite_job_id"]
    ):
        fail("config-runner-binding-invalid")

    pre_inputs, post_inputs = validate_signed_runtime_inputs(
        config["pre_purge_runtime_inputs"], config["runtime_inputs"]
    )
    if (
        config["pre_purge_root_env_digest"] != pre_inputs[0]["sha256"]
        or config["post_purge_root_env_digest"] != post_inputs[0]["sha256"]
        or config["scene_video_env_digest"] != pre_inputs[1]["sha256"]
        or config["scene_video_env_uid"] != pre_inputs[1]["uid"]
        or config["scene_video_env_gid"] != pre_inputs[1]["gid"]
        or config["github_identity_env_digest"] != pre_inputs[4]["sha256"]
        or config["github_identity_env_uid"] != pre_inputs[4]["uid"]
        or config["github_identity_env_gid"] != pre_inputs[4]["gid"]
        or config["registration_email_env_digest"] != pre_inputs[5]["sha256"]
        or config["registration_email_env_uid"] != pre_inputs[5]["uid"]
        or config["registration_email_env_gid"] != pre_inputs[5]["gid"]
    ):
        fail("config-runtime-input-binding-invalid")
    retirement_digest = validate_runtime_retirement_contract(
        config["runtime_retirement"], config["runtime_sha"], deployment_id
    )
    deploy_digest = validate_runtime_deploy_contract(
        config["runtime_deploy"], config["runtime_sha"], deployment_id
    )
    substrate_digest = validate_database_substrate(
        config["database_substrate"], config["database_image"]
    )
    if (
        retirement_digest != config["runtime_retirement_digest"]
        or deploy_digest != config["runtime_deploy_digest"]
        or substrate_digest != config["database_substrate_digest"]
    ):
        fail("config-observed-contract-digest-invalid")

    if sha256(plan_raw) != config["plan_digest"]:
        fail("plan-digest-mismatch")
    plan = parse_strict_json(plan_raw, "plan")
    plan_required = {
        "api_container_port",
        "api_host_ip",
        "api_host_port",
        "authority_profile",
        "backup_max_age_seconds",
        "cloudflared_image",
        "database_image",
        "database_substrate",
        "database_substrate_digest",
        "deployment_id",
        "envelope_sha",
        "executables",
        "github_identity_env_digest",
        "github_identity_env_gid",
        "github_identity_env_mode",
        "github_identity_env_path",
        "github_identity_env_uid",
        "host_machine_id_digest",
        "post_purge_root_env_digest",
        "pre_purge_root_env_digest",
        "pre_purge_runtime_inputs",
        "predecessor_runtime_sha",
        "preflight_steps",
        "project_name",
        "public_origin",
        "registration_email_env_digest",
        "registration_email_env_gid",
        "registration_email_env_mode",
        "registration_email_env_path",
        "registration_email_env_uid",
        "release_generation",
        "release_steps",
        "render_image",
        "repository",
        "runner_job_id",
        "runner_label",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256",
        "runner_prerequisite_intent_sha256",
        "runner_prerequisite_job_id",
        "runner_reservation_sha256",
        "runner_run_attempt",
        "runner_run_id",
        "rollback_steps",
        "runtime_deploy",
        "runtime_deploy_digest",
        "runtime_inputs",
        "runtime_retirement",
        "runtime_retirement_digest",
        "runtime_sha",
        "scene_video_env_digest",
        "scene_video_env_gid",
        "scene_video_env_mode",
        "scene_video_env_path",
        "scene_video_env_uid",
        "schema",
        "transaction_started_at_epoch",
        "verify_steps",
        "version",
        "web_image",
        "workflow_sha",
    }
    _require_keys(plan, plan_required, "plan")
    plan_bindings = {
        "schema": PLAN_SCHEMA,
        "authority_profile": PROFILE,
        "runtime_sha": config["runtime_sha"],
        "workflow_sha": config["workflow_sha"],
        "deployment_id": deployment_id,
        "transaction_started_at_epoch": config["transaction_started_at_epoch"],
        "backup_max_age_seconds": config["backup_max_age_seconds"],
        "envelope_sha": config["envelope_sha"],
        "host_machine_id_digest": config["host_machine_id_digest"],
        "repository": REPOSITORY,
        "project_name": PROJECT_NAME,
        "public_origin": PUBLIC_ORIGIN,
        "pre_purge_root_env_digest": config["pre_purge_root_env_digest"],
        "post_purge_root_env_digest": config["post_purge_root_env_digest"],
        "runtime_retirement_digest": config["runtime_retirement_digest"],
        "runtime_deploy_digest": config["runtime_deploy_digest"],
        "database_substrate_digest": config["database_substrate_digest"],
        "api_host_ip": config["api_host_ip"],
        "api_host_port": config["api_host_port"],
        "api_container_port": config["api_container_port"],
        "web_image": web_image,
        "render_image": render_image,
        "predecessor_runtime_sha": predecessor,
        "github_identity_env_path": config["github_identity_env_path"],
        "github_identity_env_digest": config["github_identity_env_digest"],
        "github_identity_env_mode": config["github_identity_env_mode"],
        "github_identity_env_uid": config["github_identity_env_uid"],
        "github_identity_env_gid": config["github_identity_env_gid"],
        "registration_email_env_path": config["registration_email_env_path"],
        "registration_email_env_digest": config["registration_email_env_digest"],
        "registration_email_env_mode": config["registration_email_env_mode"],
        "registration_email_env_uid": config["registration_email_env_uid"],
        "registration_email_env_gid": config["registration_email_env_gid"],
        "cloudflared_image": config["cloudflared_image"],
        "database_image": config["database_image"],
        "scene_video_env_path": config["scene_video_env_path"],
        "scene_video_env_digest": config["scene_video_env_digest"],
        "scene_video_env_mode": config["scene_video_env_mode"],
        "scene_video_env_uid": config["scene_video_env_uid"],
        "scene_video_env_gid": config["scene_video_env_gid"],
        "release_generation": config["release_generation"],
        "runner_job_id": config["runner_job_id"],
        "runner_label": config["runner_label"],
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
        "runner_reservation_sha256": config["runner_reservation_sha256"],
        "runner_run_attempt": config["runner_run_attempt"],
        "runner_run_id": config["runner_run_id"],
        "version": 2,
    }
    for key, expected in plan_bindings.items():
        if type(plan.get(key)) is not type(expected) or plan.get(key) != expected:
            fail(f"plan-{key}-binding-invalid")
    for key in (
        "pre_purge_runtime_inputs",
        "runtime_inputs",
        "runtime_retirement",
        "runtime_deploy",
        "database_substrate",
    ):
        if canonical_json(plan.get(key)) != canonical_json(config[key]):
            fail(f"plan-{key}-binding-invalid")
    validate_plan_steps(plan)
    return config, plan, receipt_key_id


def _expected_isolation_argv(
    plan: dict[str, Any], operation: str, *, receipt: bool, pre_purge: bool
) -> list[str]:
    argv = [
        RUNTIME_ISOLATION_HELPER_PATH,
        operation,
        "--runtime-sha",
        plan["runtime_sha"],
        "--deployment-id",
        plan["deployment_id"],
        "--envelope-sha",
        plan["envelope_sha"],
        "--web-image",
        plan["web_image"],
        "--render-image",
        plan["render_image"],
        "--cloudflared-image",
        plan["cloudflared_image"],
        "--database-image",
        plan["database_image"],
        "--api-host-ip",
        plan["api_host_ip"],
        "--api-host-port",
        str(plan["api_host_port"]),
        "--api-container-port",
        str(plan["api_container_port"]),
    ]
    if pre_purge:
        argv.extend(
            ["--pre-purge-root-env-digest", plan["pre_purge_root_env_digest"]]
        )
    if receipt:
        argv.extend(
            [
                "--receipt",
                f"{RUNTIME_ISOLATION_RECEIPT_ROOT}/{plan['runtime_sha']}/"
                f"{plan['deployment_id']}/{operation}.json",
            ]
        )
    return argv


def _expected_runtime_deploy_argv(plan: dict[str, Any]) -> list[str]:
    return [
        RUNTIME_DEPLOY_HELPER_PATH,
        "deploy-runtime",
        "--runtime-sha",
        plan["runtime_sha"],
        "--deployment-id",
        plan["deployment_id"],
        "--envelope-sha",
        plan["envelope_sha"],
        "--web-image",
        plan["web_image"],
        "--render-image",
        plan["render_image"],
        "--cloudflared-image",
        plan["cloudflared_image"],
        "--database-image",
        plan["database_image"],
        "--api-host-ip",
        plan["api_host_ip"],
        "--api-host-port",
        str(plan["api_host_port"]),
        "--api-container-port",
        str(plan["api_container_port"]),
        "--receipt",
        f"{RUNTIME_DEPLOY_RECEIPT_ROOT}/{plan['runtime_sha']}/"
        f"{plan['deployment_id']}/deploy-runtime.json",
    ]


def _step_contract(
    identifier: str,
    effect: str,
    argv: list[str],
    timeout_seconds: int,
) -> dict[str, Any]:
    return {
        "argv": argv,
        "effect": effect,
        "expected_exit_code": 0,
        "id": identifier,
        "idempotent": True,
        "timeout_seconds": timeout_seconds,
    }


def validate_plan_steps(plan: dict[str, Any]) -> None:
    runtime_sha = plan.get("runtime_sha")
    deployment_id = plan.get("deployment_id")
    if (
        not isinstance(runtime_sha, str)
        or not GIT_SHA_PATTERN.fullmatch(runtime_sha)
        or not isinstance(deployment_id, str)
        or not DEPLOYMENT_ID_PATTERN.fullmatch(deployment_id)
    ):
        fail("plan-release-identity-invalid")
    pre_inputs, post_inputs = validate_signed_runtime_inputs(
        plan.get("pre_purge_runtime_inputs"), plan.get("runtime_inputs")
    )
    if (
        plan.get("pre_purge_root_env_digest") != pre_inputs[0]["sha256"]
        or plan.get("post_purge_root_env_digest") != post_inputs[0]["sha256"]
    ):
        fail("plan-runtime-input-binding-invalid")
    if (
        validate_runtime_retirement_contract(
            plan.get("runtime_retirement"), runtime_sha, deployment_id
        )
        != plan.get("runtime_retirement_digest")
        or validate_runtime_deploy_contract(
            plan.get("runtime_deploy"), runtime_sha, deployment_id
        )
        != plan.get("runtime_deploy_digest")
        or validate_database_substrate(
            plan.get("database_substrate"), plan.get("database_image")
        )
        != plan.get("database_substrate_digest")
    ):
        fail("plan-observed-contract-digest-invalid")

    executables = plan.get("executables")
    required_executables = {
        PREDEPLOY_BACKUP_HELPER_PATH,
        RUNTIME_ISOLATION_HELPER_PATH,
        DATABASE_CONTROL_HELPER_PATH,
        RUNTIME_DEPLOY_HELPER_PATH,
    }
    if not isinstance(executables, dict) or set(executables) != required_executables:
        fail("plan-executables-invalid")
    for path, expected_digest in executables.items():
        if (
            not EXECUTABLE_PATTERN.fullmatch(path)
            or not isinstance(expected_digest, str)
            or not SHA256_PATTERN.fullmatch(expected_digest)
        ):
            fail("plan-executable-invalid")
    if executables[PREDEPLOY_BACKUP_HELPER_PATH] != PREDEPLOY_BACKUP_HELPER_SHA256:
        fail("plan-predeploy-backup-executable-invalid")
    if executables[DATABASE_CONTROL_HELPER_PATH] != DATABASE_CONTROL_HELPER_SHA256:
        fail("plan-database-control-executable-invalid")
    if executables[RUNTIME_ISOLATION_HELPER_PATH] != RUNTIME_ISOLATION_HELPER_SHA256:
        fail("plan-runtime-isolation-executable-invalid")
    if executables[RUNTIME_DEPLOY_HELPER_PATH] != RUNTIME_DEPLOY_HELPER_SHA256:
        fail("plan-runtime-deploy-executable-invalid")

    groups = (
        ("preflight_steps", "read-only"),
        ("release_steps", "mutation"),
        ("verify_steps", "verification"),
        ("rollback_steps", "rollback"),
    )
    for key, expected_effect in groups:
        steps = plan.get(key)
        if not isinstance(steps, list) or not 1 <= len(steps) <= 64:
            fail(f"plan-{key}-invalid")
        seen: set[str] = set()
        for step in steps:
            if not isinstance(step, dict) or set(step) != {
                "argv",
                "effect",
                "expected_exit_code",
                "id",
                "idempotent",
                "timeout_seconds",
            }:
                fail("plan-step-shape-invalid")
            step_id = step.get("id")
            argv = step.get("argv")
            if (
                not isinstance(step_id, str)
                or not STEP_ID_PATTERN.fullmatch(step_id)
                or step_id in seen
                or step.get("effect") != expected_effect
                or type(step.get("idempotent")) is not bool
                or (expected_effect == "rollback" and step["idempotent"] is not True)
                or type(step.get("expected_exit_code")) is not int
                or step.get("expected_exit_code") != 0
                or type(step.get("timeout_seconds")) is not int
                or not 1 <= step["timeout_seconds"] <= 9600
                or not isinstance(argv, list)
                or not 1 <= len(argv) <= 64
            ):
                fail("plan-step-invalid")
            seen.add(step_id)
            for argument in argv:
                if (
                    not isinstance(argument, str)
                    or not argument
                    or len(argument.encode("utf-8")) > 4096
                    or _forbidden_argument_text(argument)
                ):
                    fail("plan-step-argument-invalid")
            if argv[0] not in executables:
                fail("plan-step-executable-unbound")

    preflight = plan["preflight_steps"]
    if preflight != [
        _step_contract(
            VERIFY_ISOLATION_INPUTS_STEP_ID,
            "read-only",
            _expected_isolation_argv(
                plan, "verify-isolation-inputs", receipt=False, pre_purge=True
            ),
            600,
        )
    ]:
        fail("plan-preflight-isolation-contract-invalid")

    release_steps = plan["release_steps"]
    if (
        len(release_steps) != len(REQUIRED_RELEASE_STEP_IDS)
        or tuple(step["id"] for step in release_steps) != REQUIRED_RELEASE_STEP_IDS
    ):
        fail("plan-release-order-invalid")
    expected_backup_argv = [
        PREDEPLOY_BACKUP_HELPER_PATH,
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
        f"{BACKUP_RECEIPT_ROOT}/{runtime_sha}/{deployment_id}/create.json",
        "--encryption-key",
        BACKUP_ENCRYPTION_KEY_PATH,
    ]
    if release_steps[0] != _step_contract(
        "predeploy-encrypted-backup", "mutation", expected_backup_argv, 9600
    ):
        fail("plan-predeploy-backup-contract-invalid")
    if release_steps[1] != _step_contract(
        "purge-propertyquarry-legacy-runtime-exposure",
        "mutation",
        _expected_isolation_argv(
            plan,
            "purge-legacy-runtime-exposure",
            receipt=True,
            pre_purge=True,
        ),
        600,
    ):
        fail("plan-runtime-purge-contract-invalid")
    if release_steps[2] != _step_contract(
        "retire-stale-propertyquarry-runtime",
        "mutation",
        _expected_isolation_argv(
            plan,
            "retire-stale-propertyquarry-runtime",
            receipt=True,
            pre_purge=False,
        ),
        600,
    ):
        fail("plan-runtime-retirement-contract-invalid")
    database_contracts = (
        (
            "provision-propertyquarry-database-roles",
            "provision-roles",
            900,
            "provision-roles.json",
        ),
        (
            "migrate-propertyquarry-schema",
            "migrate-schema",
            1500,
            "migrate-schema.json",
        ),
        (
            "harden-propertyquarry-runtime-acl",
            "harden-runtime-acl",
            900,
            "harden-runtime-acl.json",
        ),
        (
            "verify-propertyquarry-schema-readiness",
            "verify-schema-readiness",
            600,
            "verify-schema-readiness.json",
        ),
    )
    for index, (step_id, operation, timeout_seconds, receipt_name) in enumerate(
        database_contracts,
        start=3,
    ):
        expected_argv = [
            DATABASE_CONTROL_HELPER_PATH,
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
            f"{DATABASE_RECEIPT_ROOT}/{runtime_sha}/{deployment_id}/{receipt_name}",
        ]
        if release_steps[index] != _step_contract(
            step_id, "mutation", expected_argv, timeout_seconds
        ):
            fail("plan-database-control-contract-invalid")
    if release_steps[7] != _step_contract(
        "deploy-propertyquarry-runtime",
        "mutation",
        _expected_runtime_deploy_argv(plan),
        1800,
    ):
        fail("plan-runtime-deploy-contract-invalid")

    verify = plan["verify_steps"]
    if verify != [
        _step_contract(
            TERMINAL_ISOLATION_VERIFY_STEP_ID,
            "verification",
            _expected_isolation_argv(
                plan,
                "verify-runtime-isolation",
                receipt=True,
                pre_purge=False,
            ),
            600,
        )
    ]:
        fail("plan-terminal-isolation-verification-invalid")
    rollback = plan["rollback_steps"]
    if rollback != [
        _step_contract(
            ROLLBACK_ISOLATION_STEP_ID,
            "rollback",
            _expected_isolation_argv(
                plan,
                "restore-legacy-runtime-exposure",
                receipt=True,
                pre_purge=True,
            ),
            600,
        )
    ]:
        fail("plan-runtime-rollback-contract-invalid")


def _verified_runner_wire(
    raw: bytes,
    *,
    public: Ed25519PublicKey,
    key_id: str,
    domain: bytes,
    label: str,
) -> dict[str, Any]:
    wrapper = parse_strict_json(raw, label)
    if (
        not isinstance(wrapper, dict)
        or set(wrapper) != {"payload", "signature", "signature_key_id"}
        or not isinstance(wrapper.get("payload"), dict)
        or type(wrapper.get("signature")) is not str
        or wrapper.get("signature_key_id") != key_id
    ):
        fail(f"{label}-wrapper-invalid")
    signature_text = wrapper["signature"]
    try:
        signature = base64.b64decode(
            signature_text.encode("ascii") + b"=" * (-len(signature_text) % 4),
            altchars=b"-_",
            validate=True,
        )
        canonical = canonical_json(wrapper["payload"])
        public.verify(signature, framed(domain, canonical))
    except (UnicodeEncodeError, ValueError, InvalidSignature):
        fail(f"{label}-signature-invalid")
    if (
        len(signature) != 64
        or signature_text
        != base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    ):
        fail(f"{label}-signature-encoding-invalid")
    return wrapper["payload"]


def validate_runner_prerequisite_material(
    *,
    intent_raw: bytes,
    approval_raw: bytes,
    reservation_raw: bytes,
    config: dict[str, Any],
    receipt_public: Ed25519PublicKey,
    receipt_key_id: str,
) -> dict[str, Any]:
    """Authenticate the durable protected-environment prerequisite proof chain."""
    reservation = _verified_runner_wire(
        reservation_raw,
        public=receipt_public,
        key_id=receipt_key_id,
        domain=RUNNER_RESERVATION_SIGNATURE_DOMAIN,
        label="runner-prerequisite-reservation",
    )
    intent = _verified_runner_wire(
        intent_raw,
        public=receipt_public,
        key_id=receipt_key_id,
        domain=RUNNER_PREREQUISITE_INTENT_SIGNATURE_DOMAIN,
        label="runner-prerequisite-intent",
    )
    intent_keys = {
        "authority_profile", "comment", "discovered_at_epoch", "environment_id",
        "environment_name", "initial_jobs_sha256",
        "initial_pending_deployments_sha256", "initial_runs_index_sha256",
        "prerequisite_job_id", "prerequisite_job_name",
        "receipt_authority_key_id", "release_job", "repository", "repository_id",
        "repository_owner_id", "reservation_expires_at_epoch",
        "reservation_sha256", "run_attempt", "run_id", "runner_label", "schema",
        "version", "workflow_path", "workflow_ref", "workflow_sha",
    }
    discovered = intent.get("discovered_at_epoch")
    reservation_created = reservation.get("created_at_epoch")
    reservation_expires = reservation.get("expires_at_epoch")
    if (
        set(intent) != intent_keys
        or intent.get("schema") != RUNNER_PREREQUISITE_INTENT_SCHEMA
        or intent.get("version") != 2
        or intent.get("authority_profile") != PROFILE
        or intent.get("repository") != REPOSITORY
        or intent.get("repository_id") != REPOSITORY_ID
        or intent.get("repository_owner_id") != REPOSITORY_OWNER_ID
        or intent.get("workflow_path") != ".github/workflows/smoke-runtime.yml"
        or intent.get("workflow_ref") != WORKFLOW_REF
        or intent.get("workflow_sha") != config["workflow_sha"]
        or intent.get("receipt_authority_key_id") != receipt_key_id
        or intent.get("reservation_sha256") != sha256(reservation_raw)
        or intent.get("reservation_sha256") != config["runner_reservation_sha256"]
        or intent.get("reservation_expires_at_epoch") != reservation_expires
        or intent.get("runner_label") != config["runner_label"]
        or intent.get("runner_label") != reservation.get("runner_label")
        or intent.get("environment_name") != ENVIRONMENT
        or type(intent.get("environment_id")) is not str
        or NUMERIC_ID_PATTERN.fullmatch(intent["environment_id"]) is None
        or intent.get("prerequisite_job_name") != RUNNER_PREREQUISITE_JOB
        or intent.get("prerequisite_job_id") != config["runner_prerequisite_job_id"]
        or type(intent.get("prerequisite_job_id")) is not str
        or NUMERIC_ID_PATTERN.fullmatch(intent["prerequisite_job_id"]) is None
        or intent.get("release_job") != RELEASE_JOB
        or intent.get("run_id") != config["runner_run_id"]
        or type(intent.get("run_id")) is not str
        or NUMERIC_ID_PATTERN.fullmatch(intent["run_id"]) is None
        or intent.get("run_attempt") != config["runner_run_attempt"]
        or type(intent.get("run_attempt")) is not int
        or isinstance(intent.get("run_attempt"), bool)
        or type(discovered) is not int
        or isinstance(discovered, bool)
        or type(reservation_created) is not int
        or isinstance(reservation_created, bool)
        or type(reservation_expires) is not int
        or isinstance(reservation_expires, bool)
        or not reservation_created <= discovered <= reservation_expires
        or intent.get("comment")
        != "PropertyQuarry governed prerequisite approval " + sha256(reservation_raw)
        or any(
            type(intent.get(field)) is not str
            or SHA256_PATTERN.fullmatch(intent[field]) is None
            for field in (
                "initial_jobs_sha256",
                "initial_pending_deployments_sha256",
                "initial_runs_index_sha256",
            )
        )
        or sha256(intent_raw) != config["runner_prerequisite_intent_sha256"]
    ):
        fail("runner-prerequisite-intent-binding-invalid")

    approval = _verified_runner_wire(
        approval_raw,
        public=receipt_public,
        key_id=receipt_key_id,
        domain=RUNNER_PREREQUISITE_APPROVAL_SIGNATURE_DOMAIN,
        label="runner-prerequisite-approval",
    )
    approval_keys = {
        "approval_api_disposition", "approval_response_sha256",
        "approved_at_epoch", "completed_jobs_sha256", "environment_id",
        "environment_name", "intent_sha256", "post_pending_deployments_sha256",
        "prerequisite_conclusion", "prerequisite_job_id",
        "prerequisite_job_name", "receipt_authority_key_id", "release_job",
        "repository", "repository_id", "repository_owner_id",
        "reservation_expires_at_epoch", "reservation_sha256",
        "review_history_sha256", "run_attempt", "run_id", "runner_label",
        "schema", "version", "workflow_path", "workflow_ref", "workflow_sha",
    }
    approved = approval.get("approved_at_epoch")
    disposition = approval.get("approval_api_disposition")
    response_digest = approval.get("approval_response_sha256")
    approval_payload_digest = sha256(canonical_json(approval))
    if (
        set(approval) != approval_keys
        or approval.get("schema") != RUNNER_PREREQUISITE_APPROVAL_SCHEMA
        or approval.get("version") != 2
        or approval.get("intent_sha256") != sha256(intent_raw)
        or approval.get("reservation_sha256") != intent["reservation_sha256"]
        or approval.get("runner_label") != intent["runner_label"]
        or approval.get("run_id") != intent["run_id"]
        or approval.get("run_attempt") != intent["run_attempt"]
        or approval.get("prerequisite_job_id") != intent["prerequisite_job_id"]
        or approval.get("prerequisite_job_name") != RUNNER_PREREQUISITE_JOB
        or approval.get("prerequisite_conclusion") != "success"
        or approval.get("environment_id") != intent["environment_id"]
        or approval.get("environment_name") != ENVIRONMENT
        or approval.get("receipt_authority_key_id") != receipt_key_id
        or approval.get("repository") != REPOSITORY
        or approval.get("repository_id") != REPOSITORY_ID
        or approval.get("repository_owner_id") != REPOSITORY_OWNER_ID
        or approval.get("workflow_path") != ".github/workflows/smoke-runtime.yml"
        or approval.get("workflow_ref") != WORKFLOW_REF
        or approval.get("workflow_sha") != config["workflow_sha"]
        or approval.get("release_job") != RELEASE_JOB
        or approval.get("reservation_expires_at_epoch") != reservation_expires
        or disposition not in {"approved", "post-approved-recovered"}
        or (
            disposition == "approved"
            and (
                type(response_digest) is not str
                or SHA256_PATTERN.fullmatch(response_digest) is None
            )
        )
        or (disposition == "post-approved-recovered" and response_digest is not None)
        or any(
            type(approval.get(field)) is not str
            or SHA256_PATTERN.fullmatch(approval[field]) is None
            for field in (
                "completed_jobs_sha256",
                "post_pending_deployments_sha256",
                "review_history_sha256",
            )
        )
        or type(approved) is not int
        or isinstance(approved, bool)
        or not discovered <= approved <= reservation_expires
        or approved > config["transaction_started_at_epoch"]
        or sha256(approval_raw) != config["runner_prerequisite_approval_sha256"]
        or approval_payload_digest
        != config["runner_prerequisite_approval_payload_sha256"]
    ):
        fail("runner-prerequisite-approval-binding-invalid")
    return {
        "runner_prerequisite_approval_payload_sha256": approval_payload_digest,
        "runner_prerequisite_approval_sha256": sha256(approval_raw),
        "runner_prerequisite_intent_sha256": sha256(intent_raw),
        "runner_prerequisite_job_id": approval["prerequisite_job_id"],
    }


def validate_runner_material(
    *,
    reservation_raw: bytes,
    ticket_raw: bytes,
    config: dict[str, Any],
    config_raw: bytes,
    plan_raw: bytes,
    receipt_public: Ed25519PublicKey,
    receipt_key_id: str,
    now: int | None = None,
) -> dict[str, Any]:
    reservation = _verified_runner_wire(
        reservation_raw,
        public=receipt_public,
        key_id=receipt_key_id,
        domain=RUNNER_RESERVATION_SIGNATURE_DOMAIN,
        label="runner-reservation",
    )
    reservation_keys = {
        "authority_profile", "created_at_epoch", "environment", "expires_at_epoch",
        "receipt_authority_key_id", "release_job", "repository", "repository_id",
        "repository_owner_id", "reservation_nonce", "runner_label",
        "runner_label_nonce", "schema", "source_checkout_identity_sha256",
        "source_checkout_path", "source_tree_sha256", "version", "workflow_path",
        "workflow_ref", "workflow_sha",
    }
    nonce = reservation.get("reservation_nonce")
    label = reservation.get("runner_label")
    created = reservation.get("created_at_epoch")
    reservation_expires = reservation.get("expires_at_epoch")
    if (
        set(reservation) != reservation_keys
        or reservation.get("schema") != RUNNER_RESERVATION_SCHEMA
        or reservation.get("version") != 2
        or reservation.get("authority_profile") != PROFILE
        or reservation.get("environment") != ENVIRONMENT
        or reservation.get("repository") != REPOSITORY
        or reservation.get("repository_id") != REPOSITORY_ID
        or reservation.get("repository_owner_id") != REPOSITORY_OWNER_ID
        or reservation.get("workflow_path") != ".github/workflows/smoke-runtime.yml"
        or reservation.get("workflow_ref") != WORKFLOW_REF
        or reservation.get("workflow_sha") != config["workflow_sha"]
        or reservation.get("release_job") != RELEASE_JOB
        or reservation.get("receipt_authority_key_id") != receipt_key_id
        or type(nonce) is not str
        or re.fullmatch(r"[0-9a-f]{64}", nonce) is None
        or type(label) is not str
        or re.fullmatch(r"pqrelease-[0-9a-f]{32}", label) is None
        or reservation.get("runner_label_nonce") != label.removeprefix("pqrelease-")
        or type(created) is not int
        or isinstance(created, bool)
        or type(reservation_expires) is not int
        or isinstance(reservation_expires, bool)
        or created < 1
        or reservation_expires - created != RUNNER_RESERVATION_TTL_SECONDS
        or reservation.get("source_checkout_path")
        != f"{RUNNER_RELEASE_CHECKOUT_ROOT}/{config['workflow_sha']}"
        or not SHA256_PATTERN.fullmatch(
            _string(reservation, "source_checkout_identity_sha256", "runner-reservation")
        )
        or not SHA256_PATTERN.fullmatch(
            _string(reservation, "source_tree_sha256", "runner-reservation")
        )
        or sha256(reservation_raw) != config["runner_reservation_sha256"]
        or label != config["runner_label"]
    ):
        fail("runner-reservation-binding-invalid")
    derived = hashlib.sha256(
        RUNNER_LABEL_DERIVATION_DOMAIN + bytes.fromhex(nonce)
    ).hexdigest()[:32]
    if label != "pqrelease-" + derived:
        fail("runner-reservation-label-derivation-invalid")

    ticket = _verified_runner_wire(
        ticket_raw,
        public=receipt_public,
        key_id=receipt_key_id,
        domain=RUNNER_LAUNCH_TICKET_SIGNATURE_DOMAIN,
        label="runner-launch-ticket",
    )
    ticket_keys = {
        "authority_profile", "bound_at_epoch", "config_digest",
        "dispatch_ticket_sha256", "docker_socket", "environment",
        "expires_at_epoch", "job_id", "plan_digest", "receipt_authority_key_id",
        "release_job", "repository", "repository_id", "repository_owner_id",
        "reservation_nonce", "run_attempt", "run_id", "runner_image",
        "runner_label", "runner_label_nonce",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256", "runner_prerequisite_intent_sha256",
        "runner_prerequisite_job_id", "runtime_sha", "schema", "version",
        "workflow_path", "workflow_ref", "workflow_sha",
    }
    bound_at = ticket.get("bound_at_epoch")
    ticket_expires = ticket.get("expires_at_epoch")
    socket = ticket.get("docker_socket")
    current = int(time.time()) if now is None else now
    if (
        set(ticket) != ticket_keys
        or ticket.get("schema") != RUNNER_LAUNCH_TICKET_SCHEMA
        or ticket.get("version") != 2
        or ticket.get("authority_profile") != PROFILE
        or ticket.get("environment") != ENVIRONMENT
        or ticket.get("repository") != REPOSITORY
        or ticket.get("repository_id") != REPOSITORY_ID
        or ticket.get("repository_owner_id") != REPOSITORY_OWNER_ID
        or ticket.get("workflow_path") != ".github/workflows/smoke-runtime.yml"
        or ticket.get("workflow_ref") != WORKFLOW_REF
        or ticket.get("workflow_sha") != config["workflow_sha"]
        or ticket.get("release_job") != RELEASE_JOB
        or ticket.get("runtime_sha") != config["runtime_sha"]
        or ticket.get("config_digest") != sha256(config_raw)
        or ticket.get("plan_digest") != sha256(plan_raw)
        or ticket.get("receipt_authority_key_id") != receipt_key_id
        or ticket.get("dispatch_ticket_sha256") != sha256(reservation_raw)
        or ticket.get("reservation_nonce") != nonce
        or ticket.get("runner_label") != config["runner_label"]
        or ticket.get("runner_label_nonce") != reservation["runner_label_nonce"]
        or ticket.get("runner_prerequisite_approval_payload_sha256")
        != config["runner_prerequisite_approval_payload_sha256"]
        or ticket.get("runner_prerequisite_approval_sha256")
        != config["runner_prerequisite_approval_sha256"]
        or ticket.get("runner_prerequisite_intent_sha256")
        != config["runner_prerequisite_intent_sha256"]
        or ticket.get("runner_prerequisite_job_id")
        != config["runner_prerequisite_job_id"]
        or ticket.get("run_id") != config["runner_run_id"]
        or ticket.get("run_attempt") != config["runner_run_attempt"]
        or ticket.get("job_id") != config["runner_job_id"]
        or ticket.get("runner_image") != config["web_image"]
        or type(bound_at) is not int
        or isinstance(bound_at, bool)
        or type(ticket_expires) is not int
        or isinstance(ticket_expires, bool)
        or bound_at < config["transaction_started_at_epoch"]
        or ticket_expires <= bound_at
        or ticket_expires - bound_at > RUNNER_TICKET_TTL_SECONDS
        or ticket_expires > reservation_expires
        or type(current) is not int
        or current < bound_at
        or current > ticket_expires
        or not isinstance(socket, dict)
        or set(socket) != {"device", "gid", "inode", "mode", "nlink", "path", "uid"}
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
        fail("runner-launch-ticket-binding-or-freshness-invalid")
    return {
        "runner_launch_ticket_sha256": sha256(ticket_raw),
        "runner_source_checkout_identity_sha256": reservation[
            "source_checkout_identity_sha256"
        ],
        "runner_source_checkout_path": reservation["source_checkout_path"],
        "runner_source_tree_sha256": reservation["source_tree_sha256"],
    }


def validate_materialization_receipt(
    receipt_raw: bytes,
    signature: bytes,
    *,
    config: dict[str, Any],
    config_raw: bytes,
    plan_raw: bytes,
    package_public: Ed25519PublicKey,
    package_key_id: str,
    receipt_key_id: str,
    runner_material: dict[str, Any],
    now: int | None = None,
) -> dict[str, Any]:
    if len(signature) != 64:
        fail("materialization-receipt-signature-size-invalid")
    try:
        package_public.verify(
            signature, framed(MATERIALIZATION_SIGNATURE_DOMAIN, receipt_raw)
        )
    except InvalidSignature:
        fail("materialization-receipt-signature-invalid")
    receipt = parse_strict_json(receipt_raw, "materialization-receipt")
    required = {
        "authoritative", "config_sha256", "deployment_id", "final_artifact_id",
        "final_artifact_sha256", "image_publication_run_attempt",
        "image_publication_run_completed_at_epoch", "image_publication_run_id",
        "installed_state_absence_proven", "materialized_at_epoch",
        "observation_completed_at_epoch", "package_authority_key_id", "plan_sha256",
        "preflight_artifact_id", "preflight_artifact_sha256", "production_ready",
        "receipt_authority_key_id", "release_generation", "release_hygiene_sha256",
        "render_attestation_id", "root_helper_authorization_required", "runtime_sha",
        "runner_launch_ticket_sha256", "runner_source_checkout_identity_sha256",
        "runner_source_checkout_path", "runner_source_tree_sha256",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256", "runner_prerequisite_intent_sha256",
        "runner_prerequisite_job_id", "schema",
        "valid_until_epoch", "version", "web_attestation_id", "workflow_sha",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        fail("materialization-receipt-shape-invalid")
    materialized = receipt.get("materialized_at_epoch")
    observed = receipt.get("observation_completed_at_epoch")
    valid_until = receipt.get("valid_until_epoch")
    publication_completed = receipt.get("image_publication_run_completed_at_epoch")
    current = int(time.time()) if now is None else now
    if (
        receipt.get("schema") != MATERIALIZATION_RECEIPT_SCHEMA
        or receipt.get("version") != 2
        or receipt.get("authoritative") is not False
        or receipt.get("production_ready") is not False
        or receipt.get("installed_state_absence_proven") is not False
        or receipt.get("root_helper_authorization_required") is not True
        or receipt.get("config_sha256") != sha256(config_raw)
        or receipt.get("plan_sha256") != sha256(plan_raw)
        or receipt.get("deployment_id") != config.get("deployment_id")
        or receipt.get("package_authority_key_id") != package_key_id
        or receipt.get("receipt_authority_key_id") != receipt_key_id
        or receipt.get("release_generation") != config.get("release_generation")
        or receipt.get("runtime_sha") != config.get("runtime_sha")
        or receipt.get("workflow_sha") != config.get("workflow_sha")
        or any(receipt.get(key) != value for key, value in runner_material.items())
        or materialized != config.get("transaction_started_at_epoch")
        or type(materialized) is not int
        or type(observed) is not int
        or type(valid_until) is not int
        or type(publication_completed) is not int
        or observed < materialized
        or observed - materialized > MAX_MATERIALIZATION_OBSERVATION_SECONDS
        or valid_until != materialized + BACKUP_MAX_AGE_SECONDS
        or publication_completed > materialized + 60
        or materialized - publication_completed > MAX_PUBLICATION_EVIDENCE_AGE_SECONDS
        or type(current) is not int
        or current < materialized
        or current > valid_until
    ):
        fail("materialization-receipt-binding-or-freshness-invalid")
    for key in (
        "final_artifact_id", "image_publication_run_attempt", "image_publication_run_id",
        "preflight_artifact_id", "render_attestation_id", "web_attestation_id",
    ):
        if not isinstance(receipt.get(key), str) or not NUMERIC_ID_PATTERN.fullmatch(receipt[key]):
            fail("materialization-receipt-evidence-id-invalid")
    for key in (
        "final_artifact_sha256", "preflight_artifact_sha256", "release_hygiene_sha256",
    ):
        if not isinstance(receipt.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", receipt[key]):
            fail("materialization-receipt-evidence-digest-invalid")
    return receipt


def render_templates(config: dict[str, Any]) -> dict[str, bytes]:
    template_names = {
        "canary": "propertyquarry-release-single-host-v2-activation-canary.service",
        "socket": "propertyquarry-release-single-host-v2.socket",
        "service": "propertyquarry-release-single-host-v2@.service",
        "sysusers": "propertyquarry-release-single-host-v2.sysusers.conf.in",
        "tmpfiles": "propertyquarry-release-single-host-v2.tmpfiles.conf",
    }
    result: dict[str, bytes] = {}
    for key, name in template_names.items():
        raw = read_regular(TEMPLATE_DIRECTORY / name, 65_536)
        try:
            raw.decode("ascii", "strict")
        except UnicodeDecodeError:
            fail("template-not-ascii")
        if not raw.endswith(b"\n") or b"\r" in raw or b"\x00" in raw:
            fail("template-format-invalid")
        result[key] = raw
    sysusers = result["sysusers"]
    if (
        sysusers.count(b"@ALLOWED_RUNNER_UID@") != 1
        or sysusers.count(b"@ALLOWED_RUNNER_GID@") != 2
    ):
        fail("sysusers-template-invalid")
    sysusers = sysusers.replace(
        b"@ALLOWED_RUNNER_UID@", str(config["allowed_runner_uid"]).encode("ascii")
    ).replace(
        b"@ALLOWED_RUNNER_GID@", str(config["allowed_runner_gid"]).encode("ascii")
    )
    if b"@" in sysusers:
        fail("sysusers-template-unresolved")
    result["sysusers"] = sysusers
    service = result["service"]
    if (
        b"LoadCredentialEncrypted=github-api-token:" not in service
        or (
            b"LoadCredentialEncrypted=github-api-token:"
            b"/etc/propertyquarry-release-single-host-v2/github-api-token.cred"
            not in service
        )
        or b"StandardInput=socket" not in service
        or IDENTITY_ENV_PATH.encode("ascii") not in service
        or (
            "ReadWritePaths=" + DATABASE_RUNTIME_DIRECTORY
        ).encode("ascii") not in service
        or b"ReadOnlyPaths=/docker/property/state/runtime/propertyquarry_admission.env"
        not in service
        or (
            b"ReadWritePaths=/mnt/pcloud/propertyquarry/releases/backups/v2"
            not in service
        )
    ):
        fail("service-template-binding-invalid")
    canary = result["canary"]
    for required in (
        b"Type=oneshot",
        b"ExecStart=/usr/libexec/propertyquarry-release-control/"
        b"propertyquarry-release-single-host-v2 activation-probe",
        b"LoadCredentialEncrypted=github-api-token:",
        b"LoadCredential=activation-challenge:",
        b"LoadCredential=receipt-authority-key:",
        b"PrivateNetwork=no",
        b"LimitCORE=0",
    ):
        if required not in canary:
            fail("activation-canary-template-binding-invalid")
    if b"RemainAfterExit=yes" in canary:
        fail("activation-canary-template-binding-invalid")
    socket = result["socket"]
    if b"Accept=yes" not in socket or b"SocketMode=0660" not in socket:
        fail("socket-template-binding-invalid")
    return result


def validate_runner_assets(
    lock_raw: bytes, launcher: bytes, lifecycle: bytes
) -> None:
    lock = parse_strict_json(lock_raw, "runner-lock", trailing_newline=True)
    if set(lock) != {
        "architecture",
        "archive_bytes",
        "archive_sha256",
        "download_url",
        "filename",
        "platform",
        "schema",
        "version",
    }:
        fail("runner-lock-shape-invalid")
    if (
        lock["schema"]
        != "propertyquarry.release-control.single-host-runner-lock.v2"
        or lock["version"] != "2.335.1"
        or lock["platform"] != "linux"
        or lock["architecture"] != "x64"
        or lock["filename"] != "actions-runner-linux-x64-2.335.1.tar.gz"
        or lock["archive_bytes"] != 225628509
        or lock["archive_sha256"]
        != "4ef2f25285f0ae4477f1fe1e346db76d2f3ebf03824e2ddd1973a2819bf6c8cf"
        or lock["download_url"]
        != "https://github.com/actions/runner/releases/download/v2.335.1/"
        "actions-runner-linux-x64-2.335.1.tar.gz"
    ):
        fail("runner-lock-binding-invalid")
    try:
        launcher.decode("ascii", "strict")
    except UnicodeDecodeError:
        fail("runner-launcher-not-ascii")
    required_fragments = (
        b"#!/bin/bash\n",
        b"^pqrelease-[0-9a-f]{32}$",
        b"propertyquarry-runner-v2",
        b"--ephemeral --disableupdate",
        b"propertyquarry-release-controller-v2,${runner_label}",
        lock["archive_sha256"].encode("ascii"),
        str(lock["archive_bytes"]).encode("ascii"),
    )
    if (
        not launcher.endswith(b"\n")
        or b"\x00" in launcher
        or b"\r" in launcher
        or any(fragment not in launcher for fragment in required_fragments)
    ):
        fail("runner-launcher-binding-invalid")
    try:
        lifecycle.decode("ascii", "strict")
    except UnicodeDecodeError:
        fail("runner-lifecycle-not-ascii")
    lifecycle_fragments = (
        b"#!/bin/bash\n",
        b'[[ "$#" -eq 0 ]]',
        b'[[ "$EUID" == "0" && "${GROUPS[0]}" == "0" ]]',
        b"start_runner_token_broker() {",
        b"coproc RUNNER_TOKEN_BROKER {",
        b"IFS= read -r broker_gate || exit 50",
        b'[[ "$broker_gate" == "release-supervisor" ]] || exit 50',
        b'PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD=8',
        b"/proc/self/fd/8",
        b'"$controller" runner-supervise 8<&8',
        b"exec 8<&-",
        b"start_runner_token_broker\n",
        b"printf '%s\\n' 'release-supervisor' "
        b'>&"$supervisor_gate_fd"',
        b'run-propertyquarry-ephemeral-runner-v2',
        b'runner-ticket-admit',
        b'docker run --rm --pull never -i',
        b'--cap-drop ALL --security-opt no-new-privileges',
        lock["archive_sha256"].encode("ascii"),
        str(lock["archive_bytes"]).encode("ascii"),
    )
    broker_start = lifecycle.find(b"start_runner_token_broker() {\n")
    broker_end = lifecycle.find(
        b"}\n# End fixed runner token broker.\n",
        broker_start,
    )
    verify_start = lifecycle.find(b"verify_local_docker() {\n", broker_end)
    verify_end = lifecycle.find(b'\n}\n\n[[ "$#" -eq 0 ]]', verify_start)
    broker_call = lifecycle.find(b"\nstart_runner_token_broker\n", verify_end)
    first_external_check = lifecycle.find(
        b'\n[[ -f "$controller"',
        broker_call,
    )
    broker_close = lifecycle.find(b"\n  exec 8<&-\n", broker_start, broker_end)
    gate_read = lifecycle.find(
        b"IFS= read -r broker_gate || exit 50",
        broker_start,
        broker_end,
    )
    gate_check = lifecycle.find(
        b'[[ "$broker_gate" == "release-supervisor" ]] || exit 50',
        broker_start,
        broker_end,
    )
    marker_export = lifecycle.find(
        b"export PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD=8",
        broker_start,
        broker_end,
    )
    supervisor_exec = lifecycle.find(
        b'exec "$controller" runner-supervise 8<&8',
        broker_start,
        broker_end,
    )
    supervisor_exec_end = supervisor_exec + len(
        b'exec "$controller" runner-supervise 8<&8'
    )
    image_inspect = lifecycle.find(
        b'docker image inspect "$runner_image"',
        first_external_check,
    )
    release_gate = lifecycle.find(
        b"printf '%s\\n' 'release-supervisor' "
        b'>&"$supervisor_gate_fd"',
        image_inspect,
    )
    registration_read = lifecycle.find(
        b"IFS= read -r -t 300 registration_token",
        release_gate,
    )
    invocation_prelude = lifecycle[
        verify_end + len(b"\n}\n") : broker_call + 1
    ]
    if (
        not lifecycle.endswith(b"\n")
        or b"\x00" in lifecycle
        or b"\r" in lifecycle
        or any(fragment not in lifecycle for fragment in lifecycle_fragments)
        or min(
            broker_start,
            broker_end,
            verify_start,
            verify_end,
            broker_call,
            first_external_check,
            broker_close,
            gate_read,
            gate_check,
            marker_export,
            supervisor_exec,
            image_inspect,
            release_gate,
            registration_read,
        )
        < 0
        or not (
            broker_start
            < broker_end
            < verify_start
            < verify_end
            < broker_call
            < first_external_check
            < image_inspect
            < release_gate
            < registration_read
        )
        or not (
            broker_start
            < gate_read
            < gate_check
            < marker_export
            < supervisor_exec
            < broker_end
        )
        or lifecycle[gate_read:supervisor_exec_end]
        != (
            b"IFS= read -r broker_gate || exit 50\n"
            b'    [[ "$broker_gate" == "release-supervisor" ]] || exit 50\n'
            b"    unset HOME DOCKER_HOST\n"
            b"    export PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD=8\n"
            b'    exec "$controller" runner-supervise 8<&8'
        )
        or not broker_start < broker_close < broker_end
        or invocation_prelude
        != (
            b'\n[[ "$#" -eq 0 ]] || fail\n'
            b'[[ "$EUID" == "0" && "${GROUPS[0]}" == "0" ]] || fail\n'
        )
        or lifecycle.count(b"PROPERTYQUARRY_RUNNER_ADMIN_TOKEN_FD=8") != 1
        or b"$(id -u)" in lifecycle
        or b"coproc RUNNER_SUPERVISOR" in lifecycle
        or any(
            forbidden in lifecycle
            for forbidden in (b"eval ", b"bash -c", b"sh -c")
        )
    ):
        fail("runner-lifecycle-binding-invalid")


def load_runner_assets() -> tuple[bytes, bytes, bytes]:
    lock_raw = read_regular(MODULE_DIRECTORY / "runner.lock.json", MAX_JSON_BYTES)
    launcher = read_regular(
        MODULE_DIRECTORY / "tools" / "run-ephemeral-runner.sh",
        65_536,
        expected_modes=(0o755,),
    )
    lifecycle = read_regular(
        MODULE_DIRECTORY / "tools" / "run-ephemeral-runner-with-docker.sh",
        131_072,
        expected_modes=(0o755,),
    )
    validate_runner_assets(lock_raw, launcher, lifecycle)
    return lock_raw, launcher, lifecycle


def manifest_for(
    payloads: list[Payload],
    config: dict[str, Any],
    package_key_id: str,
    receipt_key_id: str,
    config_raw: bytes,
    plan_raw: bytes,
    build_receipt_raw: bytes,
) -> dict[str, Any]:
    entries = []
    for payload in sorted(payloads, key=lambda item: item.package_path):
        entries.append(
            {
                "install_path": payload.install_path,
                "mode": f"{payload.mode:04o}",
                "package_path": payload.package_path,
                "purpose": payload.purpose,
                "sha256": sha256(payload.data),
                "size": len(payload.data),
            }
        )
    return {
        "api_container_port": config["api_container_port"],
        "api_host_ip": config["api_host_ip"],
        "api_host_port": config["api_host_port"],
        "archive_format": ARCHIVE_FORMAT,
        "authority_profile": PROFILE,
        "backup_max_age_seconds": config["backup_max_age_seconds"],
        "build_receipt_digest": sha256(build_receipt_raw),
        "cloudflared_image": config["cloudflared_image"],
        "database_substrate_digest": config["database_substrate_digest"],
        "database_image": config["database_image"],
        "config_digest": sha256(config_raw),
        "deployment_id": config["deployment_id"],
        "envelope_sha": config["envelope_sha"],
        "files": entries,
        "host_machine_id_digest": config["host_machine_id_digest"],
        "installed_manifest_path": MANIFEST_INSTALL_PATH,
        "installed_manifest_signature_path": MANIFEST_SIGNATURE_INSTALL_PATH,
        "non_authoritative_until": NON_AUTHORITATIVE_UNTIL,
        "package_authority_key_id": package_key_id,
        "package_signing_private_key_included": False,
        "payload_root": "payload",
        "plan_digest": sha256(plan_raw),
        "post_purge_root_env_digest": config["post_purge_root_env_digest"],
        "pre_purge_root_env_digest": config["pre_purge_root_env_digest"],
        "pre_purge_runtime_inputs_digest": _canonical_digest(
            config["pre_purge_runtime_inputs"]
        ),
        "receipt_authority_key_id": receipt_key_id,
        "release_generation": config["release_generation"],
        "render_image": config["render_image"],
        "root_helper_verification_required": True,
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
        "runtime_deploy_digest": config["runtime_deploy_digest"],
        "runtime_inputs_digest": _canonical_digest(config["runtime_inputs"]),
        "runtime_retirement_digest": config["runtime_retirement_digest"],
        "runtime_sha": config["runtime_sha"],
        "workflow_sha": config["workflow_sha"],
        "schema": SCHEMA,
        "scene_video_env_digest": config["scene_video_env_digest"],
        "scene_video_env_gid": config["scene_video_env_gid"],
        "scene_video_env_mode": config["scene_video_env_mode"],
        "scene_video_env_path": config["scene_video_env_path"],
        "scene_video_env_uid": config["scene_video_env_uid"],
        "transaction_started_at_epoch": config["transaction_started_at_epoch"],
        "version": 2,
        "web_image": config["web_image"],
    }


def _tar_bytes(members: dict[str, tuple[int, bytes]]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(members):
            mode, data = members[name]
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def _safe_archive_name(name: str) -> bool:
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or "\x00" in name
        or len(name.encode("utf-8")) > 240
    ):
        return False
    path = PurePosixPath(name)
    return (
        all(part not in ("", ".", "..") for part in path.parts)
        and "/".join(path.parts) == name
    )


def _validate_manifest(
    manifest: dict[str, Any], package_key_id: str
) -> dict[str, dict[str, Any]]:
    required = {
        "api_container_port",
        "api_host_ip",
        "api_host_port",
        "archive_format",
        "authority_profile",
        "backup_max_age_seconds",
        "build_receipt_digest",
        "cloudflared_image",
        "config_digest",
        "database_image",
        "database_substrate_digest",
        "deployment_id",
        "envelope_sha",
        "files",
        "host_machine_id_digest",
        "installed_manifest_path",
        "installed_manifest_signature_path",
        "non_authoritative_until",
        "package_authority_key_id",
        "package_signing_private_key_included",
        "payload_root",
        "plan_digest",
        "post_purge_root_env_digest",
        "pre_purge_root_env_digest",
        "pre_purge_runtime_inputs_digest",
        "receipt_authority_key_id",
        "release_generation",
        "render_image",
        "root_helper_verification_required",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256",
        "runner_prerequisite_intent_sha256",
        "runner_prerequisite_job_id",
        "runtime_deploy_digest",
        "runtime_inputs_digest",
        "runtime_retirement_digest",
        "runtime_sha",
        "schema",
        "scene_video_env_digest",
        "scene_video_env_gid",
        "scene_video_env_mode",
        "scene_video_env_path",
        "scene_video_env_uid",
        "transaction_started_at_epoch",
        "version",
        "web_image",
        "workflow_sha",
    }
    if set(manifest) != required:
        fail("manifest-shape-invalid")
    if (
        manifest["schema"] != SCHEMA
        or manifest["version"] != 2
        or manifest["archive_format"] != ARCHIVE_FORMAT
        or manifest["authority_profile"] != PROFILE
        or manifest["payload_root"] != "payload"
        or manifest["non_authoritative_until"] != NON_AUTHORITATIVE_UNTIL
        or manifest["root_helper_verification_required"] is not True
        or manifest["package_signing_private_key_included"] is not False
        or manifest["installed_manifest_path"] != MANIFEST_INSTALL_PATH
        or manifest["installed_manifest_signature_path"]
        != MANIFEST_SIGNATURE_INSTALL_PATH
        or manifest["package_authority_key_id"] != package_key_id
        or manifest["api_host_ip"] != API_HOST_IP
        or manifest["api_host_port"] != API_HOST_PORT
        or manifest["api_container_port"] != API_CONTAINER_PORT
        or manifest["backup_max_age_seconds"] != BACKUP_MAX_AGE_SECONDS
        or manifest["database_image"] != DATABASE_IMAGE
        or not isinstance(manifest["web_image"], str)
        or not IMAGE_PATTERN.fullmatch(manifest["web_image"])
        or not manifest["web_image"].startswith(
            "ghcr.io/archonmegalon/propertyquarry-standalone-web-runtime@sha256:"
        )
        or not isinstance(manifest["render_image"], str)
        or not IMAGE_PATTERN.fullmatch(manifest["render_image"])
        or not manifest["render_image"].startswith(
            "ghcr.io/archonmegalon/propertyquarry-standalone-render-runtime@sha256:"
        )
        or manifest["web_image"] == manifest["render_image"]
        or not isinstance(manifest["cloudflared_image"], str)
        or not CLOUDFLARED_IMAGE_PATTERN.fullmatch(manifest["cloudflared_image"])
        or manifest["scene_video_env_path"] != SCENE_VIDEO_ENV_PATH
        or manifest["scene_video_env_mode"] != 384
        or manifest["scene_video_env_uid"] != 1000
        or manifest["scene_video_env_gid"] != 1000
    ):
        fail("manifest-binding-invalid")
    for key in (
        "build_receipt_digest",
        "config_digest",
        "database_substrate_digest",
        "host_machine_id_digest",
        "package_authority_key_id",
        "plan_digest",
        "post_purge_root_env_digest",
        "pre_purge_root_env_digest",
        "pre_purge_runtime_inputs_digest",
        "receipt_authority_key_id",
        "runner_prerequisite_approval_payload_sha256",
        "runner_prerequisite_approval_sha256",
        "runner_prerequisite_intent_sha256",
        "runtime_deploy_digest",
        "runtime_inputs_digest",
        "runtime_retirement_digest",
        "scene_video_env_digest",
    ):
        if not isinstance(manifest[key], str) or not SHA256_PATTERN.fullmatch(
            manifest[key]
        ):
            fail(f"manifest-{key}-invalid")
    if (
        not isinstance(manifest["deployment_id"], str)
        or not DEPLOYMENT_ID_PATTERN.fullmatch(manifest["deployment_id"])
    ):
        fail("manifest-deployment-id-invalid")
    if (
        not isinstance(manifest["runner_prerequisite_job_id"], str)
        or not NUMERIC_ID_PATTERN.fullmatch(manifest["runner_prerequisite_job_id"])
    ):
        fail("manifest-runner-prerequisite-job-id-invalid")
    if not isinstance(manifest["runtime_sha"], str) or not GIT_SHA_PATTERN.fullmatch(
        manifest["runtime_sha"]
    ):
        fail("manifest-runtime_sha-invalid")
    if (
        not isinstance(manifest["workflow_sha"], str)
        or not GIT_SHA_PATTERN.fullmatch(manifest["workflow_sha"])
        or manifest["workflow_sha"] == manifest["runtime_sha"]
    ):
        fail("manifest-workflow_sha-invalid")
    if not isinstance(
        manifest["envelope_sha"], str
    ) or not ENVELOPE_SHA_PATTERN.fullmatch(manifest["envelope_sha"]):
        fail("manifest-envelope_sha-invalid")
    if (
        not isinstance(manifest["release_generation"], int)
        or isinstance(manifest["release_generation"], bool)
        or not 1 <= manifest["release_generation"] <= (1 << 62)
    ):
        fail("manifest-generation-invalid")
    if (
        not isinstance(manifest["transaction_started_at_epoch"], int)
        or isinstance(manifest["transaction_started_at_epoch"], bool)
        or not 1 <= manifest["transaction_started_at_epoch"] <= (1 << 62)
    ):
        fail("manifest-transaction-start-invalid")
    files = manifest["files"]
    if not isinstance(files, list) or len(files) != len(PAYLOAD_LAYOUT):
        fail("manifest-files-invalid")
    expected_order = []
    entries: dict[str, dict[str, Any]] = {}
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {
            "install_path",
            "mode",
            "package_path",
            "purpose",
            "sha256",
            "size",
        }:
            fail("manifest-file-shape-invalid")
        install_path = entry.get("install_path")
        if not isinstance(install_path, str) or install_path not in PAYLOAD_LAYOUT:
            fail("manifest-install-path-invalid")
        purpose, mode = PAYLOAD_LAYOUT[install_path]
        package_path = "payload" + install_path
        if (
            entry["purpose"] != purpose
            or entry["mode"] != f"{mode:04o}"
            or entry["package_path"] != package_path
            or not _safe_archive_name(package_path)
            or not isinstance(entry["size"], int)
            or isinstance(entry["size"], bool)
            or entry["size"] < 1
            or entry["size"] > MAX_BINARY_BYTES
            or not isinstance(entry["sha256"], str)
            or not SHA256_PATTERN.fullmatch(entry["sha256"])
            or package_path in entries
        ):
            fail("manifest-file-binding-invalid")
        entries[package_path] = entry
        expected_order.append(package_path)
    if set(entries) != {"payload" + path for path in PAYLOAD_LAYOUT}:
        fail("manifest-file-set-invalid")
    if expected_order != sorted(expected_order):
        fail("manifest-file-order-invalid")
    for install_path, expected_digest, expected_size in (
        (
            PREDEPLOY_BACKUP_HELPER_PATH,
            PREDEPLOY_BACKUP_HELPER_SHA256,
            PREDEPLOY_BACKUP_HELPER_BYTES,
        ),
        (
            DATABASE_CONTROL_HELPER_PATH,
            DATABASE_CONTROL_HELPER_SHA256,
            DATABASE_CONTROL_HELPER_BYTES,
        ),
        (
            RUNTIME_DATABASE_HELPER_PATH,
            RUNTIME_DATABASE_HELPER_SHA256,
            RUNTIME_DATABASE_HELPER_BYTES,
        ),
        (
            RUNTIME_ISOLATION_HELPER_PATH,
            RUNTIME_ISOLATION_HELPER_SHA256,
            RUNTIME_ISOLATION_HELPER_BYTES,
        ),
        (
            RUNTIME_DEPLOY_HELPER_PATH,
            RUNTIME_DEPLOY_HELPER_SHA256,
            RUNTIME_DEPLOY_HELPER_BYTES,
        ),
    ):
        entry = entries["payload" + install_path]
        if entry["sha256"] != expected_digest or entry["size"] != expected_size:
            fail("manifest-sealed-helper-binding-invalid")
    return entries


def build_package(arguments: argparse.Namespace) -> dict[str, Any]:
    binary = read_regular(arguments.binary, MAX_BINARY_BYTES)
    predeploy_backup_helper = read_regular(
        arguments.predeploy_backup_helper,
        MAX_BINARY_BYTES,
        expected_modes=(0o755,),
    )
    database_control_helper = read_regular(
        arguments.database_control_helper,
        MAX_BINARY_BYTES,
        expected_modes=(0o755,),
    )
    runtime_database_helper = read_regular(
        arguments.runtime_database_helper,
        MAX_BINARY_BYTES,
        expected_modes=(0o755,),
    )
    runtime_isolation_helper = read_regular(
        arguments.runtime_isolation_helper,
        MAX_BINARY_BYTES,
        expected_modes=(0o755,),
    )
    runtime_deploy_helper = read_regular(
        arguments.runtime_deploy_helper,
        MAX_BINARY_BYTES,
        expected_modes=(0o755,),
    )
    validate_sealed_helper(
        predeploy_backup_helper,
        expected_sha256=PREDEPLOY_BACKUP_HELPER_SHA256,
        expected_bytes=PREDEPLOY_BACKUP_HELPER_BYTES,
        label="predeploy-backup-helper",
    )
    validate_sealed_helper(
        database_control_helper,
        expected_sha256=DATABASE_CONTROL_HELPER_SHA256,
        expected_bytes=DATABASE_CONTROL_HELPER_BYTES,
        label="database-control-helper",
    )
    validate_sealed_helper(
        runtime_database_helper,
        expected_sha256=RUNTIME_DATABASE_HELPER_SHA256,
        expected_bytes=RUNTIME_DATABASE_HELPER_BYTES,
        label="runtime-database-helper",
    )
    validate_sealed_helper(
        runtime_isolation_helper,
        expected_sha256=RUNTIME_ISOLATION_HELPER_SHA256,
        expected_bytes=RUNTIME_ISOLATION_HELPER_BYTES,
        label="runtime-isolation-helper",
    )
    validate_sealed_helper(
        runtime_deploy_helper,
        expected_sha256=RUNTIME_DEPLOY_HELPER_SHA256,
        expected_bytes=RUNTIME_DEPLOY_HELPER_BYTES,
        label="runtime-deploy-helper",
    )
    build_receipt_raw = read_regular(arguments.build_receipt, MAX_JSON_BYTES)
    config_raw = read_regular(arguments.config, MAX_JSON_BYTES)
    config_signature = read_regular(arguments.config_signature, 64)
    plan_raw = read_regular(arguments.plan, MAX_JSON_BYTES)
    materialization_receipt_raw = read_regular(
        arguments.materialization_receipt, MAX_JSON_BYTES
    )
    materialization_receipt_signature = read_regular(
        arguments.materialization_receipt_signature, 64
    )
    runner_reservation_raw = read_regular(arguments.runner_reservation, MAX_JSON_BYTES)
    runner_launch_ticket_raw = read_regular(
        arguments.runner_launch_ticket, MAX_JSON_BYTES
    )
    runner_prerequisite_intent_raw = read_regular(
        arguments.runner_prerequisite_intent, MAX_JSON_BYTES
    )
    runner_prerequisite_approval_raw = read_regular(
        arguments.runner_prerequisite_approval, MAX_JSON_BYTES
    )
    package_public_raw = read_regular(arguments.package_authority_public_key, 4096)
    package_private_raw = read_regular(
        arguments.package_authority_private_key, 4096, private=True
    )
    receipt_public_raw = read_regular(arguments.receipt_authority_public_key, 4096)
    receipt_private_raw = read_regular(
        arguments.receipt_authority_private_key, 4096, private=True
    )
    package_public, package_public_raw, package_key_id = load_public_key(
        package_public_raw, "package-public-key"
    )
    package_private, _ = load_private_key(
        package_private_raw, "package-private-key"
    )
    if package_private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) != package_public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ):
        fail("package-key-pair-mismatch")
    receipt_public, receipt_public_raw, _ = load_public_key(
        receipt_public_raw, "receipt-public-key"
    )
    receipt_private, receipt_private_raw = load_private_key(
        receipt_private_raw, "receipt-private-key"
    )
    if receipt_private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) != receipt_public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ):
        fail("receipt-key-pair-mismatch")
    config, plan, receipt_key_id = validate_config_and_plan(
        config_raw,
        config_signature,
        plan_raw,
        package_public,
        package_key_id,
        receipt_public,
    )
    runner_material = validate_runner_material(
        reservation_raw=runner_reservation_raw,
        ticket_raw=runner_launch_ticket_raw,
        config=config,
        config_raw=config_raw,
        plan_raw=plan_raw,
        receipt_public=receipt_public,
        receipt_key_id=receipt_key_id,
    )
    prerequisite_material = validate_runner_prerequisite_material(
        intent_raw=runner_prerequisite_intent_raw,
        approval_raw=runner_prerequisite_approval_raw,
        reservation_raw=runner_reservation_raw,
        config=config,
        receipt_public=receipt_public,
        receipt_key_id=receipt_key_id,
    )
    runner_material.update(prerequisite_material)
    validate_materialization_receipt(
        materialization_receipt_raw,
        materialization_receipt_signature,
        config=config,
        config_raw=config_raw,
        plan_raw=plan_raw,
        package_public=package_public,
        package_key_id=package_key_id,
        receipt_key_id=receipt_key_id,
        runner_material=runner_material,
    )
    if plan["executables"].get(PREDEPLOY_BACKUP_HELPER_PATH) != sha256(
        predeploy_backup_helper
    ):
        fail("predeploy-backup-helper-plan-binding-invalid")
    if plan["executables"].get(DATABASE_CONTROL_HELPER_PATH) != sha256(
        database_control_helper
    ):
        fail("database-control-helper-plan-binding-invalid")
    if plan["executables"].get(RUNTIME_ISOLATION_HELPER_PATH) != sha256(
        runtime_isolation_helper
    ):
        fail("runtime-isolation-helper-plan-binding-invalid")
    if plan["executables"].get(RUNTIME_DEPLOY_HELPER_PATH) != sha256(
        runtime_deploy_helper
    ):
        fail("runtime-deploy-helper-plan-binding-invalid")
    build_receipt = parse_strict_json(
        build_receipt_raw, "build-receipt", trailing_newline=True
    )
    validate_build_receipt(build_receipt, binary, package_key_id)
    templates = render_templates(config)
    runner_lock_raw, runner_launcher, runner_lifecycle = load_runner_assets()
    by_path = {
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-single-host-v2": binary,
        "/etc/propertyquarry-release-single-host-v2/"
        "native-build-receipt.v2.json": build_receipt_raw,
        "/etc/propertyquarry-release-single-host-v2/authority.v2.json": config_raw,
        "/etc/propertyquarry-release-single-host-v2/authority.v2.sig": config_signature,
        "/etc/propertyquarry-release-single-host-v2/transaction-plan.v2.json": plan_raw,
        "/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.json": materialization_receipt_raw,
        "/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.sig": materialization_receipt_signature,
        "/etc/propertyquarry-release-single-host-v2/package-authority-v2.pem": package_public_raw,
        "/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key": receipt_private_raw,
        "/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem": receipt_public_raw,
        "/var/lib/propertyquarry-release-single-host-v2/runner-launch-ticket.v2.json": runner_launch_ticket_raw,
        "/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json": runner_reservation_raw,
        "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-intent.v2.json": runner_prerequisite_intent_raw,
        "/var/lib/propertyquarry-release-single-host-v2/runner-prerequisite-approval.v2.json": runner_prerequisite_approval_raw,
        "/usr/lib/systemd/system/propertyquarry-release-single-host-v2.socket": templates[
            "socket"
        ],
        "/usr/lib/systemd/system/propertyquarry-release-single-host-v2@.service": templates[
            "service"
        ],
        "/usr/lib/systemd/system/"
        "propertyquarry-release-single-host-v2-activation-canary.service": templates[
            "canary"
        ],
        "/usr/lib/sysusers.d/propertyquarry-release-single-host-v2.conf": templates[
            "sysusers"
        ],
        "/usr/lib/tmpfiles.d/propertyquarry-release-single-host-v2.conf": templates[
            "tmpfiles"
        ],
        "/usr/lib/propertyquarry-release-runner-v2/runner.lock.json": runner_lock_raw,
        "/usr/libexec/propertyquarry-release-control/"
        "run-propertyquarry-ephemeral-runner-v2": runner_launcher,
        RUNNER_LIFECYCLE_INSTALL_PATH: runner_lifecycle,
        PREDEPLOY_BACKUP_HELPER_PATH: predeploy_backup_helper,
        DATABASE_CONTROL_HELPER_PATH: database_control_helper,
        RUNTIME_DATABASE_HELPER_PATH: runtime_database_helper,
        RUNTIME_ISOLATION_HELPER_PATH: runtime_isolation_helper,
        RUNTIME_DEPLOY_HELPER_PATH: runtime_deploy_helper,
    }
    if set(by_path) != set(PAYLOAD_LAYOUT):
        fail("internal-payload-layout-invalid")
    payloads = [
        Payload(path, PAYLOAD_LAYOUT[path][0], PAYLOAD_LAYOUT[path][1], by_path[path])
        for path in PAYLOAD_LAYOUT
    ]
    manifest = manifest_for(
        payloads,
        config,
        package_key_id,
        receipt_key_id,
        config_raw,
        plan_raw,
        build_receipt_raw,
    )
    manifest_raw = canonical_json(manifest)
    manifest_signature = package_private.sign(
        framed(MANIFEST_SIGNATURE_DOMAIN, manifest_raw)
    )
    members: dict[str, tuple[int, bytes]] = {
        "manifest.v2.json": (0o444, manifest_raw),
        "manifest.v2.sig": (0o444, manifest_signature),
    }
    for payload in payloads:
        members[payload.package_path] = (payload.mode, payload.data)
    archive_raw = _tar_bytes(members)
    # The transport contains the receipt-signing private key.  Keep the archive
    # owner-readable only even though every public payload mode is independently
    # declared in the signed manifest.
    write_new_file(arguments.output, archive_raw, 0o400)
    return {
        "authoritative": False,
        "config_digest": sha256(config_raw),
        "manifest_sha256": sha256(manifest_raw),
        "non_authoritative_until": NON_AUTHORITATIVE_UNTIL,
        "package_authority_key_id": package_key_id,
        "package_sha256": sha256(archive_raw),
        "performs_release_effects": False,
        "production_ready": False,
        "root_install_performed": False,
        "schema": "propertyquarry.release-control.single-host-package-build-result.v2",
        "version": 2,
    }


def verify_package(
    package_path: str, package_public_path: str
) -> VerifiedPackage:
    archive_raw = read_regular(
        package_path, MAX_ARCHIVE_BYTES, expected_modes=(0o400,)
    )
    package_public_raw = read_regular(package_public_path, 4096)
    package_public, package_public_raw, package_key_id = load_public_key(
        package_public_raw, "package-public-key"
    )
    try:
        archive = tarfile.open(fileobj=io.BytesIO(archive_raw), mode="r:")
    except tarfile.TarError:
        fail("archive-invalid")
    with archive:
        if archive.pax_headers:
            fail("archive-pax-header-invalid")
        infos = archive.getmembers()
        if not 1 <= len(infos) <= MAX_MEMBERS:
            fail("archive-member-count-invalid")
        members: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        for info in infos:
            if (
                not _safe_archive_name(info.name)
                or info.name in members
                or info.type != tarfile.REGTYPE
                or not info.isfile()
                or info.uid != 0
                or info.gid != 0
                or info.uname != ""
                or info.gname != ""
                or info.mtime != 0
                or info.linkname != ""
                or info.pax_headers
                or info.size < 1
                or info.size > MAX_BINARY_BYTES
                or info.mode & ~0o777
            ):
                fail("archive-member-invalid")
            extracted = archive.extractfile(info)
            if extracted is None:
                fail("archive-member-unreadable")
            data = extracted.read(info.size + 1)
            if len(data) != info.size:
                fail("archive-member-size-invalid")
            members[info.name] = data
            modes[info.name] = info.mode
    if "manifest.v2.json" not in members or "manifest.v2.sig" not in members:
        fail("archive-manifest-missing")
    manifest_raw = members["manifest.v2.json"]
    manifest_signature = members["manifest.v2.sig"]
    if len(manifest_signature) != 64:
        fail("manifest-signature-size-invalid")
    manifest = parse_strict_json(manifest_raw, "manifest")
    try:
        package_public.verify(
            manifest_signature, framed(MANIFEST_SIGNATURE_DOMAIN, manifest_raw)
        )
    except InvalidSignature:
        fail("manifest-signature-invalid")
    entries = _validate_manifest(manifest, package_key_id)
    expected_names = {"manifest.v2.json", "manifest.v2.sig", *entries}
    if set(members) != expected_names:
        fail("archive-member-set-invalid")
    if modes["manifest.v2.json"] != 0o444 or modes["manifest.v2.sig"] != 0o444:
        fail("archive-manifest-mode-invalid")
    for package_name, entry in entries.items():
        data = members[package_name]
        expected_mode = int(entry["mode"], 8)
        if (
            modes[package_name] != expected_mode
            or len(data) != entry["size"]
            or sha256(data) != entry["sha256"]
        ):
            fail("archive-payload-binding-invalid")
    anchor_path = "payload/etc/propertyquarry-release-single-host-v2/package-authority-v2.pem"
    bundled_public, bundled_public_raw, bundled_key_id = load_public_key(
        members[anchor_path], "bundled-package-anchor"
    )
    if (
        bundled_public_raw != package_public_raw
        or bundled_key_id != package_key_id
        or bundled_public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        != package_public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ):
        fail("bundled-package-anchor-mismatch")
    receipt_public_path = (
        "payload/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.pem"
    )
    receipt_private_path = (
        "payload/etc/propertyquarry-release-single-host-v2/receipt-authority-v2.key"
    )
    receipt_public, _, _ = load_public_key(
        members[receipt_public_path], "bundled-receipt-anchor"
    )
    receipt_private, _ = load_private_key(
        members[receipt_private_path], "bundled-receipt-key"
    )
    if receipt_private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ) != receipt_public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    ):
        fail("bundled-receipt-key-pair-mismatch")
    config_path = (
        "payload/etc/propertyquarry-release-single-host-v2/authority.v2.json"
    )
    config_signature_path = (
        "payload/etc/propertyquarry-release-single-host-v2/authority.v2.sig"
    )
    plan_path = (
        "payload/etc/propertyquarry-release-single-host-v2/transaction-plan.v2.json"
    )
    materialization_receipt_path = (
        "payload/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.json"
    )
    materialization_signature_path = (
        "payload/etc/propertyquarry-release-single-host-v2/materialization-receipt.v2.sig"
    )
    runner_reservation_path = (
        "payload/var/lib/propertyquarry-release-single-host-v2/runner-reservation.v2.json"
    )
    runner_launch_ticket_path = (
        "payload/var/lib/propertyquarry-release-single-host-v2/runner-launch-ticket.v2.json"
    )
    runner_prerequisite_intent_path = (
        "payload/var/lib/propertyquarry-release-single-host-v2/"
        "runner-prerequisite-intent.v2.json"
    )
    runner_prerequisite_approval_path = (
        "payload/var/lib/propertyquarry-release-single-host-v2/"
        "runner-prerequisite-approval.v2.json"
    )
    binary_path = (
        "payload/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-release-single-host-v2"
    )
    build_receipt_path = (
        "payload/etc/propertyquarry-release-single-host-v2/"
        "native-build-receipt.v2.json"
    )
    config, plan, receipt_key_id = validate_config_and_plan(
        members[config_path],
        members[config_signature_path],
        members[plan_path],
        package_public,
        package_key_id,
        receipt_public,
    )
    runner_material = validate_runner_material(
        reservation_raw=members[runner_reservation_path],
        ticket_raw=members[runner_launch_ticket_path],
        config=config,
        config_raw=members[config_path],
        plan_raw=members[plan_path],
        receipt_public=receipt_public,
        receipt_key_id=receipt_key_id,
    )
    prerequisite_material = validate_runner_prerequisite_material(
        intent_raw=members[runner_prerequisite_intent_path],
        approval_raw=members[runner_prerequisite_approval_path],
        reservation_raw=members[runner_reservation_path],
        config=config,
        receipt_public=receipt_public,
        receipt_key_id=receipt_key_id,
    )
    runner_material.update(prerequisite_material)
    validate_materialization_receipt(
        members[materialization_receipt_path],
        members[materialization_signature_path],
        config=config,
        config_raw=members[config_path],
        plan_raw=members[plan_path],
        package_public=package_public,
        package_key_id=package_key_id,
        receipt_key_id=receipt_key_id,
        runner_material=runner_material,
    )
    receipt = parse_strict_json(
        members[build_receipt_path], "build-receipt", trailing_newline=True
    )
    validate_build_receipt(receipt, members[binary_path], package_key_id)
    expected_templates = render_templates(config)
    packaged_templates = {
        "canary": members[
            "payload/usr/lib/systemd/system/"
            "propertyquarry-release-single-host-v2-activation-canary.service"
        ],
        "socket": members[
            "payload/usr/lib/systemd/system/"
            "propertyquarry-release-single-host-v2.socket"
        ],
        "service": members[
            "payload/usr/lib/systemd/system/"
            "propertyquarry-release-single-host-v2@.service"
        ],
        "sysusers": members[
            "payload/usr/lib/sysusers.d/"
            "propertyquarry-release-single-host-v2.conf"
        ],
        "tmpfiles": members[
            "payload/usr/lib/tmpfiles.d/"
            "propertyquarry-release-single-host-v2.conf"
        ],
    }
    if packaged_templates != expected_templates:
        fail("packaged-template-binding-invalid")
    validate_runner_assets(
        members[
            "payload/usr/lib/propertyquarry-release-runner-v2/runner.lock.json"
        ],
        members[
            "payload/usr/libexec/propertyquarry-release-control/"
            "run-propertyquarry-ephemeral-runner-v2"
        ],
        members["payload" + RUNNER_LIFECYCLE_INSTALL_PATH],
    )
    backup_helper_path = (
        "payload/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-predeploy-backup-v2"
    )
    if plan["executables"].get(
        "/usr/libexec/propertyquarry-release-control/"
        "propertyquarry-predeploy-backup-v2"
    ) != sha256(members[backup_helper_path]):
        fail("predeploy-backup-helper-plan-binding-invalid")
    for helper_path, label in (
        (DATABASE_CONTROL_HELPER_PATH, "database-control"),
        (RUNTIME_ISOLATION_HELPER_PATH, "runtime-isolation"),
        (RUNTIME_DEPLOY_HELPER_PATH, "runtime-deploy"),
    ):
        package_path = "payload" + helper_path
        if plan["executables"].get(helper_path) != sha256(members[package_path]):
            fail(f"{label}-helper-plan-binding-invalid")
    if (
        manifest["config_digest"] != sha256(members[config_path])
        or manifest["plan_digest"] != sha256(members[plan_path])
        or manifest["build_receipt_digest"] != sha256(members[build_receipt_path])
        or manifest["receipt_authority_key_id"] != receipt_key_id
        or manifest["runtime_sha"] != config["runtime_sha"]
        or manifest["workflow_sha"] != config["workflow_sha"]
        or manifest["deployment_id"] != config["deployment_id"]
        or manifest["transaction_started_at_epoch"]
        != config["transaction_started_at_epoch"]
        or manifest["backup_max_age_seconds"]
        != config["backup_max_age_seconds"]
        or manifest["envelope_sha"] != config["envelope_sha"]
        or manifest["release_generation"] != config["release_generation"]
        or manifest["host_machine_id_digest"]
        != config["host_machine_id_digest"]
        or manifest["api_host_ip"] != config["api_host_ip"]
        or manifest["api_host_port"] != config["api_host_port"]
        or manifest["api_container_port"] != config["api_container_port"]
        or manifest["cloudflared_image"] != config["cloudflared_image"]
        or manifest["database_image"] != config["database_image"]
        or manifest["web_image"] != config["web_image"]
        or manifest["render_image"] != config["render_image"]
        or manifest["pre_purge_root_env_digest"]
        != config["pre_purge_root_env_digest"]
        or manifest["post_purge_root_env_digest"]
        != config["post_purge_root_env_digest"]
        or manifest["pre_purge_runtime_inputs_digest"]
        != _canonical_digest(config["pre_purge_runtime_inputs"])
        or manifest["runtime_inputs_digest"]
        != _canonical_digest(config["runtime_inputs"])
        or manifest["runtime_retirement_digest"]
        != config["runtime_retirement_digest"]
        or manifest["runtime_deploy_digest"]
        != config["runtime_deploy_digest"]
        or manifest["database_substrate_digest"]
        != config["database_substrate_digest"]
        or manifest["runner_prerequisite_approval_payload_sha256"]
        != config["runner_prerequisite_approval_payload_sha256"]
        or manifest["runner_prerequisite_approval_sha256"]
        != config["runner_prerequisite_approval_sha256"]
        or manifest["runner_prerequisite_intent_sha256"]
        != config["runner_prerequisite_intent_sha256"]
        or manifest["runner_prerequisite_job_id"]
        != config["runner_prerequisite_job_id"]
        or manifest["scene_video_env_path"] != config["scene_video_env_path"]
        or manifest["scene_video_env_mode"] != config["scene_video_env_mode"]
        or manifest["scene_video_env_uid"] != config["scene_video_env_uid"]
        or manifest["scene_video_env_gid"] != config["scene_video_env_gid"]
        or manifest["scene_video_env_digest"]
        != config["scene_video_env_digest"]
    ):
        fail("manifest-profile-binding-invalid")
    exact_members = {
        name: (modes[name], data) for name, data in members.items()
    }
    if _tar_bytes(exact_members) != archive_raw:
        fail("archive-not-deterministic-ustar")
    return VerifiedPackage(
        archive_sha256=sha256(archive_raw),
        manifest_sha256=sha256(manifest_raw),
        manifest=manifest,
        members=members,
        modes=modes,
    )


def write_new_file(path_value: str, raw: bytes, mode: int) -> None:
    path = Path(path_value)
    if not path.is_absolute() or path.name in ("", ".", ".."):
        fail("output-path-invalid")
    try:
        parent = path.parent.resolve(strict=True)
    except OSError:
        fail("output-parent-invalid")
    if parent != path.parent or not parent.is_dir() or path.exists() or path.is_symlink():
        fail("output-path-invalid")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=os.fspath(parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(raw)
        written = 0
        while written < len(raw):
            count = os.write(descriptor, view[written:])
            if count < 1:
                fail("output-short-write")
            written += count
        os.fsync(descriptor)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            fail("output-already-exists")
        os.unlink(temporary)
        temporary = ""
        parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def stage_package(verified: VerifiedPackage, output_value: str) -> None:
    output = Path(output_value)
    if not output.is_absolute() or output.name in ("", ".", ".."):
        fail("stage-path-invalid")
    try:
        parent = output.parent.resolve(strict=True)
    except OSError:
        fail("stage-parent-invalid")
    if parent != output.parent or not parent.is_dir():
        fail("stage-parent-invalid")
    try:
        os.mkdir(output, 0o700)
    except FileExistsError:
        fail("stage-already-exists")
    try:
        created_directories: set[Path] = {output}
        for name in sorted(verified.members):
            destination = output.joinpath(*PurePosixPath(name).parts)
            relative_parent = destination.parent.relative_to(output)
            current = output
            for part in relative_parent.parts:
                current = current / part
                try:
                    os.mkdir(current, 0o700)
                    created_directories.add(current)
                except FileExistsError:
                    metadata = os.lstat(current)
                    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(
                        metadata.st_mode
                    ):
                        fail("stage-parent-component-invalid")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(destination, flags, verified.modes[name])
            try:
                data = verified.members[name]
                view = memoryview(data)
                written = 0
                while written < len(data):
                    count = os.write(descriptor, view[written:])
                    if count < 1:
                        fail("stage-short-write")
                    written += count
                os.fchmod(descriptor, verified.modes[name])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        for directory in sorted(
            created_directories, key=lambda item: len(item.parts), reverse=True
        ):
            descriptor = os.open(
                directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        parent_descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except Exception:
        shutil.rmtree(output)
        raise


def result_for_verified(verified: VerifiedPackage, schema: str) -> dict[str, Any]:
    return {
        "authoritative": False,
        "config_digest": verified.manifest["config_digest"],
        "manifest_sha256": verified.manifest_sha256,
        "non_authoritative_until": NON_AUTHORITATIVE_UNTIL,
        "package_authority_key_id": verified.manifest["package_authority_key_id"],
        "package_sha256": verified.archive_sha256,
        "performs_release_effects": False,
        "production_ready": False,
        "root_install_performed": False,
        "schema": schema,
        "version": 2,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Build or verify a non-authoritative PropertyQuarry v2 package"
    )
    commands = result.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--binary", required=True)
    build.add_argument("--predeploy-backup-helper", required=True)
    build.add_argument("--database-control-helper", required=True)
    build.add_argument("--runtime-database-helper", required=True)
    build.add_argument("--runtime-isolation-helper", required=True)
    build.add_argument("--runtime-deploy-helper", required=True)
    build.add_argument("--build-receipt", required=True)
    build.add_argument("--config", required=True)
    build.add_argument("--config-signature", required=True)
    build.add_argument("--plan", required=True)
    build.add_argument("--materialization-receipt", required=True)
    build.add_argument("--materialization-receipt-signature", required=True)
    build.add_argument("--runner-reservation", required=True)
    build.add_argument("--runner-launch-ticket", required=True)
    build.add_argument("--runner-prerequisite-intent", required=True)
    build.add_argument("--runner-prerequisite-approval", required=True)
    build.add_argument("--package-authority-public-key", required=True)
    build.add_argument("--package-authority-private-key", required=True)
    build.add_argument("--receipt-authority-public-key", required=True)
    build.add_argument("--receipt-authority-private-key", required=True)
    build.add_argument("--output", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--package", required=True)
    verify.add_argument("--package-authority-public-key", required=True)
    stage = commands.add_parser("stage")
    stage.add_argument("--package", required=True)
    stage.add_argument("--package-authority-public-key", required=True)
    stage.add_argument("--output", required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = parser().parse_args(argv)
        if arguments.command == "build":
            result = build_package(arguments)
        else:
            verified = verify_package(
                arguments.package, arguments.package_authority_public_key
            )
            if arguments.command == "stage":
                stage_package(verified, arguments.output)
                result = result_for_verified(
                    verified,
                    "propertyquarry.release-control.single-host-package-stage-result.v2",
                )
            else:
                result = result_for_verified(
                    verified,
                    "propertyquarry.release-control.single-host-package-verify-result.v2",
                )
        sys.stdout.buffer.write(canonical_json(result) + b"\n")
        return 0
    except PackageFailure as error:
        sys.stderr.write(f"propertyquarry-package-rejected:{error}\n")
        return 50
    except KeyboardInterrupt:
        sys.stderr.write("propertyquarry-package-rejected:interrupted\n")
        return 50
    except Exception:
        sys.stderr.write("propertyquarry-package-rejected:internal-failure\n")
        return 50


if __name__ == "__main__":
    raise SystemExit(main())
