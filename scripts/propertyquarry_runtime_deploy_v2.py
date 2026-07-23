#!/usr/bin/env python3
"""Execute the one sealed PropertyQuarry Compose deployment transaction."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import BinaryIO, Mapping, Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


SCHEMA = "propertyquarry.runtime-deploy-receipt.v2"
SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runtime-deploy-receipt-"
    b"signature.v2\x00"
)
AUTHORITY_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-profile-signature.v2\x00"
)
MANIFEST_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-package-manifest-signature.v2\x00"
)
BACKUP_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-predeploy-backup-receipt-"
    b"signature.v2\x00"
)
ISOLATION_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runtime-isolation-receipt-"
    b"signature.v2\x00"
)
DATABASE_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-database-receipt-signature.v2\x00"
)

AUTHORITY_SCHEMA = "propertyquarry.release-control.single-host-profile.v2"
PLAN_SCHEMA = "propertyquarry.release-control.single-host-transaction-plan.v2"
MANIFEST_SCHEMA = "propertyquarry.release-control.single-host-package.v2"
BACKUP_SCHEMA = "propertyquarry.predeploy-backup-receipt.v2"
ISOLATION_SCHEMA = "propertyquarry.runtime-isolation-receipt.v2"
DATABASE_SCHEMA = "propertyquarry.database-control-receipt.v2"
LEGACY_REGISTRATION_EMAIL_KEY_COUNT = 8
REGISTRATION_EMAIL_KEY_COUNT = 10

INSTALL_ROOT = Path("/etc/propertyquarry-release-single-host-v2")
AUTHORITY_PATH = INSTALL_ROOT / "authority.v2.json"
AUTHORITY_SIGNATURE_PATH = INSTALL_ROOT / "authority.v2.sig"
PLAN_PATH = INSTALL_ROOT / "transaction-plan.v2.json"
MANIFEST_PATH = INSTALL_ROOT / "package-manifest.v2.json"
MANIFEST_SIGNATURE_PATH = INSTALL_ROOT / "package-manifest.v2.sig"
PACKAGE_PUBLIC_KEY_PATH = INSTALL_ROOT / "package-authority-v2.pem"
RECEIPT_PRIVATE_KEY_PATH = INSTALL_ROOT / "receipt-authority-v2.key"
RECEIPT_PUBLIC_KEY_PATH = INSTALL_ROOT / "receipt-authority-v2.pem"
MACHINE_ID_PATH = Path("/etc/machine-id")

DEPLOY_RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/deploy-receipts"
)
BACKUP_RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/backup-receipts"
)
ISOLATION_RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/isolation-receipts"
)
DATABASE_RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/database-receipts"
)

PROPERTY_ROOT = Path("/docker/property")
DOCKER_EXECUTABLE = Path("/usr/bin/docker")
COMPOSE_PLUGIN = Path("/usr/libexec/docker/cli-plugins/docker-compose")
COMPOSE_FILES = (
    PROPERTY_ROOT / "docker-compose.property.yml",
    PROPERTY_ROOT / "docker-compose.cloudflared.yml",
)
ENV_FILES = (
    PROPERTY_ROOT / ".env",
    PROPERTY_ROOT / "state/runtime/property_scene_video_shared.env",
    PROPERTY_ROOT / "state/runtime/propertyquarry_database_roles.env",
    PROPERTY_ROOT / "state/runtime/propertyquarry_admission.env",
    PROPERTY_ROOT / "state/runtime/propertyquarry_google_identity.env",
    PROPERTY_ROOT / "state/runtime/propertyquarry_registration_email.env",
)
DATABASE_ENV = ENV_FILES[2]

API_HOST_IP = "127.0.0.1"
API_HOST_PORT = 8097
API_CONTAINER_PORT = 8090
PROJECT_NAME = "property"
DATABASE_IMAGE = (
    "postgres:16-alpine@sha256:"
    "16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
)
DATABASE_OPERATIONS = (
    "provision-roles",
    "migrate-schema",
    "harden-runtime-acl",
    "verify-schema-readiness",
)
DATABASE_ROLES = (
    "propertyquarry_owner",
    "propertyquarry_migrator",
    "propertyquarry_api",
    "propertyquarry_worker",
    "propertyquarry_scheduler",
)
ROOT_UID = 0
ROOT_GID = 0
RUNTIME_UID = 1000
RUNTIME_GID = 1000
SUBPROCESS_TIMEOUT_SECONDS = 1800
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_ENV_BYTES = 2 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 128 * 1024 * 1024
MAX_COMPOSE_BYTES = 8 * 1024 * 1024

RUNTIME_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEPLOYMENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
ENVELOPE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$")
MODE_RE = re.compile(r"^[0-7]{4}$")
IMAGE_RE = re.compile(
    r"^[a-z0-9./_-]+(?::[a-z0-9._-]+)?@sha256:[0-9a-f]{64}$"
)

INSTALLED_BINDING_KEYS = (
    "authority_digest",
    "authority_signature_digest",
    "config_digest",
    "package_authority_key_id",
    "package_manifest_digest",
    "package_manifest_signature_digest",
    "plan_digest",
)
OBSERVATION_KEYS = frozenset({"path", "sha256", "mode", "uid", "gid", "size"})
RUNTIME_DEPLOY_KEYS = frozenset(
    {
        "deployment_id",
        "operation",
        "receipt_path",
        "compose_argv",
        "env_files",
        "docker_executable",
        "compose_plugin",
        "compose_files",
    }
)
RUNTIME_RETIREMENT_KEYS = frozenset(
    {
        "containers",
        "deployment_id",
        "desired_live_allowlist",
        "operation",
        "preserve_volumes",
        "receipt_path",
    }
)
DATABASE_PAYLOAD_KEYS = frozenset(
    {
        "authority_digest",
        "backup_max_age_seconds",
        "backup_receipt_sha256",
        "database",
        "database_container",
        "database_image",
        "database_image_id",
        "database_repo_digest",
        "database_substrate_after",
        "database_substrate_before",
        "deployment_id",
        "docker_network",
        "env_file",
        "env_file_sha256",
        "finished_at_epoch",
        "host_machine_id_digest",
        "operation",
        "predecessor_receipt_sha256",
        "production_ready",
        "purge_receipt_sha256",
        "receipt_authority_key_id",
        "result",
        "retirement_receipt_sha256",
        "runtime_inputs",
        "runtime_sha",
        "schema",
        "secret_values_emitted",
        "started_at_epoch",
        "status",
        "transaction_started_at_epoch",
        "web_image",
    }
)
DEPLOY_PAYLOAD_KEYS = frozenset(
    {
        *INSTALLED_BINDING_KEYS,
        "api_container_port",
        "api_host_ip",
        "api_host_port",
        "argv_count",
        "argv_sha256",
        "backup_receipt_sha256",
        "backup_max_age_seconds",
        "build_performed",
        "cloudflared_image",
        "database_container",
        "database_container_id",
        "database_image",
        "database_image_id",
        "database_oid",
        "database_pgdata_volume",
        "database_receipts",
        "database_repo_digest",
        "deployment_id",
        "duration_seconds",
        "environment_digests",
        "envelope_sha",
        "exit_code",
        "finished_at_epoch",
        "host_machine_id_digest",
        "idempotent",
        "mutation",
        "operation",
        "orphans_removed",
        "output_redacted",
        "post_observations",
        "pre_observations",
        "production_ready",
        "pull_policy",
        "purge_receipt_sha256",
        "receipt_authority_key_id",
        "render_image",
        "retirement_receipt_sha256",
        "runtime_deploy",
        "runtime_inputs",
        "runtime_retirement_digest",
        "runtime_sha",
        "schema",
        "secret_values_emitted",
        "started_at_epoch",
        "status",
        "stderr_bytes",
        "stderr_sha256",
        "stdout_bytes",
        "stdout_sha256",
        "subprocess_timeout_seconds",
        "transaction_started_at_epoch",
        "wait_completed",
        "web_image",
    }
)


class DeployError(RuntimeError):
    """A deliberately terse, non-secret-bearing deploy failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout_bytes: int
    stdout_sha256: str
    stderr_bytes: int
    stderr_sha256: str


