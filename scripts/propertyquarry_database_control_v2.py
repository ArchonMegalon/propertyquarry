#!/usr/bin/env python3
"""Run the ordered PropertyQuarry database release gates without secret argv."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from typing import Mapping, Sequence

from cryptography.exceptions import InvalidSignature

try:  # Repo execution.
    from scripts import propertyquarry_predeploy_backup_v2 as backup_contract
    from scripts import provision_propertyquarry_runtime_database as runtime_database
except ImportError:  # Installed, adjacent executable execution.
    from importlib.machinery import SourceFileLoader
    import importlib.util

    _installed_backup_path = next(
        (
            candidate
            for candidate in (
                Path(__file__).with_name("propertyquarry-predeploy-backup-v2"),
                Path(__file__).with_name("propertyquarry_predeploy_backup_v2.py"),
            )
            if candidate.is_file()
        ),
        Path(__file__).with_name("propertyquarry-predeploy-backup-v2"),
    )
    _installed_backup_loader = SourceFileLoader(
        "propertyquarry_predeploy_backup_v2",
        str(_installed_backup_path),
    )
    _installed_backup_spec = importlib.util.spec_from_loader(
        _installed_backup_loader.name,
        _installed_backup_loader,
    )
    if _installed_backup_spec is None:  # pragma: no cover - interpreter guard
        raise ImportError("installed_backup_module_unavailable")
    backup_contract = importlib.util.module_from_spec(_installed_backup_spec)
    sys.modules[_installed_backup_loader.name] = backup_contract
    _installed_backup_loader.exec_module(backup_contract)
    import provision_propertyquarry_runtime_database as runtime_database  # type: ignore[no-redef]


RECEIPT_SCHEMA = "propertyquarry.database-control-receipt.v2"
RECEIPT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-database-receipt-signature.v2\x00"
)
BACKUP_RECEIPT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-predeploy-backup-receipt-signature.v2\x00"
)
ISOLATION_RECEIPT_SIGNATURE_DOMAIN = (
    b"propertyquarry.release-control.single-host-runtime-isolation-receipt-signature.v2\x00"
)
BACKUP_RECEIPT_SCHEMA = "propertyquarry.predeploy-backup-receipt.v2"
ISOLATION_RECEIPT_SCHEMA = "propertyquarry.runtime-isolation-receipt.v2"
OPERATIONS = (
    "provision-roles",
    "migrate-schema",
    "harden-runtime-acl",
    "verify-schema-readiness",
)
DATABASE_CONTAINER = "propertyquarry-db-live"
DATABASE_VOLUME = "property_propertyquarry_pgdata"
DATABASE_VOLUME_MOUNTPOINT = (
    "/var/lib/docker/volumes/property_propertyquarry_pgdata/_data"
)
DOCKER_NETWORK = "property_default"
RUNTIME_ENV_FILE = Path(
    "/docker/property/state/runtime/propertyquarry_database_roles.env"
)
ADMISSION_ENV_FILE = Path(
    "/docker/property/state/runtime/propertyquarry_admission.env"
)
AUTHORITY_PATH = Path(
    "/etc/propertyquarry-release-single-host-v2/authority.v2.json"
)
TRANSACTION_PLAN_PATH = Path(
    "/etc/propertyquarry-release-single-host-v2/transaction-plan.v2.json"
)
RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/database-receipts"
)
BACKUP_RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/backup-receipts"
)
ISOLATION_RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/isolation-receipts"
)
MACHINE_ID_PATH = Path("/etc/machine-id")
RUNTIME_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DEPLOYMENT_ID_RE = re.compile(r"^[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SHA256_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
VOLUME_CREATED_AT_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+ -]+Z?$"
)
IMAGE_RE = re.compile(
    r"^[a-z0-9./_-]+(?::[a-z0-9._-]+)?@sha256:[0-9a-f]{64}$"
)
EXPECTED_DATABASE_IMAGE = (
    "postgres:16-alpine@sha256:"
    "16bc17c64a573ef34162af9298258d1aec548232985b33ed7b1eac33ba35c229"
)
RUNTIME_INPUT_PATHS = (
    Path("/docker/property/.env"),
    Path("/docker/property/state/runtime/property_scene_video_shared.env"),
    RUNTIME_ENV_FILE,
    ADMISSION_ENV_FILE,
    Path("/docker/property/state/runtime/propertyquarry_google_identity.env"),
    Path("/docker/property/state/runtime/propertyquarry_registration_email.env"),
)
DATABASE_RECEIPT_PAYLOAD_KEYS = {
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
PREVIOUS_DATABASE_OPERATION = {
    "migrate-schema": "provision-roles",
    "harden-runtime-acl": "migrate-schema",
    "verify-schema-readiness": "harden-runtime-acl",
}
PURGE_RESULT_KEYS = {
    "backup_receipt_sha256",
    "inputs",
    "legacy_keys_removed",
    "post_purge_root_env_digest",
    "pre_purge_root_env_digest",
    "rollback_artifact",
    "rollback_artifact_expected_removed_keys",
}
PURGE_INPUT_KEYS = {
    "file_digests",
    "google_key_count",
    "legacy_registration_email_present",
    "registration_email_key_count",
}


class DatabaseControlError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = str(code or "database_control_failed")
        self.detail = str(detail or "")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _sha256_id(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _exact_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _validate_runtime_inputs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) != len(RUNTIME_INPUT_PATHS):
        raise DatabaseControlError("runtime_inputs_invalid")
    validated: list[dict[str, object]] = []
    expected_keys = {"gid", "mode", "path", "sha256", "size", "uid"}
    for expected_path, item in zip(RUNTIME_INPUT_PATHS, value, strict=True):
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise DatabaseControlError("runtime_inputs_invalid")
        digest = item.get("sha256")
        size = _exact_positive_int(item.get("size"))
        if (
            item.get("path") != str(expected_path)
            or not isinstance(digest, str)
            or SHA256_ID_RE.fullmatch(digest) is None
            or item.get("mode") != 0o600
            or isinstance(item.get("mode"), bool)
            or item.get("uid") != 1000
            or isinstance(item.get("uid"), bool)
            or item.get("gid") != 1000
            or isinstance(item.get("gid"), bool)
            or size is None
        ):
            raise DatabaseControlError("runtime_inputs_invalid")
        validated.append(dict(item))
    return validated


def _measure_runtime_inputs() -> list[dict[str, object]]:
    measured: list[dict[str, object]] = []
    for path in RUNTIME_INPUT_PATHS:
        raw = _read_regular(
            path,
            max_bytes=8 * 1024 * 1024,
            mode=0o600,
            uid=1000,
            gid=1000,
        )
        metadata = path.lstat()
        measured.append(
            {
                "gid": metadata.st_gid,
                "mode": stat.S_IMODE(metadata.st_mode),
                "path": str(path),
                "sha256": _sha256_id(raw),
                "size": metadata.st_size,
                "uid": metadata.st_uid,
            }
        )
    return _validate_runtime_inputs(measured)


def _validate_string_map(value: object, *, allow_none: bool) -> dict[str, str] | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(item, str)
        or "\x00" in key
        or "\x00" in item
        for key, item in value.items()
    ):
        raise DatabaseControlError("database_pgdata_volume_invalid")
    return {key: value[key] for key in sorted(value)}


def _validate_database_substrate(value: object) -> dict[str, object]:
    expected_keys = {
        "container_id",
        "container_name",
        "database",
        "database_oid",
        "image",
        "image_id",
        "pgdata_volume",
        "repo_digest",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise DatabaseControlError("database_substrate_invalid")
    container_id = value.get("container_id")
    database_oid = _exact_positive_int(value.get("database_oid"))
    image_id = value.get("image_id")
    repo_digest = value.get("repo_digest")
    if (
        not isinstance(container_id, str)
        or CONTAINER_ID_RE.fullmatch(container_id) is None
        or value.get("container_name") != DATABASE_CONTAINER
        or value.get("database") != runtime_database.TARGET_DATABASE
        or database_oid is None
        or value.get("image") != EXPECTED_DATABASE_IMAGE
        or not isinstance(image_id, str)
        or SHA256_ID_RE.fullmatch(image_id) is None
        or repo_digest != _canonical_repo_digest(EXPECTED_DATABASE_IMAGE)
    ):
        raise DatabaseControlError("database_substrate_invalid")
    volume = value.get("pgdata_volume")
    volume_keys = {
        "created_at",
        "driver",
        "labels",
        "mountpoint",
        "name",
        "options",
        "scope",
    }
    if not isinstance(volume, dict) or set(volume) != volume_keys:
        raise DatabaseControlError("database_pgdata_volume_invalid")
    created_at = volume.get("created_at")
    labels = _validate_string_map(volume.get("labels"), allow_none=False)
    options = _validate_string_map(volume.get("options"), allow_none=False)
    if (
        not isinstance(created_at, str)
        or VOLUME_CREATED_AT_RE.fullmatch(created_at) is None
        or volume.get("driver") != "local"
        or volume.get("mountpoint") != DATABASE_VOLUME_MOUNTPOINT
        or volume.get("name") != DATABASE_VOLUME
        or volume.get("scope") != "local"
        or not isinstance(labels, dict)
        or labels.get("com.docker.compose.project") != "property"
        or labels.get("com.docker.compose.volume") != "propertyquarry_pgdata"
    ):
        raise DatabaseControlError("database_pgdata_volume_invalid")
    normalized_volume: dict[str, object] = {
        "created_at": created_at,
        "driver": "local",
        "labels": labels,
        "mountpoint": DATABASE_VOLUME_MOUNTPOINT,
        "name": DATABASE_VOLUME,
        "options": options,
        "scope": "local",
    }
    return {
        "container_id": container_id,
        "container_name": DATABASE_CONTAINER,
        "database": runtime_database.TARGET_DATABASE,
        "database_oid": database_oid,
        "image": EXPECTED_DATABASE_IMAGE,
        "image_id": image_id,
        "pgdata_volume": normalized_volume,
        "repo_digest": repo_digest,
    }


def _read_regular(
    path: Path,
    *,
    max_bytes: int,
    mode: int | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> bytes:
    try:
        observed = path.lstat()
    except FileNotFoundError as exc:
        raise DatabaseControlError("required_file_missing", str(path)) from exc
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_nlink != 1
        or (mode is not None and stat.S_IMODE(observed.st_mode) != mode)
        or (uid is not None and observed.st_uid != uid)
        or (gid is not None and observed.st_gid != gid)
        or observed.st_size < 1
        or observed.st_size > max_bytes
    ):
        raise DatabaseControlError("required_file_metadata_invalid", str(path))
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise DatabaseControlError("required_file_open_failed", str(path)) from exc
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != observed.st_dev
            or opened.st_ino != observed.st_ino
            or opened.st_mode != observed.st_mode
            or opened.st_nlink != observed.st_nlink
            or opened.st_uid != observed.st_uid
            or opened.st_gid != observed.st_gid
            or opened.st_size != observed.st_size
        ):
            raise DatabaseControlError("required_file_changed", str(path))
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise DatabaseControlError("required_file_size_invalid", str(path))
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_mode != opened.st_mode
            or after.st_nlink != opened.st_nlink
            or after.st_uid != opened.st_uid
            or after.st_gid != opened.st_gid
            or after.st_size != opened.st_size
            or len(payload) != opened.st_size
        ):
            raise DatabaseControlError("required_file_changed", str(path))
        return bytes(payload)
    finally:
        os.close(descriptor)


def _load_authority(
    *,
    runtime_sha: str,
    deployment_id: str,
    web_image: str,
    database_image: str,
) -> tuple[dict[str, object], str]:
    raw = _read_regular(
        AUTHORITY_PATH,
        max_bytes=8 * 1024 * 1024,
        uid=0,
        gid=0,
    )
    try:
        authority = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise DatabaseControlError("authority_json_invalid") from exc
    if not isinstance(authority, dict) or raw != _canonical_json(authority):
        raise DatabaseControlError("authority_not_canonical")
    plan_raw = _read_regular(
        TRANSACTION_PLAN_PATH,
        max_bytes=8 * 1024 * 1024,
        uid=0,
        gid=0,
    )
    try:
        plan = json.loads(plan_raw)
    except (TypeError, ValueError) as exc:
        raise DatabaseControlError("transaction_plan_json_invalid") from exc
    if not isinstance(plan, dict) or plan_raw != _canonical_json(plan):
        raise DatabaseControlError("transaction_plan_not_canonical")
    if (
        authority.get("schema")
        != "propertyquarry.release-control.single-host-profile.v2"
        or authority.get("runtime_sha") != runtime_sha
        or authority.get("deployment_id") != deployment_id
        or authority.get("web_image") != web_image
        or authority.get("database_image") != database_image
    ):
        raise DatabaseControlError("authority_binding_invalid")
    if (
        _exact_positive_int(authority.get("transaction_started_at_epoch")) is None
        or authority.get("backup_max_age_seconds") != 3600
        or isinstance(authority.get("backup_max_age_seconds"), bool)
    ):
        raise DatabaseControlError("authority_transaction_window_invalid")
    pre_purge_inputs = _validate_runtime_inputs(
        authority.get("pre_purge_runtime_inputs")
    )
    runtime_inputs = _validate_runtime_inputs(authority.get("runtime_inputs"))
    if pre_purge_inputs[1:] != runtime_inputs[1:]:
        raise DatabaseControlError("authority_runtime_inputs_invalid")
    for field in ("path", "mode", "uid", "gid"):
        if pre_purge_inputs[0][field] != runtime_inputs[0][field]:
            raise DatabaseControlError("authority_runtime_inputs_invalid")
    if (
        authority.get("pre_purge_root_env_digest")
        != pre_purge_inputs[0]["sha256"]
        or authority.get("post_purge_root_env_digest")
        != runtime_inputs[0]["sha256"]
    ):
        raise DatabaseControlError("authority_runtime_inputs_invalid")
    _validate_database_substrate(authority.get("database_substrate"))
    common_fields = (
        "backup_max_age_seconds",
        "database_image",
        "database_substrate",
        "deployment_id",
        "post_purge_root_env_digest",
        "pre_purge_root_env_digest",
        "pre_purge_runtime_inputs",
        "runtime_inputs",
        "runtime_sha",
        "transaction_started_at_epoch",
        "web_image",
    )
    if (
        plan.get("schema")
        != "propertyquarry.release-control.single-host-transaction-plan.v2"
        or authority.get("plan_digest") != _sha256_id(plan_raw)
        or any(plan.get(field) != authority.get(field) for field in common_fields)
    ):
        raise DatabaseControlError("authority_plan_binding_invalid")
    return authority, _sha256_id(raw)


def _validate_env_file(path: Path) -> bytes:
    return _read_regular(
        path,
        max_bytes=32 * 1024,
        mode=0o600,
        uid=1000,
        gid=1000,
    )


def _docker_inspect(kind: str, target: str) -> dict[str, object]:
    if kind not in {"container", "image", "volume"}:
        raise DatabaseControlError("docker_inspect_kind_invalid")
    try:
        completed = subprocess.run(
            ("/usr/bin/docker", kind, "inspect", target),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={
                "HOME": "/nonexistent",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "TZ": "UTC",
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DatabaseControlError("database_image_inspection_unavailable") from exc
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > 1024 * 1024:
        raise DatabaseControlError("database_image_inspection_failed")
    try:
        loaded = json.loads(completed.stdout)
    except (TypeError, ValueError) as exc:
        raise DatabaseControlError("database_image_inspection_invalid") from exc
    if not isinstance(loaded, list) or len(loaded) != 1 or not isinstance(loaded[0], dict):
        raise DatabaseControlError("database_image_inspection_invalid")
    return dict(loaded[0])


def _canonical_repo_digest(reference: str) -> str:
    repository, digest = reference.rsplit("@", 1)
    prefix, separator, leaf = repository.rpartition("/")
    if ":" in leaf:
        leaf = leaf.split(":", 1)[0]
    canonical_repository = f"{prefix}{separator}{leaf}" if separator else leaf
    return f"{canonical_repository}@{digest}"


def _verify_database_container_image(database_image: str) -> dict[str, object]:
    container = _docker_inspect("container", DATABASE_CONTAINER)
    image = _docker_inspect("image", database_image)
    volume = _docker_inspect("volume", DATABASE_VOLUME)
    config = container.get("Config")
    state = container.get("State")
    labels = config.get("Labels") if isinstance(config, dict) else None
    image_id = image.get("Id")
    repo_digests = image.get("RepoDigests")
    host = container.get("HostConfig")
    network_settings = container.get("NetworkSettings")
    networks = network_settings.get("Networks") if isinstance(network_settings, dict) else None
    ports = network_settings.get("Ports") if isinstance(network_settings, dict) else None
    mounts = container.get("Mounts")
    container_id = container.get("Id")
    expected_repo_digest = _canonical_repo_digest(database_image)
    volume_labels = _validate_string_map(volume.get("Labels"), allow_none=False)
    raw_volume_options = volume.get("Options")
    volume_options = _validate_string_map(
        {} if raw_volume_options is None else raw_volume_options,
        allow_none=False,
    )
    created_at = volume.get("CreatedAt")
    pgdata_volume = {
        "created_at": created_at,
        "driver": volume.get("Driver"),
        "labels": volume_labels,
        "mountpoint": volume.get("Mountpoint"),
        "name": volume.get("Name"),
        "options": volume_options,
        "scope": volume.get("Scope"),
    }
    if (
        not isinstance(config, dict)
        or not isinstance(state, dict)
        or not isinstance(labels, dict)
        or config.get("Image") != database_image
        or labels.get("com.docker.compose.project") != "property"
        or labels.get("com.docker.compose.service") != "propertyquarry-db"
        or not isinstance(container_id, str)
        or CONTAINER_ID_RE.fullmatch(container_id) is None
        or container.get("Name") != f"/{DATABASE_CONTAINER}"
        or state.get("Status") != "running"
        or not isinstance(state.get("Health"), dict)
        or state["Health"].get("Status") != "healthy"
        or not isinstance(image_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None
        or container.get("Image") != image_id
        or not isinstance(repo_digests, list)
        or expected_repo_digest not in repo_digests
        or config.get("Cmd") != ["postgres"]
        or config.get("Entrypoint") != ["docker-entrypoint.sh"]
        or config.get("User") != ""
        or config.get("Healthcheck")
        != {
            "Interval": 30_000_000_000,
            "Retries": 3,
            "Test": ["CMD-SHELL", "pg_isready -U postgres -d propertyquarry"],
            "Timeout": 5_000_000_000,
        }
        or not isinstance(host, dict)
        or host.get("Privileged") is not False
        or host.get("ReadonlyRootfs") is not False
        or host.get("CapAdd") is not None
        or host.get("CapDrop") is not None
        or host.get("SecurityOpt") is not None
        or host.get("PidsLimit") != 128
        or host.get("PortBindings") != {}
        or host.get("PublishAllPorts") is not False
        or host.get("Init") is not True
        or host.get("CgroupParent") != "system.slice"
        or host.get("RestartPolicy")
        != {"MaximumRetryCount": 3, "Name": "on-failure"}
        or not isinstance(networks, dict)
        or set(networks) != {
            "property_default",
            "property_propertyquarry_render_internal",
        }
        or not isinstance(ports, dict)
        or any(bindings is not None for bindings in ports.values())
        or not isinstance(mounts, list)
        or len(mounts) != 1
        or not isinstance(mounts[0], dict)
        or {
            "Destination": mounts[0].get("Destination"),
            "Driver": mounts[0].get("Driver"),
            "Mode": mounts[0].get("Mode"),
            "Name": mounts[0].get("Name"),
            "Propagation": mounts[0].get("Propagation"),
            "RW": mounts[0].get("RW"),
            "Source": mounts[0].get("Source"),
            "Type": mounts[0].get("Type"),
        }
        != {
            "Destination": "/var/lib/postgresql/data",
            "Driver": "local",
            "Mode": "rw",
            "Name": DATABASE_VOLUME,
            "Propagation": "",
            "RW": True,
            "Source": DATABASE_VOLUME_MOUNTPOINT,
            "Type": "volume",
        }
    ):
        raise DatabaseControlError("database_container_image_identity_invalid")
    try:
        legacy_oid, target_oid = runtime_database._database_oids(  # noqa: SLF001
            container=DATABASE_CONTAINER,
            hidden=(),
        )
    except runtime_database.RuntimeDatabaseError as exc:
        raise DatabaseControlError("propertyquarry_database_identity_invalid") from exc
    database_oid = target_oid if target_oid is not None else legacy_oid
    substrate = {
        "container_id": container_id,
        "container_name": DATABASE_CONTAINER,
        "database": runtime_database.TARGET_DATABASE,
        "database_oid": database_oid,
        "image": database_image,
        "image_id": image_id,
        "pgdata_volume": pgdata_volume,
        "repo_digest": expected_repo_digest,
    }
    try:
        return _validate_database_substrate(substrate)
    except DatabaseControlError as exc:
        raise DatabaseControlError("database_container_image_identity_invalid") from exc


def _load_values(*, allow_create: bool) -> tuple[dict[str, str], bool, bool]:
    _validate_env_file(ADMISSION_ENV_FILE)
    if not allow_create:
        _validate_env_file(RUNTIME_ENV_FILE)
    try:
        values, reused, needs_repair = runtime_database._load_or_create_values(  # noqa: SLF001
            env_file=RUNTIME_ENV_FILE,
            admission_env_file=ADMISSION_ENV_FILE,
        )
    except runtime_database.RuntimeDatabaseError as exc:
        raise DatabaseControlError("runtime_database_credentials_invalid") from exc
    if not allow_create and not reused:
        raise DatabaseControlError("runtime_database_credentials_missing")
    return values, reused, needs_repair


def _secret_context(values: Mapping[str, str]) -> tuple[dict[str, str], tuple[str, ...]]:
    try:
        passwords = runtime_database._passwords(values)  # noqa: SLF001
        admission_password = runtime_database._parse_admission_url(  # noqa: SLF001
            values[runtime_database.ADMISSION_KEY]
        )
    except runtime_database.RuntimeDatabaseError as exc:
        raise DatabaseControlError("runtime_database_credentials_invalid") from exc
    hidden = (
        *passwords.values(),
        admission_password,
        values[runtime_database.ERASURE_KEY],
        values[runtime_database.ADMISSION_KEY],
        *values.values(),
    )
    return passwords, tuple(dict.fromkeys(hidden))


def _database_target_guard(*, hidden: Sequence[str]) -> int:
    try:
        legacy_oid, target_oid = runtime_database._database_oids(  # noqa: SLF001
            container=DATABASE_CONTAINER,
            hidden=hidden,
        )
    except runtime_database.RuntimeDatabaseError as exc:
        raise DatabaseControlError("propertyquarry_database_identity_invalid") from exc
    if target_oid is None:
        raise DatabaseControlError("propertyquarry_target_database_missing")
    try:
        runtime_database._check_sentinel(  # noqa: SLF001
            container=DATABASE_CONTAINER,
            database=runtime_database.TARGET_DATABASE,
            database_oid=target_oid,
            hidden=hidden,
            install=False,
        )
    except runtime_database.RuntimeDatabaseError as exc:
        raise DatabaseControlError("propertyquarry_activation_sentinel_invalid") from exc
    _ = legacy_oid
    return target_oid


def _provision_roles() -> dict[str, object]:
    values, reused, needs_repair = _load_values(allow_create=True)
    passwords, hidden = _secret_context(values)
    try:
        runtime_database._psql(  # noqa: SLF001
            container=DATABASE_CONTAINER,
            database="template1",
            sql=runtime_database._prepare_roles_sql(passwords),  # noqa: SLF001
            hidden=hidden,
        )
        legacy_oid, target_oid = runtime_database._database_oids(  # noqa: SLF001
            container=DATABASE_CONTAINER,
            hidden=hidden,
        )
        if target_oid is not None:
            runtime_database._check_sentinel(  # noqa: SLF001
                container=DATABASE_CONTAINER,
                database=runtime_database.TARGET_DATABASE,
                database_oid=target_oid,
                hidden=hidden,
                install=True,
            )
        elif legacy_oid is not None:
            runtime_database._check_sentinel(  # noqa: SLF001
                container=DATABASE_CONTAINER,
                database=runtime_database.LEGACY_DATABASE,
                database_oid=legacy_oid,
                hidden=hidden,
                install=True,
            )
            runtime_database._psql(  # noqa: SLF001
                container=DATABASE_CONTAINER,
                database="template1",
                sql=runtime_database._rename_sql(  # noqa: SLF001
                    source=runtime_database.LEGACY_DATABASE,
                    target=runtime_database.TARGET_DATABASE,
                    expected_oid=legacy_oid,
                ),
                hidden=hidden,
            )
            target_oid = legacy_oid
            runtime_database._check_sentinel(  # noqa: SLF001
                container=DATABASE_CONTAINER,
                database=runtime_database.TARGET_DATABASE,
                database_oid=target_oid,
                hidden=hidden,
                install=False,
            )
        else:  # pragma: no cover - underlying guard rejects this state
            raise DatabaseControlError("propertyquarry_database_missing")
        if not reused or needs_repair:
            runtime_database._write_env(  # noqa: SLF001
                RUNTIME_ENV_FILE,
                values,
                replace=needs_repair,
            )
            os.chown(RUNTIME_ENV_FILE, 1000, 1000)
        _validate_env_file(RUNTIME_ENV_FILE)
    except runtime_database.RuntimeDatabaseError as exc:
        detail = runtime_database._redact(str(exc), hidden)  # noqa: SLF001
        raise DatabaseControlError("propertyquarry_role_provision_failed", detail) from exc
    return {
        "credential_reused": reused,
        "database_oid": int(target_oid),
        "roles": [
            runtime_database.OWNER_ROLE,
            runtime_database.MIGRATOR_ROLE,
            *runtime_database.RUNTIME_ROLES,
        ],
    }


def _run_schema_container(
    *,
    operation: str,
    database_url: str,
    runtime_sha: str,
    web_image: str,
    hidden: Sequence[str],
) -> dict[str, object]:
    if operation not in {"migrate", "check"}:
        raise DatabaseControlError("schema_operation_invalid")
    argv = (
        "/usr/bin/docker",
        "run",
        "--rm",
        "--pull=never",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--network",
        DOCKER_NETWORK,
        "--pids-limit=128",
        "--memory=805306368",
        "--cpus=1.0",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,size=67108864,mode=1777",
        "--env",
        "DATABASE_URL",
        "--env",
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA",
        "--env",
        "EA_ROLE=propertyquarry-database-control",
        "--env",
        "EA_RUNTIME_MODE=prod",
        "--entrypoint",
        "/usr/local/bin/python",
        web_image,
        "-m",
        "app.product.propertyquarry_schema",
        operation,
        "--applied-by",
        runtime_sha,
    )
    environment = {
        "DATABASE_URL": database_url,
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "PROPERTYQUARRY_RELEASE_COMMIT_SHA": runtime_sha,
        "TZ": "UTC",
    }
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=1200 if operation == "migrate" else 300,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DatabaseControlError("schema_container_unavailable") from exc
    if completed.returncode != 0:
        detail = runtime_database._redact(  # noqa: SLF001
            str(completed.stderr or completed.stdout or "")[-4000:],
            hidden,
        )
        raise DatabaseControlError(
            "schema_container_failed",
            f"{completed.returncode}:{detail}",
        )
    output_lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not output_lines:
        raise DatabaseControlError("schema_container_receipt_missing")
    try:
        result = json.loads(output_lines[-1])
    except (TypeError, ValueError) as exc:
        raise DatabaseControlError("schema_container_receipt_invalid") from exc
    if not isinstance(result, dict):
        raise DatabaseControlError("schema_container_receipt_invalid")
    expected_status = "migrated" if operation == "migrate" else "ready"
    if result.get("status") != expected_status or (
        operation == "check" and result.get("ready") is not True
    ):
        raise DatabaseControlError("schema_container_not_ready")
    return result


def _operate(
    *,
    operation: str,
    runtime_sha: str,
    web_image: str,
) -> dict[str, object]:
    if operation == "provision-roles":
        return _provision_roles()
    values, reused, _needs_repair = _load_values(allow_create=False)
    _passwords, hidden = _secret_context(values)
    try:
        runtime_database._psql(  # noqa: SLF001
            container=DATABASE_CONTAINER,
            database="template1",
            sql=runtime_database._role_authority_guard_sql(),  # noqa: SLF001
            hidden=hidden,
        )
    except runtime_database.RuntimeDatabaseError as exc:
        raise DatabaseControlError("propertyquarry_role_guard_failed") from exc
    database_oid = _database_target_guard(hidden=hidden)
    if operation == "migrate-schema":
        schema_result = _run_schema_container(
            operation="migrate",
            database_url=values["PROPERTYQUARRY_MIGRATION_DATABASE_URL"],
            runtime_sha=runtime_sha,
            web_image=web_image,
            hidden=hidden,
        )
    elif operation == "harden-runtime-acl":
        schema_result = _run_schema_container(
            operation="check",
            database_url=values["PROPERTYQUARRY_MIGRATION_DATABASE_URL"],
            runtime_sha=runtime_sha,
            web_image=web_image,
            hidden=hidden,
        )
        try:
            runtime_database._psql(  # noqa: SLF001
                container=DATABASE_CONTAINER,
                database=runtime_database.TARGET_DATABASE,
                sql=runtime_database._configure_sql(),  # noqa: SLF001
                hidden=hidden,
            )
        except runtime_database.RuntimeDatabaseError as exc:
            raise DatabaseControlError("propertyquarry_acl_hardening_failed") from exc
    elif operation == "verify-schema-readiness":
        schema_result = _run_schema_container(
            operation="check",
            database_url=values["PROPERTYQUARRY_API_DATABASE_URL"],
            runtime_sha=runtime_sha,
            web_image=web_image,
            hidden=hidden,
        )
    else:  # pragma: no cover - argparse enforces this
        raise DatabaseControlError("database_operation_invalid")
    return {
        "credential_reused": reused,
        "database_oid": database_oid,
        "schema": schema_result,
    }


def _sign_payload(
    payload: Mapping[str, object],
    *,
    private_key,  # type: ignore[no-untyped-def]
    key_id: str,
) -> dict[str, object]:
    payload_object = dict(payload)
    encoded = _canonical_json(payload_object)
    signature = private_key.sign(
        RECEIPT_SIGNATURE_DOMAIN
        + len(encoded).to_bytes(8, byteorder="big", signed=False)
        + encoded
    )
    return {
        "payload": payload_object,
        "signature": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        "signature_key_id": key_id,
    }


def _read_signed_receipt(
    path: Path,
    *,
    signature_domain: bytes,
    public_key,  # type: ignore[no-untyped-def]
    key_id: str,
) -> tuple[dict[str, object], str]:
    raw = _read_regular(
        path,
        max_bytes=2 * 1024 * 1024,
        mode=0o600,
        uid=0,
        gid=0,
    )
    if len(raw) < 2 or not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
        raise DatabaseControlError("predecessor_receipt_encoding_invalid")
    encoded_wrapper = raw[:-1]
    try:
        wrapper = json.loads(encoded_wrapper)
    except (TypeError, ValueError) as exc:
        raise DatabaseControlError("predecessor_receipt_json_invalid") from exc
    try:
        canonical_wrapper = _canonical_json(wrapper)
    except (TypeError, ValueError) as exc:
        raise DatabaseControlError("predecessor_receipt_json_invalid") from exc
    if (
        not isinstance(wrapper, dict)
        or set(wrapper) != {"payload", "signature", "signature_key_id"}
        or encoded_wrapper != canonical_wrapper
        or wrapper.get("signature_key_id") != key_id
        or not isinstance(wrapper.get("payload"), dict)
        or not isinstance(wrapper.get("signature"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{86}", wrapper["signature"]) is None
    ):
        raise DatabaseControlError("predecessor_receipt_wrapper_invalid")
    payload = dict(wrapper["payload"])
    payload_raw = _canonical_json(payload)
    try:
        signature = base64.b64decode(
            wrapper["signature"] + "==",
            altchars=b"-_",
            validate=True,
        )
        public_key.verify(
            signature,
            signature_domain
            + len(payload_raw).to_bytes(8, byteorder="big", signed=False)
            + payload_raw,
        )
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise DatabaseControlError("predecessor_receipt_signature_invalid") from exc
    return payload, _sha256_id(raw)


def _receipt_time(payload: Mapping[str, object]) -> tuple[int, int]:
    started = _exact_positive_int(payload.get("started_at_epoch"))
    finished = _exact_positive_int(payload.get("finished_at_epoch"))
    if started is None or finished is None or finished < started:
        raise DatabaseControlError("predecessor_receipt_time_invalid")
    return started, finished


def _validate_external_receipt(
    payload: Mapping[str, object],
    *,
    schema: str,
    operation: str | None,
    runtime_sha: str,
    deployment_id: str,
    authority_digest: str,
    receipt_key_id: str,
    host_machine_id_digest: str,
    transaction_started_at_epoch: int,
    backup_max_age_seconds: int,
) -> tuple[int, int]:
    if (
        payload.get("schema") != schema
        or payload.get("runtime_sha") != runtime_sha
        or payload.get("deployment_id") != deployment_id
        or payload.get("authority_digest") != authority_digest
        or payload.get("receipt_authority_key_id") != receipt_key_id
        or payload.get("host_machine_id_digest") != host_machine_id_digest
        or payload.get("transaction_started_at_epoch")
        != transaction_started_at_epoch
        or payload.get("backup_max_age_seconds") != backup_max_age_seconds
        or (operation is not None and payload.get("operation") != operation)
    ):
        raise DatabaseControlError("predecessor_receipt_binding_invalid")
    if "status" in payload and payload.get("status") != "verified":
        raise DatabaseControlError("predecessor_receipt_binding_invalid")
    if "production_ready" in payload and payload.get("production_ready") is not False:
        raise DatabaseControlError("predecessor_receipt_binding_invalid")
    if "secret_values_emitted" in payload and payload.get("secret_values_emitted") is not False:
        raise DatabaseControlError("predecessor_receipt_binding_invalid")
    return _receipt_time(payload)


def _validate_prior_database_receipt(
    payload: Mapping[str, object],
    *,
    operation: str,
    runtime_sha: str,
    deployment_id: str,
    web_image: str,
    database_image: str,
    authority_digest: str,
    receipt_key_id: str,
    host_machine_id_digest: str,
    runtime_inputs: list[dict[str, object]],
    database_substrate: dict[str, object],
    backup_receipt_sha256: str,
    purge_receipt_sha256: str,
    retirement_receipt_sha256: str,
    predecessor_receipt_sha256: str,
    transaction_started_at_epoch: int,
    backup_max_age_seconds: int,
) -> tuple[int, int]:
    if set(payload) != DATABASE_RECEIPT_PAYLOAD_KEYS:
        raise DatabaseControlError("predecessor_database_receipt_shape_invalid")
    bindings = {
        "authority_digest": authority_digest,
        "backup_max_age_seconds": backup_max_age_seconds,
        "backup_receipt_sha256": backup_receipt_sha256,
        "database": runtime_database.TARGET_DATABASE,
        "database_container": DATABASE_CONTAINER,
        "database_image": database_image,
        "database_image_id": database_substrate["image_id"],
        "database_repo_digest": database_substrate["repo_digest"],
        "deployment_id": deployment_id,
        "docker_network": DOCKER_NETWORK,
        "env_file": str(RUNTIME_ENV_FILE),
        "env_file_sha256": runtime_inputs[2]["sha256"],
        "host_machine_id_digest": host_machine_id_digest,
        "operation": operation,
        "predecessor_receipt_sha256": predecessor_receipt_sha256,
        "purge_receipt_sha256": purge_receipt_sha256,
        "receipt_authority_key_id": receipt_key_id,
        "retirement_receipt_sha256": retirement_receipt_sha256,
        "runtime_sha": runtime_sha,
        "schema": RECEIPT_SCHEMA,
        "status": "verified",
        "transaction_started_at_epoch": transaction_started_at_epoch,
        "web_image": web_image,
    }
    if any(payload.get(field) != expected for field, expected in bindings.items()):
        raise DatabaseControlError("predecessor_database_receipt_binding_invalid")
    if (
        payload.get("production_ready") is not False
        or payload.get("secret_values_emitted") is not False
        or payload.get("runtime_inputs") != runtime_inputs
        or payload.get("database_substrate_before") != database_substrate
        or payload.get("database_substrate_after") != database_substrate
    ):
        raise DatabaseControlError("predecessor_database_receipt_binding_invalid")
    result = payload.get("result")
    if (
        not isinstance(result, dict)
        or _exact_positive_int(result.get("database_oid"))
        != database_substrate["database_oid"]
    ):
        raise DatabaseControlError("predecessor_database_receipt_oid_invalid")
    return _receipt_time(payload)


def _load_predecessor_chain(
    *,
    operation: str,
    runtime_sha: str,
    deployment_id: str,
    web_image: str,
    database_image: str,
    authority: Mapping[str, object],
    authority_digest: str,
    public_key,  # type: ignore[no-untyped-def]
    receipt_key_id: str,
    host_machine_id_digest: str,
    gate_started_at: int,
) -> dict[str, object]:
    runtime_inputs = _validate_runtime_inputs(authority.get("runtime_inputs"))
    database_substrate = _validate_database_substrate(
        authority.get("database_substrate")
    )
    transaction_started_at_epoch = _exact_positive_int(
        authority.get("transaction_started_at_epoch")
    )
    backup_max_age_seconds = authority.get("backup_max_age_seconds")
    if (
        transaction_started_at_epoch is None
        or backup_max_age_seconds != 3600
        or isinstance(backup_max_age_seconds, bool)
    ):
        raise DatabaseControlError("authority_transaction_window_invalid")
    backup_payload, backup_digest = _read_signed_receipt(
        BACKUP_RECEIPT_ROOT / runtime_sha / deployment_id / "create.json",
        signature_domain=BACKUP_RECEIPT_SIGNATURE_DOMAIN,
        public_key=public_key,
        key_id=receipt_key_id,
    )
    backup_started, backup_finished = _validate_external_receipt(
        backup_payload,
        schema=BACKUP_RECEIPT_SCHEMA,
        operation=None,
        runtime_sha=runtime_sha,
        deployment_id=deployment_id,
        authority_digest=authority_digest,
        receipt_key_id=receipt_key_id,
        host_machine_id_digest=host_machine_id_digest,
        transaction_started_at_epoch=transaction_started_at_epoch,
        backup_max_age_seconds=backup_max_age_seconds,
    )
    if (
        backup_payload.get("database_substrate_before") != database_substrate
        or backup_payload.get("database_substrate_after") != database_substrate
        or backup_payload.get("database_image") != database_image
        or backup_payload.get("database_image_id")
        != database_substrate["image_id"]
        or backup_payload.get("database_repo_digest")
        != database_substrate["repo_digest"]
        or backup_payload.get("pre_purge_runtime_inputs")
        != authority.get("pre_purge_runtime_inputs")
    ):
        raise DatabaseControlError("backup_database_substrate_invalid")

    purge_payload, purge_digest = _read_signed_receipt(
        ISOLATION_RECEIPT_ROOT
        / runtime_sha
        / deployment_id
        / "purge-legacy-runtime-exposure.json",
        signature_domain=ISOLATION_RECEIPT_SIGNATURE_DOMAIN,
        public_key=public_key,
        key_id=receipt_key_id,
    )
    purge_started, purge_finished = _validate_external_receipt(
        purge_payload,
        schema=ISOLATION_RECEIPT_SCHEMA,
        operation="purge-legacy-runtime-exposure",
        runtime_sha=runtime_sha,
        deployment_id=deployment_id,
        authority_digest=authority_digest,
        receipt_key_id=receipt_key_id,
        host_machine_id_digest=host_machine_id_digest,
        transaction_started_at_epoch=transaction_started_at_epoch,
        backup_max_age_seconds=backup_max_age_seconds,
    )
    purge_result = purge_payload.get("result")
    purge_inputs = purge_result.get("inputs") if isinstance(purge_result, dict) else None
    expected_file_digests = {
        str(item["path"]): str(item["sha256"])
        for item in runtime_inputs
    }
    if (
        not isinstance(purge_result, dict)
        or set(purge_result) != PURGE_RESULT_KEYS
        or purge_result.get("backup_receipt_sha256") != backup_digest
        or purge_result.get("pre_purge_root_env_digest")
        != authority.get("pre_purge_root_env_digest")
        or purge_result.get("post_purge_root_env_digest")
        != authority.get("post_purge_root_env_digest")
        or not isinstance(purge_inputs, dict)
        or set(purge_inputs) != PURGE_INPUT_KEYS
        or purge_inputs.get("file_digests") != expected_file_digests
        or purge_inputs.get("google_key_count") != 5
        or purge_inputs.get("legacy_registration_email_present") is not False
        or purge_inputs.get("registration_email_key_count") != 8
        or purge_result.get("legacy_keys_removed") not in {0, 8}
        or isinstance(purge_result.get("legacy_keys_removed"), bool)
        or purge_result.get("rollback_artifact_expected_removed_keys") != 8
        or isinstance(
            purge_result.get("rollback_artifact_expected_removed_keys"), bool
        )
        or purge_payload.get("pre_purge_runtime_inputs")
        != authority.get("pre_purge_runtime_inputs")
        or purge_payload.get("runtime_inputs") != runtime_inputs
        or purge_payload.get("pre_purge_root_env_digest")
        != authority.get("pre_purge_root_env_digest")
        or purge_payload.get("post_purge_root_env_digest")
        != authority.get("post_purge_root_env_digest")
    ):
        raise DatabaseControlError("purge_receipt_chain_invalid")

    retirement_payload, retirement_digest = _read_signed_receipt(
        ISOLATION_RECEIPT_ROOT
        / runtime_sha
        / deployment_id
        / "retire-stale-propertyquarry-runtime.json",
        signature_domain=ISOLATION_RECEIPT_SIGNATURE_DOMAIN,
        public_key=public_key,
        key_id=receipt_key_id,
    )
    retirement_started, retirement_finished = _validate_external_receipt(
        retirement_payload,
        schema=ISOLATION_RECEIPT_SCHEMA,
        operation="retire-stale-propertyquarry-runtime",
        runtime_sha=runtime_sha,
        deployment_id=deployment_id,
        authority_digest=authority_digest,
        receipt_key_id=receipt_key_id,
        host_machine_id_digest=host_machine_id_digest,
        transaction_started_at_epoch=transaction_started_at_epoch,
        backup_max_age_seconds=backup_max_age_seconds,
    )
    retirement_result = retirement_payload.get("result")
    if (
        not isinstance(retirement_result, dict)
        or retirement_result.get("backup_receipt_sha256") != backup_digest
        or retirement_result.get("purge_receipt_sha256") != purge_digest
    ):
        raise DatabaseControlError("retirement_receipt_chain_invalid")
    if not (
        transaction_started_at_epoch <= backup_started
        <= backup_finished
        <= purge_started
        <= purge_finished
        <= retirement_started <= retirement_finished <= gate_started_at
        and backup_finished - backup_started <= backup_max_age_seconds
        and gate_started_at - backup_finished <= backup_max_age_seconds
    ):
        raise DatabaseControlError("predecessor_receipt_order_invalid")

    predecessor_digest = retirement_digest
    predecessor_finished = retirement_finished
    operation_index = OPERATIONS.index(operation)
    for previous_operation in OPERATIONS[:operation_index]:
        previous_payload, previous_digest = _read_signed_receipt(
            RECEIPT_ROOT
            / runtime_sha
            / deployment_id
            / f"{previous_operation}.json",
            signature_domain=RECEIPT_SIGNATURE_DOMAIN,
            public_key=public_key,
            key_id=receipt_key_id,
        )
        previous_started, previous_finished = _validate_prior_database_receipt(
            previous_payload,
            operation=previous_operation,
            runtime_sha=runtime_sha,
            deployment_id=deployment_id,
            web_image=web_image,
            database_image=database_image,
            authority_digest=authority_digest,
            receipt_key_id=receipt_key_id,
            host_machine_id_digest=host_machine_id_digest,
            runtime_inputs=runtime_inputs,
            database_substrate=database_substrate,
            backup_receipt_sha256=backup_digest,
            purge_receipt_sha256=purge_digest,
            retirement_receipt_sha256=retirement_digest,
            predecessor_receipt_sha256=predecessor_digest,
            transaction_started_at_epoch=transaction_started_at_epoch,
            backup_max_age_seconds=backup_max_age_seconds,
        )
        if previous_started < predecessor_finished or previous_finished > gate_started_at:
            raise DatabaseControlError("predecessor_receipt_order_invalid")
        predecessor_digest = previous_digest
        predecessor_finished = previous_finished
    return {
        "backup_receipt_sha256": backup_digest,
        "predecessor_finished_at_epoch": predecessor_finished,
        "predecessor_receipt_sha256": predecessor_digest,
        "purge_receipt_sha256": purge_digest,
        "retirement_receipt_sha256": retirement_digest,
    }


def _write_receipt(path: Path, wrapper: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent_metadata = path.parent.lstat()
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        or parent_metadata.st_uid != 0
        or parent_metadata.st_gid != 0
    ):
        raise DatabaseControlError("receipt_parent_owner_invalid")
    encoded = _canonical_json(dict(wrapper)) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, 0, 0)
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_request(
    args: argparse.Namespace,
) -> tuple[str, str, str, str, str, Path]:
    operation = str(args.operation or "").strip()
    runtime_sha = str(args.runtime_sha or "").strip().lower()
    deployment_id = str(args.deployment_id or "").strip().lower()
    web_image = str(args.web_image or "").strip().lower()
    database_image = str(args.database_image or "").strip().lower()
    receipt_path = Path(args.receipt).resolve()
    if operation not in OPERATIONS:
        raise DatabaseControlError("database_operation_invalid")
    if not RUNTIME_SHA_RE.fullmatch(runtime_sha):
        raise DatabaseControlError("runtime_sha_invalid")
    if not DEPLOYMENT_ID_RE.fullmatch(deployment_id):
        raise DatabaseControlError("deployment_id_invalid")
    if not IMAGE_RE.fullmatch(web_image):
        raise DatabaseControlError("web_image_invalid")
    if database_image != EXPECTED_DATABASE_IMAGE:
        raise DatabaseControlError("database_image_invalid")
    expected_receipt = (
        RECEIPT_ROOT / runtime_sha / deployment_id / f"{operation}.json"
    )
    if receipt_path != expected_receipt:
        raise DatabaseControlError("receipt_path_invalid")
    if os.geteuid() != 0:
        raise DatabaseControlError("root_required")
    return (
        operation,
        runtime_sha,
        deployment_id,
        web_image,
        database_image,
        receipt_path,
    )


def execute(args: argparse.Namespace) -> dict[str, object]:
    (
        operation,
        runtime_sha,
        deployment_id,
        web_image,
        database_image,
        receipt_path,
    ) = _validate_request(args)
    started_at = int(time.time())
    authority, authority_digest = _load_authority(
        runtime_sha=runtime_sha,
        deployment_id=deployment_id,
        web_image=web_image,
        database_image=database_image,
    )
    private, public, receipt_key_id = backup_contract._load_receipt_authority(  # noqa: SLF001
        backup_contract.BackupPaths(),
        require_root_owner=True,
    )
    if authority.get("receipt_authority_key_id") != receipt_key_id:
        raise DatabaseControlError("receipt_authority_binding_invalid")
    host_machine_id_digest = backup_contract._machine_id_digest(  # noqa: SLF001
        MACHINE_ID_PATH
    )
    predecessor_bindings = _load_predecessor_chain(
        operation=operation,
        runtime_sha=runtime_sha,
        deployment_id=deployment_id,
        web_image=web_image,
        database_image=database_image,
        authority=authority,
        authority_digest=authority_digest,
        public_key=public,
        receipt_key_id=receipt_key_id,
        host_machine_id_digest=host_machine_id_digest,
        gate_started_at=started_at,
    )
    expected_runtime_inputs = _validate_runtime_inputs(
        authority.get("runtime_inputs")
    )
    runtime_inputs_before = _measure_runtime_inputs()
    if runtime_inputs_before != expected_runtime_inputs:
        raise DatabaseControlError("runtime_inputs_authority_mismatch")
    expected_database_substrate = _validate_database_substrate(
        authority.get("database_substrate")
    )
    database_identity_before = _verify_database_container_image(database_image)
    if database_identity_before != expected_database_substrate:
        raise DatabaseControlError("database_substrate_authority_mismatch")
    result = _operate(
        operation=operation,
        runtime_sha=runtime_sha,
        web_image=web_image,
    )
    database_identity_after = _verify_database_container_image(database_image)
    runtime_inputs_after = _measure_runtime_inputs()
    if (
        database_identity_after != database_identity_before
        or database_identity_after != expected_database_substrate
    ):
        raise DatabaseControlError("database_container_identity_changed")
    if runtime_inputs_after != runtime_inputs_before:
        raise DatabaseControlError("runtime_inputs_changed")
    result_oid = (
        _exact_positive_int(result.get("database_oid"))
        if isinstance(result, dict)
        else None
    )
    if result_oid != database_identity_after["database_oid"]:
        raise DatabaseControlError("database_operation_oid_changed")
    env_raw = _validate_env_file(RUNTIME_ENV_FILE)
    if _sha256_id(env_raw) != expected_runtime_inputs[2]["sha256"]:
        raise DatabaseControlError("runtime_database_environment_changed")
    finished_at = int(time.time())
    payload = {
        "authority_digest": authority_digest,
        "backup_max_age_seconds": authority["backup_max_age_seconds"],
        "backup_receipt_sha256": predecessor_bindings[
            "backup_receipt_sha256"
        ],
        "database": runtime_database.TARGET_DATABASE,
        "database_container": DATABASE_CONTAINER,
        "database_image": database_image,
        "database_image_id": database_identity_after["image_id"],
        "database_repo_digest": database_identity_after["repo_digest"],
        "database_substrate_after": database_identity_after,
        "database_substrate_before": database_identity_before,
        "deployment_id": deployment_id,
        "docker_network": DOCKER_NETWORK,
        "env_file": str(RUNTIME_ENV_FILE),
        "env_file_sha256": _sha256_id(env_raw),
        "finished_at_epoch": finished_at,
        "host_machine_id_digest": host_machine_id_digest,
        "operation": operation,
        "predecessor_receipt_sha256": predecessor_bindings[
            "predecessor_receipt_sha256"
        ],
        "production_ready": False,
        "purge_receipt_sha256": predecessor_bindings["purge_receipt_sha256"],
        "receipt_authority_key_id": receipt_key_id,
        "result": result,
        "retirement_receipt_sha256": predecessor_bindings[
            "retirement_receipt_sha256"
        ],
        "runtime_inputs": expected_runtime_inputs,
        "runtime_sha": runtime_sha,
        "schema": RECEIPT_SCHEMA,
        "secret_values_emitted": False,
        "started_at_epoch": started_at,
        "status": "verified",
        "transaction_started_at_epoch": authority[
            "transaction_started_at_epoch"
        ],
        "web_image": web_image,
    }
    if set(payload) != DATABASE_RECEIPT_PAYLOAD_KEYS:  # pragma: no cover
        raise DatabaseControlError("database_receipt_payload_shape_invalid")
    wrapper = _sign_payload(payload, private_key=private, key_id=receipt_key_id)
    _write_receipt(receipt_path, wrapper)
    return wrapper


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--runtime-sha", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--web-image", required=True)
    parser.add_argument("--database-image", required=True)
    parser.add_argument("--receipt", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        wrapper = execute(_parser().parse_args(argv))
    except DatabaseControlError as exc:
        print(
            json.dumps(
                {"detail": exc.detail, "reason": exc.code, "status": "failed"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "operation": wrapper["payload"]["operation"],
                "status": "ok",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
