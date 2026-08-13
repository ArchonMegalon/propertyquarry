#!/usr/bin/env python3
"""Materialize a secret-safe receipt for PropertyQuarry's live external gates."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence

if __package__:
    from .propertyquarry_postgres_dr import (
        DisasterRecoveryError,
        _attest_aws_cli,
        _minimal_hook_environment,
        _run_attested_aws_cli,
    )
else:
    from propertyquarry_postgres_dr import (
        DisasterRecoveryError,
        _attest_aws_cli,
        _minimal_hook_environment,
        _run_attested_aws_cli,
    )


SCHEMA = "propertyquarry.live_external_authority_probe.v1"
API_CONTAINER = "propertyquarry-api"
BACKUP_CONTAINER = "propertyquarry-backup-live"
GLOBAL_TRUST_STORE = Path(
    "/etc/propertyquarry/release-control/global-governance-trust-store.v1.json"
)
PUBLIC_LAUNCH_AUTHORITY = Path(
    "/run/propertyquarry/release-control/propertyquarry-public-launch-authority.v2.json"
)

DR_PROVIDER_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "PROPERTYQUARRY_DR_S3_BUCKET",
    "PROPERTYQUARRY_DR_S3_KEY_PREFIX",
    "PROPERTYQUARRY_DR_S3_OBJECT_LOCK_DAYS",
)
DR_RECOVERY_KEYS = (
    "PROPERTYQUARRY_BACKUP_ENCRYPTION_RECIPIENT",
    "PROPERTYQUARRY_RESTORE_DATABASE_URL",
    "PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM",
)
BILLING_KEYS = (
    "PAYPAL_CLIENT_ID",
    "PAYPAL_SECRET",
    "PAYFUNNELS_API_KEY",
    "PAYFUNNELS_WEBHOOK_SECRET",
    "PAYFUNNELS_PLUS_CHECKOUT_URL",
    "PAYFUNNELS_AGENT_CHECKOUT_URL",
    "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_CONTRACT",
    "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PRINCIPAL_SHA256",
    "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_RECEIPT_SHA256",
    "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_VERIFIED_AT",
)
FLIPLINK_KEYS = (
    "FLIPLINK_LOGIN_EMAIL",
    "FLIPLINK_LOGIN_PASSWORD",
    "FLIPLINK_BROWSERACT_ENABLED",
    "FLIPLINK_WEBHOOK_SECRET",
    "FLIPLINK_CUSTOM_DOMAIN",
)

Runner = Callable[..., Any]
Clock = Callable[[], float]
Which = Callable[[str], str | None]


def _utc_iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _present_names(environ: Mapping[str, str], names: Sequence[str]) -> dict[str, bool]:
    return {name: bool(str(environ.get(name) or "").strip()) for name in names}


def _container_projection(
    container: str,
    *,
    allowlisted_names: Sequence[str],
    runner: Runner,
) -> dict[str, object]:
    try:
        result = runner(
            ["docker", "container", "inspect", container],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "available": False,
            "running": False,
            "healthy": False,
            "configured": {name: False for name in allowlisted_names},
        }
    if int(getattr(result, "returncode", 1)) != 0:
        return {
            "available": False,
            "running": False,
            "healthy": False,
            "configured": {name: False for name in allowlisted_names},
        }
    try:
        payload = json.loads(str(getattr(result, "stdout", "") or ""))
        row = payload[0]
        state = dict(row.get("State") or {})
        raw_env = list((row.get("Config") or {}).get("Env") or [])
    except (IndexError, TypeError, ValueError, AttributeError):
        return {
            "available": False,
            "running": False,
            "healthy": False,
            "configured": {name: False for name in allowlisted_names},
        }
    configured: dict[str, bool] = {name: False for name in allowlisted_names}
    allowed = set(allowlisted_names)
    for entry in raw_env:
        key, separator, value = str(entry).partition("=")
        if separator and key in allowed:
            configured[key] = bool(value.strip())
    health = dict(state.get("Health") or {})
    return {
        "available": True,
        "running": state.get("Status") == "running",
        "healthy": health.get("Status") == "healthy",
        "configured": configured,
    }


def _gpg_projection(*, runner: Runner, which: Which) -> dict[str, object]:
    binary = which("gpg")
    if not binary:
        return {"binary_available": False, "public_recipient_count": 0}
    try:
        result = runner(
            [binary, "--batch", "--with-colons", "--list-keys"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "HOME": os.path.expanduser("~")},
        )
    except (OSError, subprocess.SubprocessError):
        return {"binary_available": True, "public_recipient_count": 0}
    count = sum(
        1
        for line in str(getattr(result, "stdout", "") or "").splitlines()
        if line.split(":", 1)[0] == "pub"
    )
    return {
        "binary_available": True,
        "public_recipient_count": count if int(getattr(result, "returncode", 1)) == 0 else 0,
    }


def _aws_projection(
    environ: Mapping[str, str],
    *,
    runner: Runner,
    attestor: Callable[..., Mapping[str, object]],
    cli_runner: Callable[..., Any],
) -> dict[str, object]:
    commands: list[dict[str, object]] = []
    try:
        attestation = dict(attestor(env=environ, runner=runner, commands=commands))
    except (DisasterRecoveryError, OSError, ValueError) as exc:
        return {
            "cli_attested": False,
            "cli_version": "",
            "cli_sha256": "",
            "caller_identity_verified": False,
            "identity_state": str(getattr(exc, "code", "cli_attestation_failed")),
        }
    provider_env = _minimal_hook_environment(
        environ,
        declared_keys_env=None,
        provider_keys=True,
    )
    provider_env.update(
        {
            "AWS_CLI_AUTO_PROMPT": "off",
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_PAGER": "",
        }
    )
    try:
        result = cli_runner(
            step="probe_aws_sts_identity",
            arguments=("sts", "get-caller-identity", "--output", "json", "--no-cli-pager"),
            attestation=attestation,
            env=environ,
            provider_env=provider_env,
            runner=runner,
            commands=commands,
            recorded_command=("<attested-aws-cli>", "sts", "get-caller-identity", "--output", "json"),
        )
        identity = json.loads(str(getattr(result, "stdout", "") or ""))
        verified = bool(
            isinstance(identity, dict)
            and str(identity.get("Account") or "").strip()
            and str(identity.get("Arn") or "").strip()
            and str(identity.get("UserId") or "").strip()
        )
        state = "verified" if verified else "identity_response_invalid"
    except (DisasterRecoveryError, OSError, TypeError, ValueError):
        verified = False
        state = "no_usable_scoped_identity"
    return {
        "cli_attested": True,
        "cli_version": str(attestation.get("version") or ""),
        "cli_sha256": str(attestation.get("sha256") or ""),
        "caller_identity_verified": verified,
        "identity_state": state,
    }


def _trusted_authority_file(path: Path) -> bool:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return False
    return bool(
        stat.S_ISREG(observed.st_mode)
        and not stat.S_ISLNK(observed.st_mode)
        and observed.st_uid == 0
        and observed.st_nlink == 1
        and not observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and observed.st_size > 0
    )


def build_live_external_authority_receipt(
    *,
    environ: Mapping[str, str] | None = None,
    runner: Runner = subprocess.run,
    clock: Clock = time.time,
    which: Which = shutil.which,
    attestor: Callable[..., Mapping[str, object]] = _attest_aws_cli,
    cli_runner: Callable[..., Any] = _run_attested_aws_cli,
    global_trust_store: Path = GLOBAL_TRUST_STORE,
    public_launch_authority: Path = PUBLIC_LAUNCH_AUTHORITY,
    authority_validator: Callable[[Path], bool] = _trusted_authority_file,
) -> dict[str, object]:
    env = dict(os.environ if environ is None else environ)
    api = _container_projection(
        API_CONTAINER,
        allowlisted_names=(*BILLING_KEYS, *FLIPLINK_KEYS),
        runner=runner,
    )
    backup = _container_projection(
        BACKUP_CONTAINER,
        allowlisted_names=(*DR_PROVIDER_KEYS, *DR_RECOVERY_KEYS),
        runner=runner,
    )
    host_dr = _present_names(env, (*DR_PROVIDER_KEYS, *DR_RECOVERY_KEYS))
    gpg = _gpg_projection(runner=runner, which=which)
    aws = _aws_projection(
        env,
        runner=runner,
        attestor=attestor,
        cli_runner=cli_runner,
    )
    backup_config = dict(backup["configured"])
    configured = {
        name: bool(host_dr.get(name) or backup_config.get(name))
        for name in (*DR_PROVIDER_KEYS, *DR_RECOVERY_KEYS)
    }
    s3_target_ready = all(
        configured[name]
        for name in (
            "AWS_REGION",
            "PROPERTYQUARRY_DR_S3_BUCKET",
            "PROPERTYQUARRY_DR_S3_KEY_PREFIX",
            "PROPERTYQUARRY_DR_S3_OBJECT_LOCK_DAYS",
        )
    ) or all(
        configured[name]
        for name in (
            "AWS_DEFAULT_REGION",
            "PROPERTYQUARRY_DR_S3_BUCKET",
            "PROPERTYQUARRY_DR_S3_KEY_PREFIX",
            "PROPERTYQUARRY_DR_S3_OBJECT_LOCK_DAYS",
        )
    )
    recipient_ready = bool(
        configured["PROPERTYQUARRY_BACKUP_ENCRYPTION_RECIPIENT"]
        and int(gpg["public_recipient_count"]) > 0
    )
    restore_toolchain = {
        name: bool(which(name)) for name in ("gpg", "pg_dump", "pg_restore", "psql")
    }
    restore_target_ready = bool(
        configured["PROPERTYQUARRY_RESTORE_DATABASE_URL"]
        and configured["PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM"]
        and all(restore_toolchain.values())
    )
    billing_config = dict(api["configured"])
    billing_ready = all(billing_config.get(name, False) for name in BILLING_KEYS)
    fliplink_config = dict(api["configured"])
    fliplink_ready = all(fliplink_config.get(name, False) for name in FLIPLINK_KEYS)
    launch_files = {
        "global_trust_store": authority_validator(global_trust_store),
        "public_launch_authority": authority_validator(public_launch_authority),
    }
    blockers: list[str] = []
    if not recipient_ready:
        blockers.append("approved_external_encryption_recipient")
    if not bool(aws["caller_identity_verified"]):
        blockers.append("scoped_aws_identity")
    if not s3_target_ready:
        blockers.append("versioned_compliance_locked_s3_target")
    if not restore_target_ready:
        blockers.append("disposable_restore_target_and_toolchain")
    if not all(launch_files.values()):
        blockers.append("signed_public_launch_authority")
    if not billing_ready:
        blockers.append("same_principal_live_billing_authority")
    if not fliplink_ready:
        blockers.append("fliplink_external_publication_authority")

    return {
        "schema": SCHEMA,
        "observed_at": _utc_iso(clock()),
        "status": "ready" if not blockers else "external_authority_required",
        "dr": {
            "backup_runtime": {
                "available": backup["available"],
                "running": backup["running"],
                "healthy": backup["healthy"],
            },
            "approved_recipient": {
                "configured": configured["PROPERTYQUARRY_BACKUP_ENCRYPTION_RECIPIENT"],
                **gpg,
                "ready": recipient_ready,
            },
            "aws": aws,
            "s3_target": {
                "region_configured": bool(configured["AWS_REGION"] or configured["AWS_DEFAULT_REGION"]),
                "bucket_configured": configured["PROPERTYQUARRY_DR_S3_BUCKET"],
                "key_prefix_configured": configured["PROPERTYQUARRY_DR_S3_KEY_PREFIX"],
                "object_lock_days_configured": configured["PROPERTYQUARRY_DR_S3_OBJECT_LOCK_DAYS"],
                "ready": s3_target_ready,
            },
            "restore": {
                "database_target_configured": configured["PROPERTYQUARRY_RESTORE_DATABASE_URL"],
                "destructive_confirmation_configured": configured["PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM"],
                "toolchain": restore_toolchain,
                "ready": restore_target_ready,
            },
        },
        "billing": {
            "api_runtime_available": api["available"],
            "required_field_count": len(BILLING_KEYS),
            "configured_field_count": sum(bool(billing_config.get(name)) for name in BILLING_KEYS),
            "safe_handoff_ready": billing_ready,
            "must_remain_fail_closed": not billing_ready,
        },
        "fliplink": {
            "required_field_count": len(FLIPLINK_KEYS),
            "configured_field_count": sum(bool(fliplink_config.get(name)) for name in FLIPLINK_KEYS),
            "external_publication_ready": fliplink_ready,
            "required_customer_label": "external" if fliplink_ready else "local_only",
        },
        "public_launch": {
            **launch_files,
            "ready": all(launch_files.values()),
        },
        "blockers": blockers,
        "external_authority_only": bool(blockers),
        "secret_values_recorded": False,
    }


def _write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        os.chmod(target, 0o600)
    finally:
        temporary_path.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt = build_live_external_authority_receipt()
    if args.write is not None:
        _write_private_json(args.write, receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