@dataclass(frozen=True)
class Contract:
    authority: dict[str, object]
    plan: dict[str, object]
    manifest: dict[str, object]
    bindings: dict[str, object]
    runtime_deploy: dict[str, object]
    runtime_retirement: dict[str, object]
    runtime_retirement_digest: str
    pre_purge_runtime_inputs: list[dict[str, object]]
    runtime_inputs: list[dict[str, object]]
    receipt_private: Ed25519PrivateKey
    receipt_public: Ed25519PublicKey
    receipt_key_id: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_id(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _framed(domain: bytes, raw: bytes) -> bytes:
    return domain + len(raw).to_bytes(8, byteorder="big", signed=False) + raw


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value


def _reject_float(_value: str) -> object:
    raise ValueError("float")


def _reject_constant(_value: str) -> object:
    raise ValueError("constant")


def _strict_json(raw: bytes, *, newline: bool) -> dict[str, object]:
    encoded = raw
    if newline:
        if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
            raise DeployError("json_newline_invalid")
        encoded = raw[:-1]
    elif raw.endswith(b"\n"):
        raise DeployError("json_newline_invalid")
    try:
        value = json.loads(
            encoded,
            object_pairs_hook=_reject_duplicates,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise DeployError("json_invalid") from exc
    if not isinstance(value, dict) or _canonical_json(value) != encoded:
        raise DeployError("json_not_canonical")
    return value


def _exact_int(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DeployError("integer_invalid")
    if value < minimum or value > maximum:
        raise DeployError("integer_invalid")
    return value


def _read_regular(
    path: Path,
    *,
    maximum: int,
    mode: int | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise DeployError("required_file_unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 1
        or before.st_size > maximum
        or (mode is not None and stat.S_IMODE(before.st_mode) != mode)
        or (uid is not None and before.st_uid != uid)
        or (gid is not None and before.st_gid != gid)
    ):
        raise DeployError("required_file_metadata_invalid")
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DeployError("required_file_open_failed") from exc
    try:
        opened = os.fstat(descriptor)
        identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_mode,
            opened.st_uid,
            opened.st_gid,
            opened.st_nlink,
            opened.st_size,
            opened.st_mtime_ns,
        )
        if identity != (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_uid,
            before.st_gid,
            before.st_nlink,
            before.st_size,
            before.st_mtime_ns,
        ):
            raise DeployError("required_file_changed")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise DeployError("required_file_short_read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_uid,
            after.st_gid,
            after.st_nlink,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise DeployError("required_file_changed")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_public(path: Path, *, mode: int) -> tuple[Ed25519PublicKey, str]:
    raw = _read_regular(
        path,
        maximum=16 * 1024,
        mode=mode,
        uid=ROOT_UID,
        gid=ROOT_GID,
    )
    try:
        key = serialization.load_pem_public_key(raw)
    except (TypeError, ValueError) as exc:
        raise DeployError("public_key_invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise DeployError("public_key_algorithm_invalid")
    der = key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return key, _sha256_id(der)


def _load_receipt_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey, str]:
    private_raw = _read_regular(
        RECEIPT_PRIVATE_KEY_PATH,
        maximum=16 * 1024,
        mode=0o400,
        uid=ROOT_UID,
        gid=ROOT_GID,
    )
    public, key_id = _load_public(RECEIPT_PUBLIC_KEY_PATH, mode=0o444)
    try:
        private = serialization.load_pem_private_key(private_raw, password=None)
    except (TypeError, ValueError) as exc:
        raise DeployError("receipt_private_key_invalid") from exc
    if not isinstance(private, Ed25519PrivateKey):
        raise DeployError("receipt_private_key_algorithm_invalid")
    expected = private.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    observed = public.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if not secrets.compare_digest(expected, observed):
        raise DeployError("receipt_keypair_mismatch")
    return private, public, key_id


def _verify_raw_signature(
    public: Ed25519PublicKey,
    signature: bytes,
    domain: bytes,
    raw: bytes,
) -> None:
    if len(signature) != 64:
        raise DeployError("signature_size_invalid")
    try:
        public.verify(signature, _framed(domain, raw))
    except Exception as exc:
        raise DeployError("signature_invalid") from exc


def _expected_compose_argv() -> list[str]:
    result = [
        str(DOCKER_EXECUTABLE),
        "compose",
        "--ansi",
        "never",
        "--progress",
        "quiet",
        "--project-name",
        PROJECT_NAME,
        "--project-directory",
        str(PROPERTY_ROOT),
    ]
    for path in ENV_FILES:
        result.extend(("--env-file", str(path)))
    for path in COMPOSE_FILES:
        result.extend(("--file", str(path)))
    result.extend(
        (
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
        )
    )
    return result


def _validate_observation(value: object, *, expected_path: Path) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != OBSERVATION_KEYS:
        raise DeployError("signed_observation_shape_invalid")
    if value.get("path") != str(expected_path):
        raise DeployError("signed_observation_path_invalid")
    if not SHA256_RE.fullmatch(str(value.get("sha256") or "")):
        raise DeployError("signed_observation_digest_invalid")
    if not MODE_RE.fullmatch(str(value.get("mode") or "")):
        raise DeployError("signed_observation_mode_invalid")
    _exact_int(value.get("uid"), 0, (1 << 31) - 1)
    _exact_int(value.get("gid"), 0, (1 << 31) - 1)
    _exact_int(value.get("size"), 1, MAX_EXECUTABLE_BYTES)
    return dict(value)


def _validate_runtime_input_descriptor(
    value: object,
    *,
    expected_path: Path,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != OBSERVATION_KEYS:
        raise DeployError("runtime_input_shape_invalid")
    if value.get("path") != str(expected_path):
        raise DeployError("runtime_input_path_invalid")
    if not SHA256_RE.fullmatch(str(value.get("sha256") or "")):
        raise DeployError("runtime_input_digest_invalid")
    mode = _exact_int(value.get("mode"), 0, 0o7777)
    uid = _exact_int(value.get("uid"), 0, (1 << 31) - 1)
    gid = _exact_int(value.get("gid"), 0, (1 << 31) - 1)
    size = _exact_int(value.get("size"), 1, MAX_ENV_BYTES)
    return {
        "gid": gid,
        "mode": mode,
        "path": str(expected_path),
        "sha256": str(value["sha256"]),
        "size": size,
        "uid": uid,
    }


def _runtime_input_owners(authority: Mapping[str, object]) -> tuple[tuple[int, int], ...]:
    return (
        (RUNTIME_UID, RUNTIME_GID),
        (
            _exact_int(authority.get("scene_video_env_uid"), 0, (1 << 31) - 1),
            _exact_int(authority.get("scene_video_env_gid"), 0, (1 << 31) - 1),
        ),
        (RUNTIME_UID, RUNTIME_GID),
        (RUNTIME_UID, RUNTIME_GID),
        (
            _exact_int(authority.get("github_identity_env_uid"), 0, (1 << 31) - 1),
            _exact_int(authority.get("github_identity_env_gid"), 0, (1 << 31) - 1),
        ),
        (
            _exact_int(
                authority.get("registration_email_env_uid"), 0, (1 << 31) - 1
            ),
            _exact_int(
                authority.get("registration_email_env_gid"), 0, (1 << 31) - 1
            ),
        ),
    )


def _validate_runtime_input_array(
    value: object,
    *,
    authority: Mapping[str, object],
) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != len(ENV_FILES):
        raise DeployError("runtime_inputs_shape_invalid")
    owners = _runtime_input_owners(authority)
    result: list[dict[str, object]] = []
    for item, path, (uid, gid) in zip(value, ENV_FILES, owners, strict=True):
        observed = _validate_runtime_input_descriptor(item, expected_path=path)
        if (
            observed["mode"] != 0o600
            or observed["uid"] != uid
            or observed["gid"] != gid
        ):
            raise DeployError("runtime_input_metadata_invalid")
        result.append(observed)
    return result


def _validate_retired_container(value: object) -> dict[str, object]:
    keys = {
        "container_id",
        "compose_project",
        "compose_service",
        "created_at",
        "image",
        "image_id",
        "mounts",
        "name",
        "networks",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise DeployError("runtime_retirement_container_shape_invalid")
    for key in (
        "container_id",
        "compose_project",
        "compose_service",
        "created_at",
        "image",
        "name",
    ):
        if not isinstance(value[key], str) or len(value[key]) > 4096:
            raise DeployError("runtime_retirement_container_value_invalid")
    if (
        not CONTAINER_ID_RE.fullmatch(str(value["container_id"]))
        or not value["created_at"]
        or not value["image"]
        or not value["name"]
    ):
        raise DeployError("runtime_retirement_container_value_invalid")
    if not SHA256_RE.fullmatch(str(value["image_id"])):
        raise DeployError("runtime_retirement_container_image_id_invalid")
    networks = value["networks"]
    if (
        not isinstance(networks, list)
        or any(not isinstance(item, str) or not item for item in networks)
        or networks != sorted(set(networks))
    ):
        raise DeployError("runtime_retirement_networks_invalid")
    mounts = value["mounts"]
    mount_keys = {
        "destination",
        "driver",
        "mode",
        "name",
        "propagation",
        "rw",
        "source",
        "type",
    }
    if not isinstance(mounts, list):
        raise DeployError("runtime_retirement_mounts_invalid")
    for mount in mounts:
        if not isinstance(mount, dict) or set(mount) != mount_keys:
            raise DeployError("runtime_retirement_mount_shape_invalid")
        if not isinstance(mount["rw"], bool):
            raise DeployError("runtime_retirement_mount_rw_invalid")
        for key in mount_keys - {"rw"}:
            if not isinstance(mount[key], str):
                raise DeployError("runtime_retirement_mount_value_invalid")
    if [_canonical_json(item) for item in mounts] != sorted(
        _canonical_json(item) for item in mounts
    ):
        raise DeployError("runtime_retirement_mount_order_invalid")
    return dict(value)


def _validate_runtime_retirement(
    value: object,
    *,
    runtime_sha: str,
    deployment_id: str,
) -> tuple[dict[str, object], str]:
    if not isinstance(value, dict) or set(value) != RUNTIME_RETIREMENT_KEYS:
        raise DeployError("runtime_retirement_shape_invalid")
    expected_receipt = (
        ISOLATION_RECEIPT_ROOT
        / runtime_sha
        / deployment_id
        / "retire-stale-propertyquarry-runtime.json"
    )
    if (
        value.get("operation") != "retire-stale-propertyquarry-runtime"
        or value.get("deployment_id") != deployment_id
        or value.get("receipt_path") != str(expected_receipt)
        or value.get("preserve_volumes") is not True
    ):
        raise DeployError("runtime_retirement_binding_invalid")
    allowlist = value.get("desired_live_allowlist")
    if (
        not isinstance(allowlist, list)
        or any(not isinstance(item, str) or not item for item in allowlist)
        or allowlist != sorted(set(allowlist))
    ):
        raise DeployError("runtime_retirement_allowlist_invalid")
    containers = value.get("containers")
    if not isinstance(containers, list):
        raise DeployError("runtime_retirement_containers_invalid")
    validated = [_validate_retired_container(item) for item in containers]
    names = [str(item["name"]) for item in validated]
    if names != sorted(set(names)):
        raise DeployError("runtime_retirement_container_order_invalid")
    normalized = dict(value)
    normalized["containers"] = validated
    return normalized, _sha256_id(_canonical_json(normalized))


def _validate_runtime_deploy(
    value: object,
    *,
    runtime_sha: str,
    deployment_id: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != RUNTIME_DEPLOY_KEYS:
        raise DeployError("runtime_deploy_shape_invalid")
    expected_receipt = (
        DEPLOY_RECEIPT_ROOT / runtime_sha / deployment_id / "deploy-runtime.json"
    )
    if (
        value.get("operation") != "deploy-runtime"
        or value.get("deployment_id") != deployment_id
        or value.get("receipt_path") != str(expected_receipt)
        or value.get("compose_argv") != _expected_compose_argv()
        or value.get("env_files") != [str(path) for path in ENV_FILES]
    ):
        raise DeployError("runtime_deploy_binding_invalid")
    docker = _validate_observation(
        value.get("docker_executable"), expected_path=DOCKER_EXECUTABLE
    )
    plugin = _validate_observation(value.get("compose_plugin"), expected_path=COMPOSE_PLUGIN)
    compose = value.get("compose_files")
    if not isinstance(compose, list) or len(compose) != len(COMPOSE_FILES):
        raise DeployError("runtime_deploy_compose_files_invalid")
    compose_observations = [
        _validate_observation(item, expected_path=path)
        for item, path in zip(compose, COMPOSE_FILES, strict=True)
    ]
    if int(str(docker["mode"]), 8) & 0o111 == 0 or int(str(plugin["mode"]), 8) & 0o111 == 0:
        raise DeployError("runtime_deploy_executable_mode_invalid")
    normalized = dict(value)
    normalized["docker_executable"] = docker
    normalized["compose_plugin"] = plugin
    normalized["compose_files"] = compose_observations
    return normalized


def _load_contract(args: argparse.Namespace) -> Contract:
    package_public, package_key_id = _load_public(PACKAGE_PUBLIC_KEY_PATH, mode=0o444)
    authority_raw = _read_regular(
        AUTHORITY_PATH,
        maximum=MAX_JSON_BYTES,
        mode=0o400,
        uid=ROOT_UID,
        gid=ROOT_GID,
    )
    authority_signature = _read_regular(
        AUTHORITY_SIGNATURE_PATH,
        maximum=64,
        mode=0o444,
        uid=ROOT_UID,
        gid=ROOT_GID,
    )
    _verify_raw_signature(
        package_public,
        authority_signature,
        AUTHORITY_SIGNATURE_DOMAIN,
        authority_raw,
    )
    authority = _strict_json(authority_raw, newline=False)
    plan_raw = _read_regular(
        PLAN_PATH,
        maximum=MAX_JSON_BYTES,
        mode=0o444,
        uid=ROOT_UID,
        gid=ROOT_GID,
    )
    plan = _strict_json(plan_raw, newline=False)
    manifest_raw = _read_regular(
        MANIFEST_PATH,
        maximum=MAX_JSON_BYTES,
        mode=0o444,
        uid=ROOT_UID,
        gid=ROOT_GID,
    )
    manifest_signature = _read_regular(
        MANIFEST_SIGNATURE_PATH,
        maximum=64,
        mode=0o444,
        uid=ROOT_UID,
        gid=ROOT_GID,
    )
    _verify_raw_signature(
        package_public,
        manifest_signature,
        MANIFEST_SIGNATURE_DOMAIN,
        manifest_raw,
    )
    manifest = _strict_json(manifest_raw, newline=False)
    private, receipt_public, receipt_key_id = _load_receipt_keypair()

    if (
        authority.get("schema") != AUTHORITY_SCHEMA
        or plan.get("schema") != PLAN_SCHEMA
        or manifest.get("schema") != MANIFEST_SCHEMA
    ):
        raise DeployError("installed_schema_invalid")
    authority_digest = _sha256_id(authority_raw)
    plan_digest = _sha256_id(plan_raw)
    manifest_digest = _sha256_id(manifest_raw)
    if (
        authority.get("package_authority_key_id") != package_key_id
        or authority.get("receipt_authority_key_id") != receipt_key_id
        or authority.get("plan_digest") != plan_digest
        or manifest.get("package_authority_key_id") != package_key_id
        or manifest.get("receipt_authority_key_id") != receipt_key_id
        or manifest.get("config_digest") != authority_digest
        or manifest.get("plan_digest") != plan_digest
    ):
        raise DeployError("installed_digest_binding_invalid")

    common = {
        "deployment_id": str(args.deployment_id),
        "runtime_sha": str(args.runtime_sha),
        "envelope_sha": str(args.envelope_sha),
        "web_image": str(args.web_image),
        "render_image": str(args.render_image),
        "cloudflared_image": str(args.cloudflared_image),
        "database_image": str(args.database_image),
        "api_host_ip": str(args.api_host_ip),
        "api_host_port": int(args.api_host_port),
        "api_container_port": int(args.api_container_port),
    }
    if (
        not RUNTIME_SHA_RE.fullmatch(common["runtime_sha"])
        or not DEPLOYMENT_ID_RE.fullmatch(common["deployment_id"])
        or not ENVELOPE_SHA_RE.fullmatch(common["envelope_sha"])
        or not IMAGE_RE.fullmatch(common["web_image"])
        or not IMAGE_RE.fullmatch(common["render_image"])
        or not IMAGE_RE.fullmatch(common["cloudflared_image"])
        or common["database_image"] != DATABASE_IMAGE
        or common["api_host_ip"] != API_HOST_IP
        or common["api_host_port"] != API_HOST_PORT
        or common["api_container_port"] != API_CONTAINER_PORT
    ):
        raise DeployError("request_binding_invalid")
    for document_name, document in (("authority", authority), ("plan", plan)):
        if any(document.get(key) != expected for key, expected in common.items()):
            raise DeployError(f"installed_{document_name}_binding_invalid")
    for key in (
        "deployment_id",
        "runtime_sha",
        "envelope_sha",
        "cloudflared_image",
        "database_image",
        "api_host_ip",
        "api_host_port",
        "api_container_port",
    ):
        if manifest.get(key) != common[key]:
            raise DeployError("installed_manifest_binding_invalid")

    runtime_deploy = _validate_runtime_deploy(
        authority.get("runtime_deploy"),
        runtime_sha=common["runtime_sha"],
        deployment_id=common["deployment_id"],
    )
    if plan.get("runtime_deploy") != runtime_deploy:
        raise DeployError("runtime_deploy_cross_binding_invalid")
    runtime_retirement, retirement_digest = _validate_runtime_retirement(
        authority.get("runtime_retirement"),
        runtime_sha=common["runtime_sha"],
        deployment_id=common["deployment_id"],
    )
    if (
        plan.get("runtime_retirement") != runtime_retirement
        or authority.get("runtime_retirement_digest") != retirement_digest
        or plan.get("runtime_retirement_digest") != retirement_digest
    ):
        raise DeployError("runtime_retirement_cross_binding_invalid")
    pre_purge_runtime_inputs = _validate_runtime_input_array(
        authority.get("pre_purge_runtime_inputs"), authority=authority
    )
    runtime_inputs = _validate_runtime_input_array(
        authority.get("runtime_inputs"), authority=authority
    )
    if (
        plan.get("pre_purge_runtime_inputs") != pre_purge_runtime_inputs
        or plan.get("runtime_inputs") != runtime_inputs
        or pre_purge_runtime_inputs[0]["sha256"]
        != authority.get("pre_purge_root_env_digest")
        or plan.get("pre_purge_root_env_digest")
        != pre_purge_runtime_inputs[0]["sha256"]
        or authority.get("post_purge_root_env_digest")
        != runtime_inputs[0]["sha256"]
        or plan.get("post_purge_root_env_digest")
        != runtime_inputs[0]["sha256"]
        or pre_purge_runtime_inputs[1:] != runtime_inputs[1:]
    ):
        raise DeployError("runtime_inputs_cross_binding_invalid")
    database_substrate = _validate_database_substrate(
        authority.get("database_substrate"), database_image=str(args.database_image)
    )
    if plan.get("database_substrate") != database_substrate:
        raise DeployError("database_substrate_cross_binding_invalid")
    transaction_started_at = _exact_int(
        authority.get("transaction_started_at_epoch"), 1, (1 << 62) - 1
    )
    backup_max_age = _exact_int(
        authority.get("backup_max_age_seconds"), 3600, 3600
    )
    if (
        plan.get("transaction_started_at_epoch") != transaction_started_at
        or plan.get("backup_max_age_seconds") != backup_max_age
        or transaction_started_at > int(time.time()) + 30
    ):
        raise DeployError("transaction_time_cross_binding_invalid")

    machine_raw = _read_regular(
        MACHINE_ID_PATH,
        maximum=64,
        mode=0o444,
        uid=ROOT_UID,
        gid=ROOT_GID,
    )
    try:
        machine_id = machine_raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise DeployError("machine_id_invalid") from exc
    if not re.fullmatch(r"[0-9a-f]{32}", machine_id):
        raise DeployError("machine_id_invalid")
    machine_digest = _sha256_id(machine_id.encode("ascii"))
    if authority.get("host_machine_id_digest") != machine_digest:
        raise DeployError("host_machine_binding_invalid")

    bindings: dict[str, object] = {
        "authority_digest": authority_digest,
        "authority_signature_digest": _sha256_id(authority_signature),
        "config_digest": authority_digest,
        "package_authority_key_id": package_key_id,
        "package_manifest_digest": manifest_digest,
        "package_manifest_signature_digest": _sha256_id(manifest_signature),
        "plan_digest": plan_digest,
    }
    return Contract(
        authority=authority,
        plan=plan,
        manifest=manifest,
        bindings=bindings,
        runtime_deploy=runtime_deploy,
        runtime_retirement=runtime_retirement,
        runtime_retirement_digest=retirement_digest,
        pre_purge_runtime_inputs=pre_purge_runtime_inputs,
        runtime_inputs=runtime_inputs,
        receipt_private=private,
        receipt_public=receipt_public,
        receipt_key_id=receipt_key_id,
    )


def _verify_receipt(
    path: Path,
    *,
    public: Ed25519PublicKey,
    key_id: str,
    domain: bytes,
) -> tuple[dict[str, object], str]:
    raw = _read_regular(
        path,
        maximum=MAX_JSON_BYTES,
        mode=0o600,
        uid=ROOT_UID,
        gid=ROOT_GID,
    )
    wrapper = _strict_json(raw, newline=True)
    if set(wrapper) != {"payload", "signature", "signature_key_id"}:
        raise DeployError("receipt_wrapper_shape_invalid")
    payload = wrapper.get("payload")
    signature_text = wrapper.get("signature")
    if (
        not isinstance(payload, dict)
        or wrapper.get("signature_key_id") != key_id
        or not isinstance(signature_text, str)
        or not SIGNATURE_RE.fullmatch(signature_text)
    ):
        raise DeployError("receipt_wrapper_binding_invalid")
    try:
        signature = base64.b64decode(signature_text + "==", altchars=b"-_", validate=True)
    except (TypeError, ValueError) as exc:
        raise DeployError("receipt_signature_encoding_invalid") from exc
    _verify_raw_signature(public, signature, domain, _canonical_json(payload))
    return dict(payload), _sha256_id(raw)


def _receipt_times(
    payload: Mapping[str, object],
    *,
    earliest: int,
    maximum_duration: int,
) -> tuple[int, int]:
    started = _exact_int(payload.get("started_at_epoch"), 1, (1 << 62) - 1)
    finished = _exact_int(payload.get("finished_at_epoch"), 1, (1 << 62) - 1)
    if (
        started < earliest
        or finished < started
        or finished - started > maximum_duration
        or finished > int(time.time()) + 30
    ):
        raise DeployError("receipt_time_invalid")
    return started, finished


def _common_isolation_bindings(
    payload: Mapping[str, object],
    contract: Contract,
    args: argparse.Namespace,
    *,
    operation: str,
) -> None:
    expected = {
        **contract.bindings,
        "api_container_port": API_CONTAINER_PORT,
        "api_host_ip": API_HOST_IP,
        "api_host_port": API_HOST_PORT,
        "backup_max_age_seconds": 3600,
        "cloudflared_image": str(args.cloudflared_image),
        "database_substrate_digest": _sha256_id(
            _canonical_json(contract.authority["database_substrate"])
        ),
        "database_image": str(args.database_image),
        "deployment_id": str(args.deployment_id),
        "envelope_sha": str(args.envelope_sha),
        "host_machine_id_digest": contract.authority["host_machine_id_digest"],
        "operation": operation,
        "receipt_authority_key_id": contract.receipt_key_id,
        "render_image": str(args.render_image),
        "runtime_deploy_digest": _sha256_id(
            _canonical_json(contract.runtime_deploy)
        ),
        "pre_purge_runtime_inputs": contract.pre_purge_runtime_inputs,
        "runtime_inputs": contract.runtime_inputs,
        "runtime_retirement_digest": contract.runtime_retirement_digest,
        "runtime_sha": str(args.runtime_sha),
        "schema": ISOLATION_SCHEMA,
        "status": "verified",
        "transaction_started_at_epoch": contract.authority[
            "transaction_started_at_epoch"
        ],
        "web_image": str(args.web_image),
    }
    if (
        any(payload.get(key) != value for key, value in expected.items())
        or payload.get("production_ready") is not False
        or payload.get("secret_values_emitted") is not False
    ):
        raise DeployError("isolation_receipt_binding_invalid")


def _validate_backup_receipt(
    contract: Contract,
    args: argparse.Namespace,
) -> tuple[str, int, dict[str, object]]:
    path = (
        BACKUP_RECEIPT_ROOT
        / str(args.runtime_sha)
        / str(args.deployment_id)
        / "create.json"
    )
    payload, digest = _verify_receipt(
        path,
        public=contract.receipt_public,
        key_id=contract.receipt_key_id,
        domain=BACKUP_SIGNATURE_DOMAIN,
    )
    expected = {
        **contract.bindings,
        "backup_max_age_seconds": 3600,
        "database_image": str(args.database_image),
        "deployment_id": str(args.deployment_id),
        "envelope_sha": str(args.envelope_sha),
        "host_machine_id_digest": contract.authority["host_machine_id_digest"],
        "package_authority_key_id": contract.bindings["package_authority_key_id"],
        "receipt_authority_key_id": contract.receipt_key_id,
        "render_image": str(args.render_image),
        "runtime_sha": str(args.runtime_sha),
        "schema": BACKUP_SCHEMA,
        "transaction_started_at_epoch": contract.authority[
            "transaction_started_at_epoch"
        ],
        "web_image": str(args.web_image),
    }
    if (
        any(payload.get(key) != value for key, value in expected.items())
        or payload.get("disposition") != "verified-and-published"
        or payload.get("plaintext_retained") is not False
        or payload.get("production_ready") is not False
    ):
        raise DeployError("backup_receipt_binding_invalid")
    _started, finished = _receipt_times(
        payload,
        earliest=int(contract.authority["transaction_started_at_epoch"]),
        maximum_duration=9600,
    )
    database_substrate = _validate_database_substrate(
        payload.get("database_substrate_before"),
        database_image=str(args.database_image),
    )
    if (
        payload.get("database_substrate_after") != database_substrate
        or database_substrate != contract.authority.get("database_substrate")
        or payload.get("database_image_id") != database_substrate["image_id"]
        or payload.get("database_repo_digest")
        != database_substrate["repo_digest"]
        or payload.get("pre_purge_runtime_inputs")
        != contract.pre_purge_runtime_inputs
    ):
        raise DeployError("backup_database_identity_invalid")
    return digest, finished, database_substrate


def _validate_purge_receipt(
    contract: Contract,
    args: argparse.Namespace,
    *,
    backup_digest: str,
    earliest: int,
) -> tuple[str, int, dict[str, str]]:
    operation = "purge-legacy-runtime-exposure"
    path = (
        ISOLATION_RECEIPT_ROOT
        / str(args.runtime_sha)
        / str(args.deployment_id)
        / f"{operation}.json"
    )
    payload, digest = _verify_receipt(
        path,
        public=contract.receipt_public,
        key_id=contract.receipt_key_id,
        domain=ISOLATION_SIGNATURE_DOMAIN,
    )
    _common_isolation_bindings(payload, contract, args, operation=operation)
    _started, finished = _receipt_times(payload, earliest=earliest, maximum_duration=1800)
    result = payload.get("result")
    expected_keys = {
        "backup_receipt_sha256",
        "inputs",
        "legacy_keys_removed",
        "post_purge_root_env_digest",
        "pre_purge_root_env_digest",
        "rollback_artifact",
        "rollback_artifact_expected_removed_keys",
    }
    if not isinstance(result, dict) or set(result) != expected_keys:
        raise DeployError("purge_receipt_result_shape_invalid")
    expected_removed = _exact_int(
        result.get("rollback_artifact_expected_removed_keys"),
        LEGACY_REGISTRATION_EMAIL_KEY_COUNT,
        REGISTRATION_EMAIL_KEY_COUNT,
    )
    removed = _exact_int(
        result.get("legacy_keys_removed"),
        0,
        REGISTRATION_EMAIL_KEY_COUNT,
    )
    if (
        result.get("backup_receipt_sha256") != backup_digest
        or result.get("pre_purge_root_env_digest")
        != contract.authority.get("pre_purge_root_env_digest")
        or not SHA256_RE.fullmatch(str(result.get("post_purge_root_env_digest") or ""))
        or not isinstance(result.get("rollback_artifact"), dict)
        or expected_removed
        not in {
            LEGACY_REGISTRATION_EMAIL_KEY_COUNT,
            REGISTRATION_EMAIL_KEY_COUNT,
        }
        or removed not in {0, expected_removed}
        or result.get("post_purge_root_env_digest")
        != contract.runtime_inputs[0]["sha256"]
    ):
        raise DeployError("purge_receipt_result_binding_invalid")
    inputs = result.get("inputs")
    input_keys = {
        "file_digests",
        "google_key_count",
        "legacy_registration_email_present",
        "registration_email_key_count",
    }
    file_digests = (
        inputs.get("file_digests") if isinstance(inputs, dict) else None
    )
    expected_file_digests = {
        str(item["path"]): str(item["sha256"])
        for item in contract.runtime_inputs
    }
    if (
        not isinstance(inputs, dict)
        or set(inputs) != input_keys
        or file_digests != expected_file_digests
        or inputs.get("google_key_count") != 5
        or inputs.get("legacy_registration_email_present") is not False
        or inputs.get("registration_email_key_count")
        != REGISTRATION_EMAIL_KEY_COUNT
    ):
        raise DeployError("purge_receipt_environment_invalid")
    return digest, finished, expected_file_digests


def _validate_preserved_volume(value: object) -> dict[str, object]:
    keys = {
        "created_at",
        "driver",
        "labels",
        "mountpoint",
        "name",
        "options",
        "scope",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise DeployError("retirement_volume_shape_invalid")
    for key in ("created_at", "driver", "mountpoint", "name", "scope"):
        if not isinstance(value[key], str) or not value[key]:
            raise DeployError("retirement_volume_value_invalid")
    for key in ("labels", "options"):
        item = value[key]
        if not isinstance(item, dict) or any(
            not isinstance(name, str) or not isinstance(raw, str)
            for name, raw in item.items()
        ) or list(item) != sorted(item):
            raise DeployError("retirement_volume_map_invalid")
    return dict(value)


def _validate_database_substrate(
    value: object,
    *,
    database_image: str,
) -> dict[str, object]:
    keys = {
        "container_id",
        "container_name",
        "database",
        "database_oid",
        "image",
        "image_id",
        "pgdata_volume",
        "repo_digest",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise DeployError("database_substrate_shape_invalid")
    container_id = str(value.get("container_id") or "")
    image_id = str(value.get("image_id") or "")
    oid = _exact_int(value.get("database_oid"), 1, (1 << 62) - 1)
    volume = _validate_preserved_volume(value.get("pgdata_volume"))
    expected_repo = _canonical_repo_digest(database_image)
    if (
        not CONTAINER_ID_RE.fullmatch(container_id)
        or value.get("container_name") != "propertyquarry-db-live"
        or value.get("database") != "propertyquarry"
        or value.get("image") != database_image
        or not SHA256_RE.fullmatch(image_id)
        or value.get("repo_digest") != expected_repo
        or volume.get("driver") != "local"
        or volume.get("name") != "property_propertyquarry_pgdata"
        or volume.get("mountpoint")
        != "/var/lib/docker/volumes/property_propertyquarry_pgdata/_data"
        or volume.get("scope") != "local"
        or volume["labels"].get("com.docker.compose.project") != PROJECT_NAME
        or volume["labels"].get("com.docker.compose.volume")
        != "propertyquarry_pgdata"
    ):
        raise DeployError("database_substrate_binding_invalid")
    return {
        "container_id": container_id,
        "container_name": "propertyquarry-db-live",
        "database": "propertyquarry",
        "database_oid": oid,
        "image": database_image,
        "image_id": image_id,
        "pgdata_volume": volume,
        "repo_digest": expected_repo,
    }


def _validate_retirement_receipt(
    contract: Contract,
    args: argparse.Namespace,
    *,
    backup_digest: str,
    purge_digest: str,
    earliest: int,
) -> tuple[str, int]:
    operation = "retire-stale-propertyquarry-runtime"
    path = (
        ISOLATION_RECEIPT_ROOT
        / str(args.runtime_sha)
        / str(args.deployment_id)
        / f"{operation}.json"
    )
    payload, digest = _verify_receipt(
        path,
        public=contract.receipt_public,
        key_id=contract.receipt_key_id,
        domain=ISOLATION_SIGNATURE_DOMAIN,
    )
    _common_isolation_bindings(payload, contract, args, operation=operation)
    _started, finished = _receipt_times(payload, earliest=earliest, maximum_duration=1800)
    result = payload.get("result")
    keys = {
        "backup_receipt_sha256",
        "purge_receipt_sha256",
        "retired_containers",
        "preserved_volumes",
        "unknown_matches",
        "volumes_removed",
    }
    if not isinstance(result, dict) or set(result) != keys:
        raise DeployError("retirement_receipt_result_shape_invalid")
    retired = result.get("retired_containers")
    if (
        result.get("backup_receipt_sha256") != backup_digest
        or result.get("purge_receipt_sha256") != purge_digest
        or retired != contract.runtime_retirement["containers"]
        or result.get("unknown_matches") != []
        or result.get("volumes_removed") is not False
    ):
        raise DeployError("retirement_receipt_result_binding_invalid")
    preserved = result.get("preserved_volumes")
    if not isinstance(preserved, list):
        raise DeployError("retirement_preserved_volumes_invalid")
    validated = [_validate_preserved_volume(item) for item in preserved]
    names = [str(item["name"]) for item in validated]
    if names != sorted(set(names)):
        raise DeployError("retirement_preserved_volume_order_invalid")
    return digest, finished


def _schema_versions(schema: object, *, readiness: bool) -> dict[str, int]:
    if not isinstance(schema, dict):
        raise DeployError("database_schema_result_invalid")
    components = {
        "kernel": "ea_kernel",
        "property_search": "property_search",
        "google_identity": "propertyquarry_google_identity",
    }
    expected_top = set(components) | ({"ready", "status"} if readiness else {"status"})
    if set(schema) != expected_top:
        raise DeployError("database_schema_shape_invalid")
    expected_status = "ready" if readiness else "migrated"
    if schema.get("status") != expected_status or (readiness and schema.get("ready") is not True):
        raise DeployError("database_schema_status_invalid")
    versions: dict[str, int] = {}
    for field, component in components.items():
        item = schema.get(field)
        expected_keys = (
            {
                "applied_versions",
                "component",
                "current_version",
                "ready",
                "reason",
                "required_version",
            }
            if readiness
            else {
                "applied_versions",
                "component",
                "current_version",
                "previous_version",
            }
        )
        if not isinstance(item, dict) or set(item) != expected_keys or item.get("component") != component:
            raise DeployError("database_schema_component_invalid")
        current = _exact_int(item.get("current_version"), 1, (1 << 31) - 1)
        applied = item.get("applied_versions")
        if (
            not isinstance(applied, list)
            or any(isinstance(v, bool) or not isinstance(v, int) for v in applied)
            or applied != sorted(set(applied))
            or (applied and applied[-1] != current)
        ):
            raise DeployError("database_schema_versions_invalid")
        if readiness:
            if (
                item.get("ready") is not True
                or item.get("reason") != "ready"
                or item.get("required_version") != current
                or not applied
            ):
                raise DeployError("database_schema_readiness_invalid")
        else:
            previous = _exact_int(item.get("previous_version"), 0, current)
            if applied and applied[0] <= previous:
                raise DeployError("database_schema_migration_invalid")
            if not applied and previous != current:
                raise DeployError("database_schema_migration_invalid")
        versions[field] = current
    return versions


def _validate_database_receipts(
    contract: Contract,
    args: argparse.Namespace,
    *,
    earliest: int,
    expected_substrate: Mapping[str, object],
    backup_digest: str,
    purge_digest: str,
    retirement_digest: str,
) -> tuple[dict[str, str], int, str]:
    digests: dict[str, str] = {}
    common_env_digest: str | None = None
    versions: dict[str, int] | None = None
    previous_finished = earliest
    predecessor_digest = retirement_digest
    for operation in DATABASE_OPERATIONS:
        path = (
            DATABASE_RECEIPT_ROOT
            / str(args.runtime_sha)
            / str(args.deployment_id)
            / f"{operation}.json"
        )
        payload, receipt_digest = _verify_receipt(
            path,
            public=contract.receipt_public,
            key_id=contract.receipt_key_id,
            domain=DATABASE_SIGNATURE_DOMAIN,
        )
        expected = {
            "authority_digest": contract.bindings["authority_digest"],
            "backup_max_age_seconds": 3600,
            "backup_receipt_sha256": backup_digest,
            "database": "propertyquarry",
            "database_container": "propertyquarry-db-live",
            "database_image": str(args.database_image),
            "database_image_id": expected_substrate["image_id"],
            "database_repo_digest": expected_substrate["repo_digest"],
            "database_substrate_after": dict(expected_substrate),
            "database_substrate_before": dict(expected_substrate),
            "deployment_id": str(args.deployment_id),
            "docker_network": "property_default",
            "env_file": str(DATABASE_ENV),
            "host_machine_id_digest": contract.authority["host_machine_id_digest"],
            "operation": operation,
            "predecessor_receipt_sha256": predecessor_digest,
            "purge_receipt_sha256": purge_digest,
            "receipt_authority_key_id": contract.receipt_key_id,
            "retirement_receipt_sha256": retirement_digest,
            "runtime_inputs": contract.runtime_inputs,
            "runtime_sha": str(args.runtime_sha),
            "schema": DATABASE_SCHEMA,
            "status": "verified",
            "transaction_started_at_epoch": contract.authority[
                "transaction_started_at_epoch"
            ],
            "web_image": str(args.web_image),
        }
        if (
            set(payload) != DATABASE_PAYLOAD_KEYS
            or any(payload.get(key) != value for key, value in expected.items())
            or payload.get("production_ready") is not False
            or payload.get("secret_values_emitted") is not False
            or not SHA256_RE.fullmatch(str(payload.get("env_file_sha256") or ""))
        ):
            raise DeployError("database_receipt_binding_invalid")
        _started, finished = _receipt_times(
            payload, earliest=previous_finished, maximum_duration=7200
        )
        previous_finished = finished
        env_digest = str(payload["env_file_sha256"])
        if common_env_digest is None:
            common_env_digest = env_digest
        elif env_digest != common_env_digest:
            raise DeployError("database_environment_continuity_invalid")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise DeployError("database_result_invalid")
        if operation == "provision-roles":
            if (
                set(result) != {"credential_reused", "database_oid", "roles"}
                or not isinstance(result.get("credential_reused"), bool)
                or result.get("roles") != list(DATABASE_ROLES)
            ):
                raise DeployError("database_provision_result_invalid")
        else:
            if (
                set(result) != {"credential_reused", "database_oid", "schema"}
                or result.get("credential_reused") is not True
            ):
                raise DeployError("database_gate_result_invalid")
            current = _schema_versions(
                result.get("schema"), readiness=operation != "migrate-schema"
            )
            if versions is None:
                versions = current
            elif current != versions:
                raise DeployError("database_schema_continuity_invalid")
        oid = _exact_int(result.get("database_oid"), 1, (1 << 62) - 1)
        if oid != expected_substrate["database_oid"]:
            raise DeployError("database_oid_binding_invalid")
        digests[operation] = receipt_digest
        predecessor_digest = receipt_digest
    if common_env_digest is None:
        raise DeployError("database_receipts_incomplete")
    return digests, previous_finished, common_env_digest


def _runtime_input_observations() -> list[dict[str, object]]:
    return [_observe_runtime_input(path) for path in ENV_FILES]


def _environment_digests(
    observations: Sequence[Mapping[str, object]],
) -> dict[str, str]:
    return dict(
        sorted(
            (str(item["path"]), str(item["sha256"])) for item in observations
        )
    )


def _validate_environment_bindings(
    actual: Mapping[str, str],
    contract: Contract,
    *,
    purge_digests: Mapping[str, str],
    database_env_digest: str,
) -> None:
    if dict(actual) != dict(sorted(purge_digests.items())):
        raise DeployError("environment_purge_binding_invalid")
    expected = {
        str(ENV_FILES[1]): contract.authority.get("scene_video_env_digest"),
        str(ENV_FILES[2]): database_env_digest,
        str(ENV_FILES[4]): contract.authority.get("github_identity_env_digest"),
        str(ENV_FILES[5]): contract.authority.get("registration_email_env_digest"),
    }
    if any(actual.get(path) != digest for path, digest in expected.items()):
        raise DeployError("environment_authority_binding_invalid")


def _observe(path: Path, *, maximum: int) -> dict[str, object]:
    raw = _read_regular(path, maximum=maximum)
    metadata = path.lstat()
    return {
        "gid": metadata.st_gid,
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "path": str(path),
        "sha256": _sha256_id(raw),
        "size": metadata.st_size,
        "uid": metadata.st_uid,
    }


def _observe_runtime_input(path: Path) -> dict[str, object]:
    raw = _read_regular(path, maximum=MAX_ENV_BYTES)
    metadata = path.lstat()
    return {
        "gid": metadata.st_gid,
        "mode": stat.S_IMODE(metadata.st_mode),
        "path": str(path),
        "sha256": _sha256_id(raw),
        "size": metadata.st_size,
        "uid": metadata.st_uid,
    }


def _observations() -> dict[str, object]:
    return {
        "docker_executable": _observe(
            DOCKER_EXECUTABLE, maximum=MAX_EXECUTABLE_BYTES
        ),
        "compose_plugin": _observe(COMPOSE_PLUGIN, maximum=MAX_EXECUTABLE_BYTES),
        "compose_files": [
            _observe(path, maximum=MAX_COMPOSE_BYTES) for path in COMPOSE_FILES
        ],
    }


def _expected_observations(runtime_deploy: Mapping[str, object]) -> dict[str, object]:
    return {
        "docker_executable": runtime_deploy["docker_executable"],
        "compose_plugin": runtime_deploy["compose_plugin"],
        "compose_files": runtime_deploy["compose_files"],
    }


class _DigestReader:
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.count = 0
        self.digest = hashlib.sha256()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(64 * 1024)
                if not chunk:
                    break
                self.count += len(chunk)
                self.digest.update(chunk)
        except BaseException as exc:  # pragma: no cover - defensive thread path
            self.error = exc


class _BoundedReader:
    def __init__(self, stream: BinaryIO, *, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.buffer = bytearray()
        self.total = 0
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            while True:
                chunk = self.stream.read(64 * 1024)
                if not chunk:
                    break
                self.total += len(chunk)
                remaining = self.limit + 1 - len(self.buffer)
                if remaining > 0:
                    self.buffer.extend(chunk[:remaining])
        except BaseException as exc:  # pragma: no cover - defensive thread path
            self.error = exc


def _open_verified_docker(expected: Mapping[str, object]) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(DOCKER_EXECUTABLE, flags)
    except OSError as exc:
        raise DeployError("docker_open_failed") from exc
    try:
        metadata = os.fstat(descriptor)
        digest = hashlib.sha256()
        count = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            digest.update(chunk)
        os.lseek(descriptor, 0, os.SEEK_SET)
        observed = {
            "gid": metadata.st_gid,
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "path": str(DOCKER_EXECUTABLE),
            "sha256": "sha256:" + digest.hexdigest(),
            "size": count,
            "uid": metadata.st_uid,
        }
        if observed != dict(expected):
            raise DeployError("docker_descriptor_binding_invalid")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _run_compose(
    argv: Sequence[str],
    docker_observation: Mapping[str, object],
    release_environment: Mapping[str, str],
) -> ProcessResult:
    expected_release_keys = {
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA",
        "PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID",
        "PROPERTYQUARRY_RENDER_IMAGE",
        "PROPERTYQUARRY_WEB_IMAGE",
    }
    if (
        set(release_environment) != expected_release_keys
        or RUNTIME_SHA_RE.fullmatch(
            str(release_environment.get("PROPERTYQUARRY_RELEASE_COMMIT_SHA") or "")
        )
        is None
        or DEPLOYMENT_ID_RE.fullmatch(
            str(
                release_environment.get("PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID")
                or ""
            )
        )
        is None
        or IMAGE_RE.fullmatch(
            str(release_environment.get("PROPERTYQUARRY_RENDER_IMAGE") or "")
        )
        is None
        or IMAGE_RE.fullmatch(
            str(release_environment.get("PROPERTYQUARRY_WEB_IMAGE") or "")
        )
        is None
    ):
        raise DeployError("compose_release_environment_invalid")
    descriptor = _open_verified_docker(docker_observation)
    environment = {
        "DOCKER_CONFIG": "/nonexistent/propertyquarry-release-single-host-v2",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        **dict(release_environment),
    }
    try:
        process = subprocess.Popen(
            list(argv),
            executable=f"/proc/self/fd/{descriptor}",
            cwd="/",
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(descriptor,),
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:  # pragma: no cover
            raise DeployError("compose_capture_unavailable")
        stdout = _DigestReader(process.stdout)
        stderr = _DigestReader(process.stderr)
        stdout.thread.start()
        stderr.thread.start()
        try:
            exit_code = process.wait(timeout=SUBPROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=30)
            stdout.thread.join(timeout=30)
            stderr.thread.join(timeout=30)
            raise DeployError("compose_timeout") from exc
        stdout.thread.join(timeout=30)
        stderr.thread.join(timeout=30)
        if (
            stdout.thread.is_alive()
            or stderr.thread.is_alive()
            or stdout.error is not None
            or stderr.error is not None
        ):
            raise DeployError("compose_capture_failed")
        return ProcessResult(
            exit_code=exit_code,
            stdout_bytes=stdout.count,
            stdout_sha256="sha256:" + stdout.digest.hexdigest(),
            stderr_bytes=stderr.count,
            stderr_sha256="sha256:" + stderr.digest.hexdigest(),
        )
    except OSError as exc:
        raise DeployError("compose_execution_failed") from exc
    finally:
        os.close(descriptor)


def _run_docker_query(
    argv: Sequence[str],
    docker_observation: Mapping[str, object],
    *,
    maximum_stdout: int,
) -> bytes:
    if (
        not argv
        or argv[0] != str(DOCKER_EXECUTABLE)
        or maximum_stdout < 1
        or maximum_stdout > 2 * 1024 * 1024
    ):
        raise DeployError("docker_query_contract_invalid")
    descriptor = _open_verified_docker(docker_observation)
    environment = {
        "DOCKER_CONFIG": "/nonexistent/propertyquarry-release-single-host-v2",
        "HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "TZ": "UTC",
    }
    try:
        process = subprocess.Popen(
            list(argv),
            executable=f"/proc/self/fd/{descriptor}",
            cwd="/",
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(descriptor,),
            start_new_session=True,
        )
        if process.stdout is None or process.stderr is None:  # pragma: no cover
            raise DeployError("docker_query_capture_unavailable")
        stdout = _BoundedReader(process.stdout, limit=maximum_stdout)
        stderr = _DigestReader(process.stderr)
        stdout.thread.start()
        stderr.thread.start()
        try:
            exit_code = process.wait(timeout=30)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=30)
            stdout.thread.join(timeout=30)
            stderr.thread.join(timeout=30)
            raise DeployError("docker_query_timeout") from exc
        stdout.thread.join(timeout=30)
        stderr.thread.join(timeout=30)
        if (
            stdout.thread.is_alive()
            or stderr.thread.is_alive()
            or stdout.error is not None
            or stderr.error is not None
            or stdout.total > maximum_stdout
            or exit_code != 0
        ):
            raise DeployError("docker_query_failed")
        return bytes(stdout.buffer)
    except OSError as exc:
        raise DeployError("docker_query_execution_failed") from exc
    finally:
        os.close(descriptor)


def _docker_inspect(
    kind: str,
    target: str,
    docker_observation: Mapping[str, object],
) -> dict[str, object]:
    if kind not in {"container", "image", "volume"} or not target:
        raise DeployError("docker_inspect_contract_invalid")
    raw = _run_docker_query(
        (str(DOCKER_EXECUTABLE), kind, "inspect", target),
        docker_observation,
        maximum_stdout=1024 * 1024,
    )
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise DeployError("docker_inspect_json_invalid") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise DeployError("docker_inspect_shape_invalid")
    return dict(value[0])


def _canonical_repo_digest(reference: str) -> str:
    try:
        repository, image_digest = reference.rsplit("@", 1)
    except ValueError as exc:
        raise DeployError("database_image_reference_invalid") from exc
    prefix, separator, leaf = repository.rpartition("/")
    if ":" in leaf:
        leaf = leaf.split(":", 1)[0]
    repository = f"{prefix}{separator}{leaf}" if separator else leaf
    return f"{repository}@{image_digest}"


def _measure_database_substrate(
    *,
    database_image: str,
    docker_observation: Mapping[str, object],
) -> dict[str, object]:
    container = _docker_inspect(
        "container", "propertyquarry-db-live", docker_observation
    )
    image = _docker_inspect("image", database_image, docker_observation)
    volume = _docker_inspect(
        "volume", "property_propertyquarry_pgdata", docker_observation
    )
    config = container.get("Config")
    state = container.get("State")
    labels = config.get("Labels") if isinstance(config, dict) else None
    image_id = image.get("Id")
    repo_digests = image.get("RepoDigests")
    network_settings = container.get("NetworkSettings")
    networks = (
        network_settings.get("Networks")
        if isinstance(network_settings, dict)
        else None
    )
    ports = (
        network_settings.get("Ports")
        if isinstance(network_settings, dict)
        else None
    )
    mounts = container.get("Mounts")
    container_id = str(container.get("Id") or "")
    expected_repo_digest = _canonical_repo_digest(database_image)
    if (
        not isinstance(config, dict)
        or not isinstance(state, dict)
        or not isinstance(labels, dict)
        or not CONTAINER_ID_RE.fullmatch(container_id)
        or config.get("Image") != database_image
        or labels.get("com.docker.compose.project") != PROJECT_NAME
        or labels.get("com.docker.compose.service") != "propertyquarry-db"
        or state.get("Status") != "running"
        or not isinstance(state.get("Health"), dict)
        or state["Health"].get("Status") != "healthy"
        or not isinstance(image_id, str)
        or not SHA256_RE.fullmatch(image_id)
        or container.get("Image") != image_id
        or not isinstance(repo_digests, list)
        or expected_repo_digest not in repo_digests
        or not isinstance(networks, dict)
        or set(networks)
        != {"property_default", "property_propertyquarry_render_internal"}
        or not isinstance(ports, dict)
        or any(item is not None for item in ports.values())
        or not isinstance(mounts, list)
        or len(mounts) != 1
        or not isinstance(mounts[0], dict)
        or {
            "Destination": mounts[0].get("Destination"),
            "Name": mounts[0].get("Name"),
            "RW": mounts[0].get("RW"),
            "Type": mounts[0].get("Type"),
        }
        != {
            "Destination": "/var/lib/postgresql/data",
            "Name": "property_propertyquarry_pgdata",
            "RW": True,
            "Type": "volume",
        }
    ):
        raise DeployError("database_substrate_container_invalid")
    volume_labels = volume.get("Labels")
    volume_options = volume.get("Options")
    if volume_labels is None:
        volume_labels = {}
    if volume_options is None:
        volume_options = {}
    pgdata = {
        "created_at": str(volume.get("CreatedAt") or ""),
        "driver": str(volume.get("Driver") or ""),
        "labels": (
            dict(sorted(volume_labels.items()))
            if isinstance(volume_labels, dict)
            else volume_labels
        ),
        "mountpoint": str(volume.get("Mountpoint") or ""),
        "name": str(volume.get("Name") or ""),
        "options": (
            dict(sorted(volume_options.items()))
            if isinstance(volume_options, dict)
            else volume_options
        ),
        "scope": str(volume.get("Scope") or ""),
    }
    pgdata = _validate_preserved_volume(pgdata)
    if (
        pgdata["driver"] != "local"
        or pgdata["name"] != "property_propertyquarry_pgdata"
        or pgdata["mountpoint"]
        != "/var/lib/docker/volumes/property_propertyquarry_pgdata/_data"
        or pgdata["scope"] != "local"
        or pgdata["labels"].get("com.docker.compose.project") != PROJECT_NAME
        or pgdata["labels"].get("com.docker.compose.volume")
        != "propertyquarry_pgdata"
    ):
        raise DeployError("database_substrate_volume_invalid")
    oid_raw = _run_docker_query(
        (
            str(DOCKER_EXECUTABLE),
            "exec",
            "-i",
            "propertyquarry-db-live",
            "psql",
            "--no-psqlrc",
            "--tuples-only",
            "--no-align",
            "--set=ON_ERROR_STOP=1",
            "--username=postgres",
            "--dbname=template1",
            "--command=SELECT oid::bigint FROM pg_database WHERE datname = "
            "'propertyquarry';",
        ),
        docker_observation,
        maximum_stdout=1024,
    )
    try:
        oid_text = oid_raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise DeployError("database_substrate_oid_invalid") from exc
    if not oid_text.isdecimal():
        raise DeployError("database_substrate_oid_invalid")
    database_oid = _exact_int(int(oid_text), 1, (1 << 62) - 1)
    return {
        "container_id": container_id,
        "container_name": "propertyquarry-db-live",
        "database": "propertyquarry",
        "database_oid": database_oid,
        "image": database_image,
        "image_id": image_id,
        "pgdata_volume": pgdata,
        "repo_digest": expected_repo_digest,
    }


def _sign(payload: Mapping[str, object], private: Ed25519PrivateKey, key_id: str) -> dict[str, object]:
    payload_value = dict(payload)
    encoded = _canonical_json(payload_value)
    signature = private.sign(_framed(SIGNATURE_DOMAIN, encoded))
    return {
        "payload": payload_value,
        "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        "signature_key_id": key_id,
    }


def _validate_directory(path: Path, *, mode: int) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DeployError("receipt_directory_unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_uid != ROOT_UID
        or metadata.st_gid != ROOT_GID
    ):
        raise DeployError("receipt_directory_metadata_invalid")


def _write_receipt(path: Path, wrapper: Mapping[str, object]) -> None:
    _validate_directory(DEPLOY_RECEIPT_ROOT, mode=0o700)
    current = DEPLOY_RECEIPT_ROOT
    for component in path.relative_to(DEPLOY_RECEIPT_ROOT).parts[:-1]:
        child = current / component
        try:
            child.mkdir(mode=0o700)
            directory = os.open(
                current,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            pass
        _validate_directory(child, mode=0o700)
        current = child
    parent = current
    _validate_directory(parent, mode=0o700)
    encoded = _canonical_json(dict(wrapper)) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, ROOT_UID, ROOT_GID)
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory = os.open(
            parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_request(args: argparse.Namespace) -> Path:
    if os.geteuid() != 0:
        raise DeployError("root_required")
    if str(args.operation) != "deploy-runtime":
        raise DeployError("operation_invalid")
    runtime_sha = str(args.runtime_sha)
    if not RUNTIME_SHA_RE.fullmatch(runtime_sha):
        raise DeployError("runtime_sha_invalid")
    deployment_id = str(args.deployment_id)
    if not DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise DeployError("deployment_id_invalid")
    expected = (
        DEPLOY_RECEIPT_ROOT / runtime_sha / deployment_id / "deploy-runtime.json"
    )
    receipt_text = str(args.receipt)
    if receipt_text != str(expected) or not Path(receipt_text).is_absolute():
        raise DeployError("receipt_path_invalid")
    return expected


def execute_signed(args: argparse.Namespace) -> dict[str, object]:
    receipt_path = _validate_request(args)
    contract = _load_contract(args)
    if contract.runtime_deploy["receipt_path"] != str(receipt_path):
        raise DeployError("receipt_authority_binding_invalid")

    (
        backup_digest,
        backup_finished,
        database_substrate,
    ) = _validate_backup_receipt(contract, args)
    purge_digest, purge_finished, purge_environment = _validate_purge_receipt(
        contract,
        args,
        backup_digest=backup_digest,
        earliest=backup_finished,
    )
    retirement_digest, retirement_finished = _validate_retirement_receipt(
        contract,
        args,
        backup_digest=backup_digest,
        purge_digest=purge_digest,
        earliest=purge_finished,
    )
    (
        database_receipts,
        database_finished,
        database_env_digest,
    ) = _validate_database_receipts(
        contract,
        args,
        earliest=retirement_finished,
        expected_substrate=database_substrate,
        backup_digest=backup_digest,
        purge_digest=purge_digest,
        retirement_digest=retirement_digest,
    )
    runtime_inputs_before = _runtime_input_observations()
    if runtime_inputs_before != contract.runtime_inputs:
        raise DeployError("runtime_inputs_pre_binding_invalid")
    environment_before = _environment_digests(runtime_inputs_before)
    _validate_environment_bindings(
        environment_before,
        contract,
        purge_digests=purge_environment,
        database_env_digest=database_env_digest,
    )
    expected_observations = _expected_observations(contract.runtime_deploy)
    pre_observations = _observations()
    if pre_observations != expected_observations:
        raise DeployError("pre_observation_binding_invalid")
    expected_database_substrate = dict(database_substrate)
    pre_database_substrate = _measure_database_substrate(
        database_image=str(args.database_image),
        docker_observation=contract.runtime_deploy["docker_executable"],
    )
    if pre_database_substrate != expected_database_substrate:
        raise DeployError("database_substrate_pre_binding_invalid")

    started = int(time.time())
    if (
        started < database_finished
        or started - backup_finished
        > int(contract.authority["backup_max_age_seconds"])
    ):
        raise DeployError("predecessor_receipt_time_invalid")
    process = _run_compose(
        contract.runtime_deploy["compose_argv"],
        contract.runtime_deploy["docker_executable"],
        {
            "PROPERTYQUARRY_RELEASE_COMMIT_SHA": str(args.runtime_sha),
            "PROPERTYQUARRY_RELEASE_DEPLOYMENT_ID": str(args.deployment_id),
            "PROPERTYQUARRY_RENDER_IMAGE": str(args.render_image),
            "PROPERTYQUARRY_WEB_IMAGE": str(args.web_image),
        },
    )
    finished = int(time.time())
    duration = finished - started
    if duration < 0 or duration > SUBPROCESS_TIMEOUT_SECONDS:
        raise DeployError("compose_duration_invalid")
    post_database_substrate = _measure_database_substrate(
        database_image=str(args.database_image),
        docker_observation=contract.runtime_deploy["docker_executable"],
    )
    if (
        post_database_substrate != expected_database_substrate
        or post_database_substrate != pre_database_substrate
    ):
        raise DeployError("database_substrate_post_binding_invalid")
    post_observations = _observations()
    runtime_inputs_after = _runtime_input_observations()
    environment_after = _environment_digests(runtime_inputs_after)
    if (
        post_observations != expected_observations
        or post_observations != pre_observations
    ):
        raise DeployError("post_observation_binding_invalid")
    if environment_after != environment_before:
        raise DeployError("environment_changed_during_deploy")
    if runtime_inputs_after != contract.runtime_inputs:
        raise DeployError("runtime_inputs_post_binding_invalid")
    if process.exit_code != 0:
        raise DeployError("compose_exit_invalid")

    argv = contract.runtime_deploy["compose_argv"]
    database_container_id = str(database_substrate["container_id"])
    database_image_id = str(database_substrate["image_id"])
    database_oid = int(database_substrate["database_oid"])
    database_pgdata_volume = dict(database_substrate["pgdata_volume"])
    database_repo_digest = str(database_substrate["repo_digest"])
    payload: dict[str, object] = {
        **contract.bindings,
        "api_container_port": API_CONTAINER_PORT,
        "api_host_ip": API_HOST_IP,
        "api_host_port": API_HOST_PORT,
        "argv_count": len(argv),
        "argv_sha256": _sha256_id(_canonical_json(argv)),
        "backup_max_age_seconds": int(
            contract.authority["backup_max_age_seconds"]
        ),
        "backup_receipt_sha256": backup_digest,
        "build_performed": False,
        "cloudflared_image": str(args.cloudflared_image),
        "database_container": "propertyquarry-db-live",
        "database_image": str(args.database_image),
        "deployment_id": str(args.deployment_id),
        "database_container_id": database_container_id,
        "database_image_id": database_image_id,
        "database_oid": database_oid,
        "database_pgdata_volume": database_pgdata_volume,
        "database_receipts": database_receipts,
        "database_repo_digest": database_repo_digest,
        "duration_seconds": duration,
        "environment_digests": environment_before,
        "envelope_sha": str(args.envelope_sha),
        "exit_code": process.exit_code,
        "finished_at_epoch": finished,
        "host_machine_id_digest": contract.authority["host_machine_id_digest"],
        "idempotent": True,
        "mutation": True,
        "operation": "deploy-runtime",
        "orphans_removed": False,
        "output_redacted": True,
        "post_observations": post_observations,
        "pre_observations": pre_observations,
        "production_ready": False,
        "pull_policy": "always",
        "purge_receipt_sha256": purge_digest,
        "receipt_authority_key_id": contract.receipt_key_id,
        "render_image": str(args.render_image),
        "retirement_receipt_sha256": retirement_digest,
        "runtime_deploy": contract.runtime_deploy,
        "runtime_inputs": contract.runtime_inputs,
        "runtime_retirement_digest": contract.runtime_retirement_digest,
        "runtime_sha": str(args.runtime_sha),
        "schema": SCHEMA,
        "secret_values_emitted": False,
        "started_at_epoch": started,
        "status": "verified",
        "stderr_bytes": process.stderr_bytes,
        "stderr_sha256": process.stderr_sha256,
        "stdout_bytes": process.stdout_bytes,
        "stdout_sha256": process.stdout_sha256,
        "subprocess_timeout_seconds": SUBPROCESS_TIMEOUT_SECONDS,
        "transaction_started_at_epoch": int(
            contract.authority["transaction_started_at_epoch"]
        ),
        "wait_completed": True,
        "web_image": str(args.web_image),
    }
    if set(payload) != DEPLOY_PAYLOAD_KEYS:
        raise DeployError("deploy_receipt_payload_shape_invalid")
    wrapper = _sign(payload, contract.receipt_private, contract.receipt_key_id)
    _write_receipt(receipt_path, wrapper)
    return wrapper


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    deploy = subparsers.add_parser("deploy-runtime")
    deploy.add_argument("--runtime-sha", required=True)
    deploy.add_argument("--deployment-id", required=True)
    deploy.add_argument("--envelope-sha", required=True)
    deploy.add_argument("--web-image", required=True)
    deploy.add_argument("--render-image", required=True)
    deploy.add_argument("--cloudflared-image", required=True)
    deploy.add_argument("--database-image", required=True)
    deploy.add_argument("--api-host-ip", required=True)
    deploy.add_argument("--api-host-port", required=True, type=int)
    deploy.add_argument("--api-container-port", required=True, type=int)
    deploy.add_argument("--receipt", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        wrapper = execute_signed(args)
    except DeployError as exc:
        print(
            json.dumps({"reason": exc.code, "status": "failed"}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {"operation": wrapper["payload"]["operation"], "status": "ok"},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
