#!/usr/bin/env python3
"""Materialize a secret-safe receipt for PropertyQuarry's live external gates."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
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
DATABASE_CONTAINER = "propertyquarry-db-live"
EA_API_CONTAINER = "ea-api"
GLOBAL_TRUST_STORE = Path(
    "/etc/propertyquarry/release-control/global-governance-trust-store.v1.json"
)
PUBLIC_LAUNCH_AUTHORITY = Path(
    "/run/propertyquarry/release-control/propertyquarry-public-launch-authority.v2.json"
)
POSTGRES_CLIENT_RELEASE_PIN = (
    Path(__file__).resolve().parents[1]
    / "config/propertyquarry/postgres_client_release_pin.json"
)
POSTGRES_CLIENT_RELEASE_PIN_SCHEMA = (
    "propertyquarry.postgres_client_release_pin.v1"
)
POSTGRES_CLIENT_BINARIES = ("pg_dump", "pg_restore", "psql")

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
EA_BILLING_CANDIDATE_KEYS = (
    "PAYPAL_CLIENT_ID",
    "PAYPAL_SECRET",
    "PAYPAL_ACCOUNT_EMAIL",
    "PAYFUNNELS_API_KEY",
    "PAYFUNNELS_WEBHOOK_SECRET",
    "PAYFUNNELS_PLUS_CHECKOUT_URL",
    "PAYFUNNELS_AGENT_CHECKOUT_URL",
)
EA_FLIPLINK_CANDIDATE_KEYS = (
    "EA_MEMORIAL_FLIPLINK_WEBHOOK_SECRET",
    *FLIPLINK_KEYS,
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


def _paypal_external_candidate_failure(
    state: str,
    *,
    attempted: bool,
) -> dict[str, object]:
    return {
        "attempted": attempted,
        "state": state,
        "api_environment": "",
        "credential_environment": "",
        "classification_probe_attempted": False,
        "token_http_status": 0,
        "classified_token_http_status": 0,
        "access_token_verified": False,
        "webhook_list_http_status": 0,
        "webhook_count": 0,
        "propertyquarry_principal_authorized": False,
        "billing_enabled": False,
        "secret_values_recorded": False,
    }


def _paypal_external_candidate_projection(*, runner: Runner) -> dict[str, object]:
    source = r'''
import json
import os
import requests

client_id = str(os.getenv("PAYPAL_CLIENT_ID") or "").strip()
secret = str(os.getenv("PAYPAL_SECRET") or "").strip()
api_base = str(os.getenv("PAYPAL_API_BASE") or "https://api-m.paypal.com").strip().rstrip("/")
allowed = {
    "https://api-m.paypal.com": "live",
    "https://api-m.sandbox.paypal.com": "sandbox",
}
payload = {
    "api_environment": allowed.get(api_base, "invalid"),
    "credential_environment": "",
    "classification_probe_attempted": False,
    "token_http_status": 0,
    "classified_token_http_status": 0,
    "access_token_verified": False,
    "webhook_list_http_status": 0,
    "webhook_count": 0,
}
if not client_id or not secret:
    payload["state"] = "credentials_not_configured"
elif api_base not in allowed:
    payload["state"] = "api_base_invalid"
else:
    def token_probe(base):
        response = requests.post(
            f"{base}/v1/oauth2/token",
            auth=(client_id, secret),
            headers={"Accept": "application/json", "Accept-Language": "en_US"},
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        body = response.json() if response.status_code < 400 else {}
        token = str(body.get("access_token") or "") if isinstance(body, dict) else ""
        return response, token

    response, token = token_probe(api_base)
    payload["token_http_status"] = int(response.status_code)
    payload["classified_token_http_status"] = int(response.status_code)
    active_base = api_base
    if response.status_code in {401, 403} and allowed[api_base] == "live":
        payload["classification_probe_attempted"] = True
        sandbox_base = "https://api-m.sandbox.paypal.com"
        classified, classified_token = token_probe(sandbox_base)
        payload["classified_token_http_status"] = int(classified.status_code)
        if classified_token:
            token = classified_token
            active_base = sandbox_base
    payload["access_token_verified"] = bool(token)
    if token:
        payload["credential_environment"] = allowed[active_base]
        webhooks = requests.get(
            f"{active_base}/v1/notifications/webhooks",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30,
        )
        payload["webhook_list_http_status"] = int(webhooks.status_code)
        webhook_payload = webhooks.json() if webhooks.status_code < 400 else {}
        rows = webhook_payload.get("webhooks") if isinstance(webhook_payload, dict) else []
        payload["webhook_count"] = len(rows) if isinstance(rows, list) else 0
        if webhooks.status_code >= 400:
            payload["state"] = "webhook_probe_failed"
        elif allowed[active_base] == "sandbox":
            payload["state"] = "sandbox_credentials_verified"
        else:
            payload["state"] = "verified"
    elif response.status_code in {401, 403}:
        payload["state"] = "authentication_rejected"
    elif response.status_code >= 400:
        payload["state"] = "token_probe_failed"
    else:
        payload["state"] = "token_probe_failed"
print(json.dumps(payload, sort_keys=True))
'''
    try:
        result = runner(
            ["docker", "exec", "-i", EA_API_CONTAINER, "python", "-"],
            input=source,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        if int(getattr(result, "returncode", 1)) != 0:
            return _paypal_external_candidate_failure(
                "runtime_probe_failed",
                attempted=True,
            )
        payload = json.loads(str(getattr(result, "stdout", "") or ""))
        if not isinstance(payload, dict) or set(payload) != {
            "access_token_verified",
            "api_environment",
            "classification_probe_attempted",
            "classified_token_http_status",
            "credential_environment",
            "state",
            "token_http_status",
            "webhook_count",
            "webhook_list_http_status",
        }:
            return _paypal_external_candidate_failure(
                "runtime_probe_invalid",
                attempted=True,
            )
        state = str(payload.get("state") or "")
        if state not in {
            "api_base_invalid",
            "authentication_rejected",
            "credentials_not_configured",
            "sandbox_credentials_verified",
            "token_probe_failed",
            "verified",
            "webhook_probe_failed",
        }:
            return _paypal_external_candidate_failure(
                "runtime_probe_invalid",
                attempted=True,
            )
        return {
            "attempted": True,
            "state": state,
            "api_environment": str(payload.get("api_environment") or ""),
            "credential_environment": str(
                payload.get("credential_environment") or ""
            ),
            "classification_probe_attempted": bool(
                payload.get("classification_probe_attempted")
            ),
            "token_http_status": int(payload.get("token_http_status") or 0),
            "classified_token_http_status": int(
                payload.get("classified_token_http_status") or 0
            ),
            "access_token_verified": bool(payload.get("access_token_verified")),
            "webhook_list_http_status": int(
                payload.get("webhook_list_http_status") or 0
            ),
            "webhook_count": int(payload.get("webhook_count") or 0),
            "propertyquarry_principal_authorized": False,
            "billing_enabled": False,
            "secret_values_recorded": False,
        }
    except (
        OSError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return _paypal_external_candidate_failure(
            "runtime_probe_failed",
            attempted=True,
        )


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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _postgres_client_release_pin_projection(
    path: Path,
    *,
    runner: Runner,
) -> dict[str, object]:
    failure = {
        "release_pin_attested": False,
        "version": "",
        "package_sha256": "",
        "binaries": {name: False for name in POSTGRES_CLIENT_BINARIES},
        "ready": False,
    }
    try:
        before = path.lstat()
        resolved = path.resolve(strict=True)
        if (
            resolved != path
            or stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.geteuid()}
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_nlink != 1
            or before.st_size <= 0
            or before.st_size > 64 * 1024
        ):
            return {**failure, "state": "release_pin_untrusted"}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {
            "binaries",
            "package",
            "schema",
            "status",
            "version",
        }:
            return {**failure, "state": "release_pin_schema_invalid"}
        if (
            payload.get("schema") != POSTGRES_CLIENT_RELEASE_PIN_SCHEMA
            or payload.get("status") != "CONFIGURED"
        ):
            return {**failure, "state": "release_pin_unconfigured"}
        version = str(payload.get("version") or "").strip()
        if not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", version):
            return {**failure, "state": "release_pin_version_invalid"}
        package = payload.get("package")
        if not isinstance(package, dict) or set(package) != {
            "name",
            "sha256",
            "version",
        }:
            return {**failure, "state": "release_pin_package_invalid"}
        package_sha256 = str(package.get("sha256") or "").strip().lower()
        if (
            package.get("name") != "postgresql-client-16"
            or not str(package.get("version") or "").startswith(f"{version}-")
            or not re.fullmatch(r"[0-9a-f]{64}", package_sha256)
        ):
            return {**failure, "state": "release_pin_package_invalid"}
        binaries = payload.get("binaries")
        if not isinstance(binaries, dict) or set(binaries) != set(
            POSTGRES_CLIENT_BINARIES
        ):
            return {**failure, "state": "release_pin_binaries_invalid"}
        projected: dict[str, bool] = {}
        for name in POSTGRES_CLIENT_BINARIES:
            row = binaries.get(name)
            if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
                return {**failure, "state": "release_pin_binary_invalid"}
            binary = Path(str(row.get("path") or ""))
            expected_sha256 = str(row.get("sha256") or "").strip().lower()
            observed = binary.lstat()
            if (
                not binary.is_absolute()
                or binary.resolve(strict=True) != binary
                or stat.S_ISLNK(observed.st_mode)
                or not stat.S_ISREG(observed.st_mode)
                or observed.st_uid not in {0, os.geteuid()}
                or observed.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                or not observed.st_mode & stat.S_IXUSR
                or observed.st_nlink != 1
                or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                or _sha256(binary) != expected_sha256
            ):
                return {**failure, "state": f"{name}_attestation_failed"}
            result = runner(
                [str(binary), "--version"],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            if (
                int(getattr(result, "returncode", 1)) != 0
                or f"(PostgreSQL) {version}" not in str(
                    getattr(result, "stdout", "") or ""
                )
            ):
                return {**failure, "state": f"{name}_version_failed"}
            projected[name] = True
        return {
            "release_pin_attested": True,
            "version": version,
            "package_sha256": package_sha256,
            "binaries": projected,
            "ready": all(projected.values()),
            "state": "attested",
        }
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {**failure, "state": "release_pin_unavailable"}


def _postgres_client_binary_paths(path: Path) -> dict[str, Path]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    binaries = dict(payload["binaries"])
    return {
        name: Path(str(dict(binaries[name])["path"]))
        for name in POSTGRES_CLIENT_BINARIES
    }


def _postgres_live_call_failure(state: str, *, attempted: bool) -> dict[str, object]:
    return {
        "attempted": attempted,
        "state": state,
        "live_database_readback": False,
        "server_version_num": "",
        "full_custom_archive_created": False,
        "archive_entries": 0,
        "archive_bytes": 0,
        "archive_sha256": "",
        "archive_list_validated": False,
        "plaintext_archive_retained": False,
        "passing_off_host_dr_claim": False,
        "secret_values_recorded": False,
    }


def _postgres_live_client_call_projection(
    release_pin: Path,
    *,
    runner: Runner,
) -> dict[str, object]:
    attestation = _postgres_client_release_pin_projection(
        release_pin,
        runner=runner,
    )
    if not bool(attestation["ready"]):
        return _postgres_live_call_failure("toolchain_not_attested", attempted=False)
    try:
        inspected = runner(
            ["docker", "container", "inspect", DATABASE_CONTAINER],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        if int(getattr(inspected, "returncode", 1)) != 0:
            return _postgres_live_call_failure(
                "database_runtime_unavailable",
                attempted=False,
            )
        payload = json.loads(str(getattr(inspected, "stdout", "") or ""))
        row = payload[0]
        raw_env = list((row.get("Config") or {}).get("Env") or [])
        database_env: dict[str, str] = {}
        for entry in raw_env:
            key, separator, value = str(entry).partition("=")
            if separator and key in {"POSTGRES_DB", "POSTGRES_PASSWORD", "POSTGRES_USER"}:
                database_env[key] = value
        database = str(database_env.get("POSTGRES_DB") or "").strip()
        password = str(database_env.get("POSTGRES_PASSWORD") or "")
        user = str(database_env.get("POSTGRES_USER") or "postgres").strip()
        networks = dict((row.get("NetworkSettings") or {}).get("Networks") or {})
        addresses = [
            str(dict(network).get("IPAddress") or "").strip()
            for _name, network in sorted(networks.items())
        ]
        host = next((address for address in addresses if address), "")
        if not database or not password or not user or not host:
            return _postgres_live_call_failure(
                "database_runtime_contract_incomplete",
                attempted=False,
            )
        binaries = _postgres_client_binary_paths(release_pin)
        process_env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "PGCONNECT_TIMEOUT": "10",
            "PGHOST": host,
            "PGPORT": "5432",
            "PGUSER": user,
            "PGPASSWORD": password,
            "PGDATABASE": database,
            "PGPASSFILE": "/dev/null",
            "PGSERVICEFILE": "/dev/null",
        }
        with tempfile.TemporaryDirectory(prefix="propertyquarry-pg-client-probe-") as root:
            archive = Path(root) / "live.dump"
            readback = runner(
                [
                    str(binaries["psql"]),
                    "-X",
                    "-A",
                    "-t",
                    "-q",
                    "-c",
                    "SELECT 1, current_setting('server_version_num')",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
                env=process_env,
            )
            match = re.fullmatch(
                r"1\|([0-9]+)\s*",
                str(getattr(readback, "stdout", "") or ""),
            )
            if int(getattr(readback, "returncode", 1)) != 0 or match is None:
                return _postgres_live_call_failure("database_readback_failed", attempted=True)
            server_version_num = match.group(1)
            if not server_version_num.startswith(
                str(attestation["version"]).split(".", 1)[0]
            ):
                return _postgres_live_call_failure("database_version_mismatch", attempted=True)
            dumped = runner(
                [
                    str(binaries["pg_dump"]),
                    "--format=custom",
                    "--compress=6",
                    "--no-owner",
                    "--no-privileges",
                    f"--file={archive}",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3600,
                env=process_env,
            )
            if int(getattr(dumped, "returncode", 1)) != 0:
                return _postgres_live_call_failure("database_dump_failed", attempted=True)
            observed = archive.lstat()
            if (
                stat.S_ISLNK(observed.st_mode)
                or not stat.S_ISREG(observed.st_mode)
                or observed.st_uid != os.geteuid()
                or observed.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
                or observed.st_nlink != 1
                or observed.st_size <= 0
            ):
                return _postgres_live_call_failure("database_dump_untrusted", attempted=True)
            listed = runner(
                [str(binaries["pg_restore"]), "--list", str(archive)],
                check=False,
                capture_output=True,
                text=True,
                timeout=120,
                env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            )
            entries = sum(
                1
                for line in str(getattr(listed, "stdout", "") or "").splitlines()
                if re.match(r"^[0-9]+;", line)
            )
            if int(getattr(listed, "returncode", 1)) != 0 or entries <= 0:
                return _postgres_live_call_failure("archive_list_failed", attempted=True)
            return {
                "attempted": True,
                "state": "verified",
                "live_database_readback": True,
                "server_version_num": server_version_num,
                "full_custom_archive_created": True,
                "archive_entries": entries,
                "archive_bytes": observed.st_size,
                "archive_sha256": _sha256(archive),
                "archive_list_validated": True,
                "plaintext_archive_retained": False,
                "passing_off_host_dr_claim": False,
                "secret_values_recorded": False,
            }
    except (
        FileNotFoundError,
        IndexError,
        KeyError,
        OSError,
        subprocess.SubprocessError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ):
        return _postgres_live_call_failure("live_probe_failed", attempted=True)


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
    postgres_client_release_pin: Path = POSTGRES_CLIENT_RELEASE_PIN,
    probe_live_postgres: bool = False,
    probe_external_billing: bool = False,
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
    ea_api = _container_projection(
        EA_API_CONTAINER,
        allowlisted_names=(*EA_BILLING_CANDIDATE_KEYS, *EA_FLIPLINK_CANDIDATE_KEYS),
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
    postgres_client = _postgres_client_release_pin_projection(
        postgres_client_release_pin,
        runner=runner,
    )
    pinned_postgres_tools = dict(postgres_client["binaries"])
    postgres_live_call = (
        _postgres_live_client_call_projection(
            postgres_client_release_pin,
            runner=runner,
        )
        if probe_live_postgres
        else _postgres_live_call_failure("not_requested", attempted=False)
    )
    restore_toolchain = {
        "gpg": bool(which("gpg")),
        **{
            name: bool(pinned_postgres_tools[name])
            for name in POSTGRES_CLIENT_BINARIES
        },
    }
    restore_toolchain_ready = all(restore_toolchain.values())
    restore_target_configured = bool(
        configured["PROPERTYQUARRY_RESTORE_DATABASE_URL"]
        and configured["PROPERTYQUARRY_RESTORE_DISPOSABLE_CONFIRM"]
    )
    restore_target_ready = bool(
        restore_target_configured and restore_toolchain_ready
    )
    billing_config = dict(api["configured"])
    billing_ready = all(billing_config.get(name, False) for name in BILLING_KEYS)
    fliplink_config = dict(api["configured"])
    fliplink_ready = all(fliplink_config.get(name, False) for name in FLIPLINK_KEYS)
    ea_candidate_config = dict(ea_api["configured"])
    paypal_external_candidate = (
        _paypal_external_candidate_projection(runner=runner)
        if probe_external_billing
        else _paypal_external_candidate_failure("not_requested", attempted=False)
    )
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
    if not restore_toolchain_ready:
        blockers.append("local_postgres_restore_toolchain")
    if not restore_target_configured:
        blockers.append("disposable_restore_target")
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
                "postgres_client": postgres_client,
                "live_client_call": postgres_live_call,
                "toolchain_ready": restore_toolchain_ready,
                "target_configured": restore_target_configured,
                "ready": restore_target_ready,
            },
        },
        "billing": {
            "api_runtime_available": api["available"],
            "required_field_count": len(BILLING_KEYS),
            "configured_field_count": sum(bool(billing_config.get(name)) for name in BILLING_KEYS),
            "safe_handoff_ready": billing_ready,
            "must_remain_fail_closed": not billing_ready,
            "external_candidate": {
                "source_runtime_available": ea_api["available"],
                "paypal": {
                    "client_credentials_configured": bool(
                        ea_candidate_config.get("PAYPAL_CLIENT_ID")
                        and ea_candidate_config.get("PAYPAL_SECRET")
                    ),
                    "account_identity_configured": bool(
                        ea_candidate_config.get("PAYPAL_ACCOUNT_EMAIL")
                    ),
                    **paypal_external_candidate,
                },
                "payfunnels": {
                    "api_key_configured": bool(
                        ea_candidate_config.get("PAYFUNNELS_API_KEY")
                    ),
                    "webhook_secret_configured": bool(
                        ea_candidate_config.get("PAYFUNNELS_WEBHOOK_SECRET")
                    ),
                    "plus_checkout_configured": bool(
                        ea_candidate_config.get("PAYFUNNELS_PLUS_CHECKOUT_URL")
                    ),
                    "agent_checkout_configured": bool(
                        ea_candidate_config.get("PAYFUNNELS_AGENT_CHECKOUT_URL")
                    ),
                    "propertyquarry_principal_authorized": False,
                    "billing_enabled": False,
                },
                "cross_principal_reuse_forbidden": True,
            },
        },
        "fliplink": {
            "required_field_count": len(FLIPLINK_KEYS),
            "configured_field_count": sum(bool(fliplink_config.get(name)) for name in FLIPLINK_KEYS),
            "external_publication_ready": fliplink_ready,
            "required_customer_label": "external" if fliplink_ready else "local_only",
            "external_candidate": {
                "source_runtime_available": ea_api["available"],
                "memorial_webhook_secret_configured": bool(
                    ea_candidate_config.get("EA_MEMORIAL_FLIPLINK_WEBHOOK_SECRET")
                ),
                "propertyquarry_credential_count": sum(
                    bool(ea_candidate_config.get(name)) for name in FLIPLINK_KEYS
                ),
                "propertyquarry_principal_authorized": False,
                "cross_principal_reuse_forbidden": True,
            },
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
    parser.add_argument("--probe-live-postgres", action="store_true")
    parser.add_argument("--probe-external-billing", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    receipt = build_live_external_authority_receipt(
        probe_live_postgres=bool(args.probe_live_postgres),
        probe_external_billing=bool(args.probe_external_billing),
    )
    if args.write is not None:
        _write_private_json(args.write, receipt)
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
