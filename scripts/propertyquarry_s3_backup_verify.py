#!/usr/bin/env python3
"""Upload and read back one encrypted PropertyQuarry backup from locked S3."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
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
from typing import Any, Callable, Mapping

if __package__:
    from .propertyquarry_postgres_dr import (
        AWS_CLI_MINIMAL_PATH,
        DisasterRecoveryError,
        REMOTE_PROVIDER_CONTRACTS,
        _AWS_REQUEST_ID_RE,
        _attest_aws_cli,
        _minimal_hook_environment,
        _normalize_s3_etag,
        _run_attested_aws_cli,
    )
else:
    from propertyquarry_postgres_dr import (
        AWS_CLI_MINIMAL_PATH,
        DisasterRecoveryError,
        REMOTE_PROVIDER_CONTRACTS,
        _AWS_REQUEST_ID_RE,
        _attest_aws_cli,
        _minimal_hook_environment,
        _normalize_s3_etag,
        _run_attested_aws_cli,
    )


S3_BACKUP_VERIFY_CONTRACT = "propertyquarry.s3_locked_backup_verify.v1"
MINIMUM_OBJECT_LOCK_DAYS = 30
MAXIMUM_OBJECT_LOCK_DAYS = 3650
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_S3_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_AWS_REGION_RE = re.compile(r"^[a-z]{2}(?:-gov)?-[a-z]+-[1-9][0-9]*$")
_IPV4_ADDRESS_RE = re.compile(r"^[0-9]{1,3}(?:\.[0-9]{1,3}){3}$")
_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._=-]{0,127}$")

Runner = Callable[..., Any]
Clock = Callable[[], float]


class S3BackupVerificationError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = str(code or "s3_backup_verification_failed")
        super().__init__(self.code)


def _require_env(env: Mapping[str, str], name: str) -> str:
    value = str(env.get(name) or "").strip()
    if not value:
        raise S3BackupVerificationError(f"{name.lower()}_missing")
    return value


def _sha256_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest()


def _open_artifact(env: Mapping[str, str]) -> tuple[int, Path, int, str]:
    raw_path = _require_env(env, "PROPERTYQUARRY_BACKUP_ARTIFACT_PATH")
    artifact = Path(raw_path)
    if not artifact.is_absolute() or artifact != Path(os.path.abspath(artifact)):
        raise S3BackupVerificationError("backup_artifact_path_invalid")
    expected_sha256 = _require_env(
        env,
        "PROPERTYQUARRY_BACKUP_ARTIFACT_SHA256",
    ).lower()
    if _SHA256_RE.fullmatch(expected_sha256) is None:
        raise S3BackupVerificationError("backup_artifact_sha256_invalid")
    try:
        expected_size = int(
            _require_env(env, "PROPERTYQUARRY_BACKUP_ARTIFACT_SIZE_BYTES")
        )
    except ValueError as exc:
        raise S3BackupVerificationError("backup_artifact_size_invalid") from exc
    if expected_size <= 0:
        raise S3BackupVerificationError("backup_artifact_size_invalid")
    if _require_env(env, "PROPERTYQUARRY_BACKUP_ARTIFACT_ENCRYPTED") != "1":
        raise S3BackupVerificationError("backup_artifact_not_encrypted")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(artifact, flags)
    except OSError as exc:
        raise S3BackupVerificationError("backup_artifact_untrusted") from exc
    try:
        metadata = os.fstat(descriptor)
        lexical = os.lstat(artifact)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(lexical.st_mode)
            or (metadata.st_dev, metadata.st_ino)
            != (lexical.st_dev, lexical.st_ino)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, os.geteuid()}
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
            or metadata.st_size != expected_size
            or _sha256_descriptor(descriptor) != expected_sha256
        ):
            raise S3BackupVerificationError("backup_artifact_untrusted")
        return descriptor, artifact, expected_size, expected_sha256
    except Exception:
        os.close(descriptor)
        raise


def _provider_config(
    env: Mapping[str, str],
    *,
    artifact_sha256: str,
) -> tuple[str, str, str, int]:
    raw_region = str(
        env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or ""
    )
    region = raw_region.strip()
    raw_bucket = str(env.get("PROPERTYQUARRY_DR_S3_BUCKET") or "")
    bucket = _require_env(env, "PROPERTYQUARRY_DR_S3_BUCKET")
    raw_prefix = str(env.get("PROPERTYQUARRY_DR_S3_KEY_PREFIX") or "")
    prefix = _require_env(env, "PROPERTYQUARRY_DR_S3_KEY_PREFIX")
    if (
        raw_region != region
        or region != region.lower()
        or raw_bucket != bucket
        or bucket != bucket.lower()
        or _AWS_REGION_RE.fullmatch(region) is None
        or _S3_BUCKET_RE.fullmatch(bucket) is None
        or ".." in bucket
        or bucket in {"file", "local", "localhost"}
        or _IPV4_ADDRESS_RE.fullmatch(bucket) is not None
    ):
        raise S3BackupVerificationError("s3_provider_identity_invalid")
    segments = prefix.split("/")
    if (
        raw_prefix != prefix
        or prefix != prefix.strip("/")
        or not segments
        or any(_KEY_SEGMENT_RE.fullmatch(segment) is None for segment in segments)
    ):
        raise S3BackupVerificationError("s3_object_key_prefix_invalid")
    try:
        retention_days = int(
            _require_env(env, "PROPERTYQUARRY_DR_S3_OBJECT_LOCK_DAYS")
        )
    except ValueError as exc:
        raise S3BackupVerificationError("s3_object_lock_days_invalid") from exc
    if not MINIMUM_OBJECT_LOCK_DAYS <= retention_days <= MAXIMUM_OBJECT_LOCK_DAYS:
        raise S3BackupVerificationError("s3_object_lock_days_invalid")
    return (
        region,
        bucket,
        f"{prefix}/{artifact_sha256}.dump.gpg",
        retention_days,
    )


def _json_result(result: object, *, step: str) -> dict[str, object]:
    try:
        payload = json.loads(str(getattr(result, "stdout", "") or ""))
    except Exception as exc:
        raise S3BackupVerificationError(f"{step}_response_invalid") from exc
    if not isinstance(payload, dict):
        raise S3BackupVerificationError(f"{step}_response_invalid")
    return payload


def _provider_request_id(payload: Mapping[str, object], result: object) -> str:
    metadata = payload.get("ResponseMetadata")
    nested = metadata.get("RequestId") if isinstance(metadata, Mapping) else ""
    direct = str(payload.get("RequestId") or nested or "").strip()
    if direct:
        return direct
    match = _AWS_REQUEST_ID_RE.search(str(getattr(result, "stderr", "") or ""))
    return str(match.group(1) if match else "").strip()


def _parse_provider_time(value: object, *, step: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise S3BackupVerificationError(f"{step}_object_lock_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise S3BackupVerificationError(f"{step}_object_lock_invalid")
    return parsed.astimezone(timezone.utc)


def upload_and_verify_s3_backup(
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    clock: Clock = time.time,
) -> dict[str, object]:
    """Upload one exact encrypted artifact and verify locked bytes by read-back."""

    env = dict(os.environ if environ is None else environ)
    artifact_fd, _artifact_path, artifact_size, artifact_sha256 = _open_artifact(env)
    try:
        region, bucket, object_key, retention_days = _provider_config(
            env,
            artifact_sha256=artifact_sha256,
        )
        now = datetime.fromtimestamp(clock(), timezone.utc).replace(microsecond=0)
        retain_until = now + timedelta(days=retention_days)
        retain_until_text = retain_until.isoformat().replace("+00:00", "Z")
        checksum_base64 = base64.b64encode(bytes.fromhex(artifact_sha256)).decode("ascii")
        endpoint_suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
        endpoint = f"https://s3.{region}.{endpoint_suffix}"
        provider_env = _minimal_hook_environment(
            env,
            declared_keys_env=None,
            provider_keys=True,
        )
        provider_env.update(
            {
                "AWS_IGNORE_CONFIGURED_ENDPOINT_URLS": "true",
                "AWS_CLI_AUTO_PROMPT": "off",
                "AWS_CLI_HISTORY_FILE": "/dev/null",
                "AWS_PAGER": "",
                "AWS_DEFAULT_OUTPUT": "json",
                "AWS_EC2_METADATA_DISABLED": "true",
                "PATH": AWS_CLI_MINIMAL_PATH,
            }
        )
        commands: list[dict[str, object]] = []
        attestation = _attest_aws_cli(
            env=env,
            runner=runner,
            commands=commands,
        )
        artifact_fd_path = f"/proc/self/fd/{artifact_fd}"
        put_result = _run_attested_aws_cli(
            step="put_locked_backup_object",
            arguments=[
                "--debug",
                "--region",
                region,
                "--endpoint-url",
                endpoint,
                "s3api",
                "put-object",
                "--bucket",
                bucket,
                "--key",
                object_key,
                "--body",
                artifact_fd_path,
                "--checksum-algorithm",
                "SHA256",
                "--checksum-sha256",
                checksum_base64,
                "--server-side-encryption",
                "AES256",
                "--object-lock-mode",
                "COMPLIANCE",
                "--object-lock-retain-until-date",
                retain_until_text,
                "--metadata",
                f"propertyquarry-sha256={artifact_sha256}",
            ],
            attestation=attestation,
            env=env,
            provider_env=provider_env,
            runner=runner,
            commands=commands,
            recorded_command=[
                str(attestation["path"]),
                "s3api",
                "put-object",
                "<locked-object-identity-redacted>",
            ],
            extra_pass_fds=(artifact_fd,),
        )
        put = _json_result(put_result, step="put_object")
        version_id = str(put.get("VersionId") or "").strip()
        etag = _normalize_s3_etag(put.get("ETag"))
        if (
            not version_id
            or version_id.lower() in {"latest", "null", "none", "unversioned"}
            or not etag
            or str(put.get("ChecksumSHA256") or "").strip() != checksum_base64
        ):
            raise S3BackupVerificationError("put_object_identity_invalid")

        head_result = _run_attested_aws_cli(
            step="head_locked_backup_object",
            arguments=[
                "--debug",
                "--region",
                region,
                "--endpoint-url",
                endpoint,
                "s3api",
                "head-object",
                "--bucket",
                bucket,
                "--key",
                object_key,
                "--version-id",
                version_id,
                "--checksum-mode",
                "ENABLED",
            ],
            attestation=attestation,
            env=env,
            provider_env=provider_env,
            runner=runner,
            commands=commands,
            recorded_command=[
                str(attestation["path"]),
                "s3api",
                "head-object",
                "<locked-object-identity-redacted>",
            ],
        )
        head = _json_result(head_result, step="head_object")

        with tempfile.TemporaryDirectory(prefix="propertyquarry-s3-readback-") as temp_dir:
            download = Path(temp_dir) / "readback.dump.gpg"
            get_result = _run_attested_aws_cli(
                step="get_locked_backup_object",
                arguments=[
                    "--debug",
                    "--region",
                    region,
                    "--endpoint-url",
                    endpoint,
                    "s3api",
                    "get-object",
                    "--bucket",
                    bucket,
                    "--key",
                    object_key,
                    "--version-id",
                    version_id,
                    "--if-match",
                    etag,
                    "--checksum-mode",
                    "ENABLED",
                    str(download),
                ],
                attestation=attestation,
                env=env,
                provider_env=provider_env,
                runner=runner,
                commands=commands,
                recorded_command=[
                    str(attestation["path"]),
                    "s3api",
                    "get-object",
                    "<locked-object-identity-redacted>",
                ],
            )
            retrieved = _json_result(get_result, step="get_object")
            readback_flags = (
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            try:
                readback_fd = os.open(download, readback_flags)
            except OSError as exc:
                raise S3BackupVerificationError(
                    "get_object_checksum_mismatch"
                ) from exc
            try:
                metadata = os.fstat(readback_fd)
                lexical = os.lstat(download)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_ISLNK(lexical.st_mode)
                    or (metadata.st_dev, metadata.st_ino)
                    != (lexical.st_dev, lexical.st_ino)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != os.geteuid()
                    or metadata.st_mode
                    & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID)
                    or metadata.st_size != artifact_size
                    or _sha256_descriptor(readback_fd) != artifact_sha256
                ):
                    raise S3BackupVerificationError(
                        "get_object_checksum_mismatch"
                    )
            finally:
                os.close(readback_fd)

        for response, step in ((head, "head_object"), (retrieved, "get_object")):
            metadata = response.get("Metadata")
            response_retain_until = _parse_provider_time(
                response.get("ObjectLockRetainUntilDate"),
                step=step,
            )
            try:
                response_size = int(response.get("ContentLength") or 0)
            except (TypeError, ValueError) as exc:
                raise S3BackupVerificationError(
                    f"{step}_identity_invalid"
                ) from exc
            if (
                str(response.get("VersionId") or "").strip() != version_id
                or _normalize_s3_etag(response.get("ETag")) != etag
                or response_size != artifact_size
                or str(response.get("ChecksumSHA256") or "").strip()
                != checksum_base64
                or str(response.get("ServerSideEncryption") or "").strip()
                != "AES256"
                or str(response.get("ObjectLockMode") or "").strip().upper()
                != "COMPLIANCE"
                or response_retain_until < retain_until
                or not isinstance(metadata, Mapping)
                or str(metadata.get("propertyquarry-sha256") or "").strip().lower()
                != artifact_sha256
            ):
                raise S3BackupVerificationError(f"{step}_identity_invalid")

        head_request_id = _provider_request_id(head, head_result)
        get_request_id = _provider_request_id(retrieved, get_result)
        if (
            not head_request_id
            or not get_request_id
            or head_request_id == get_request_id
        ):
            raise S3BackupVerificationError("provider_request_identity_missing")
        verified_at = datetime.fromtimestamp(clock(), timezone.utc).replace(
            microsecond=0
        )
        return {
            "contract": S3_BACKUP_VERIFY_CONTRACT,
            "provider": "s3",
            "backend": "aws_s3api",
            "region": region,
            "bucket": bucket,
            "object_key": object_key,
            "version_id": version_id,
            "etag": etag,
            "sha256": artifact_sha256,
            "size_bytes": artifact_size,
            "encrypted": True,
            "off_host": True,
            "object_exists": True,
            "checksum_verified": True,
            "immutability_verified": True,
            "object_lock_mode": "COMPLIANCE",
            "object_lock_retain_until": retain_until_text,
            "provider_request_id": f"{head_request_id}:{get_request_id}",
            "verified_at": verified_at.isoformat().replace("+00:00", "Z"),
            "verification_method": REMOTE_PROVIDER_CONTRACTS["s3"][
                "verification_method"
            ],
        }
    finally:
        os.close(artifact_fd)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("propertyquarry_s3_backup_verify_takes_no_arguments", file=sys.stderr)
        return 2
    try:
        receipt = upload_and_verify_s3_backup()
    except (S3BackupVerificationError, DisasterRecoveryError) as exc:
        code = str(getattr(exc, "code", "s3_backup_verification_failed"))
        print(f"PropertyQuarry locked S3 backup refused: {code}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
