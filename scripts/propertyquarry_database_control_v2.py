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

try:  # Repo execution.
    from scripts import propertyquarry_predeploy_backup_v2 as backup_contract
    from scripts import provision_propertyquarry_runtime_database as runtime_database
except ImportError:  # Installed, adjacent executable execution.
    from importlib.machinery import SourceFileLoader
    import importlib.util

    _installed_backup_path = Path(__file__).with_name(
        "propertyquarry-predeploy-backup-v2"
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
OPERATIONS = (
    "provision-roles",
    "migrate-schema",
    "harden-runtime-acl",
    "verify-schema-readiness",
)
DATABASE_CONTAINER = "propertyquarry-db-live"
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
RECEIPT_ROOT = Path(
    "/var/lib/propertyquarry-release-single-host-v2/database-receipts"
)
MACHINE_ID_PATH = Path("/etc/machine-id")
RUNTIME_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(r"^[a-z0-9./_-]+@sha256:[0-9a-f]{64}$")


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
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) > max_bytes:
            raise DatabaseControlError("required_file_size_invalid", str(path))
        return bytes(payload)
    finally:
        os.close(descriptor)


def _load_authority(*, runtime_sha: str, web_image: str) -> tuple[dict[str, object], str]:
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
    if (
        authority.get("schema")
        != "propertyquarry.release-control.single-host-profile.v2"
        or authority.get("runtime_sha") != runtime_sha
        or authority.get("web_image") != web_image
    ):
        raise DatabaseControlError("authority_binding_invalid")
    return authority, _sha256_id(raw)


def _validate_env_file(path: Path) -> bytes:
    return _read_regular(
        path,
        max_bytes=32 * 1024,
        mode=0o600,
        uid=1000,
        gid=1000,
    )


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


def _validate_request(args: argparse.Namespace) -> tuple[str, str, str, Path]:
    operation = str(args.operation or "").strip()
    runtime_sha = str(args.runtime_sha or "").strip().lower()
    web_image = str(args.web_image or "").strip().lower()
    receipt_path = Path(args.receipt).resolve()
    if operation not in OPERATIONS:
        raise DatabaseControlError("database_operation_invalid")
    if not RUNTIME_SHA_RE.fullmatch(runtime_sha):
        raise DatabaseControlError("runtime_sha_invalid")
    if not IMAGE_RE.fullmatch(web_image):
        raise DatabaseControlError("web_image_invalid")
    expected_receipt = RECEIPT_ROOT / runtime_sha / f"{operation}.json"
    if receipt_path != expected_receipt:
        raise DatabaseControlError("receipt_path_invalid")
    if os.geteuid() != 0:
        raise DatabaseControlError("root_required")
    return operation, runtime_sha, web_image, receipt_path


def execute(args: argparse.Namespace) -> dict[str, object]:
    operation, runtime_sha, web_image, receipt_path = _validate_request(args)
    started_at = int(time.time())
    authority, authority_digest = _load_authority(
        runtime_sha=runtime_sha,
        web_image=web_image,
    )
    private, _public, receipt_key_id = backup_contract._load_receipt_authority(  # noqa: SLF001
        backup_contract.BackupPaths(),
        require_root_owner=True,
    )
    if authority.get("receipt_authority_key_id") != receipt_key_id:
        raise DatabaseControlError("receipt_authority_binding_invalid")
    result = _operate(
        operation=operation,
        runtime_sha=runtime_sha,
        web_image=web_image,
    )
    env_raw = _validate_env_file(RUNTIME_ENV_FILE)
    finished_at = int(time.time())
    payload = {
        "authority_digest": authority_digest,
        "database": runtime_database.TARGET_DATABASE,
        "database_container": DATABASE_CONTAINER,
        "docker_network": DOCKER_NETWORK,
        "env_file": str(RUNTIME_ENV_FILE),
        "env_file_sha256": _sha256_id(env_raw),
        "finished_at_epoch": finished_at,
        "host_machine_id_digest": backup_contract._machine_id_digest(  # noqa: SLF001
            MACHINE_ID_PATH
        ),
        "operation": operation,
        "production_ready": False,
        "receipt_authority_key_id": receipt_key_id,
        "result": result,
        "runtime_sha": runtime_sha,
        "schema": RECEIPT_SCHEMA,
        "secret_values_emitted": False,
        "started_at_epoch": started_at,
        "status": "verified",
        "web_image": web_image,
    }
    wrapper = _sign_payload(payload, private_key=private, key_id=receipt_key_id)
    _write_receipt(receipt_path, wrapper)
    return wrapper


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--runtime-sha", required=True)
    parser.add_argument("--web-image", required=True)
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
