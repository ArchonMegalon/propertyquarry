#!/usr/bin/env python3
"""Materialize the narrow PropertyQuarry browser-auth environment.

The source EA environment contains credentials for several unrelated services.
This provisioner copies only the Emailit and Google OAuth values required by
PropertyQuarry's existing sign-in routes, generates PropertyQuarry-specific
state/encryption and release-probe secrets, and writes an atomic mode-0600 env
file. The release probe is constrained to one origin, one principal, and the
reviewed read-only customer routes enforced by the application middleware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from typing import Mapping


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from app.propertyquarry_release_probe import (  # noqa: E402
    normalized_propertyquarry_release_probe_origin,
    propertyquarry_release_probe_research_detail_route_valid,
    propertyquarry_release_probe_shortlist_run_path_valid,
)


PROPERTYQUARRY_GOOGLE_REDIRECT_URI = "https://propertyquarry.com/google/callback"
PROPERTYQUARRY_RELEASE_PROBE_DEFAULTS = {
    "PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID": "propertyquarry-release-probe",
    "PROPERTYQUARRY_RELEASE_PROBE_ORIGIN": "https://propertyquarry.com",
    "PROPERTYQUARRY_RELEASE_PROBE_RESEARCH_DETAIL_ROUTE": (
        "/app/research/perf-candidate-1020?run_id=run-gold-mobile"
    ),
    "PROPERTYQUARRY_RELEASE_PROBE_SHORTLIST_RUN_PATH": (
        "/app/shortlist/run/0a89ead9e0b048288cca22d1aac54fa7"
    ),
}

_COPIED_KEYS = (
    "EMAILIT_API_KEY",
    "EA_EMAIL_DEFAULT_FROM",
    "EA_EMAIL_DEFAULT_NAME",
    "EA_REGISTRATION_EMAIL_FROM",
    "EA_REGISTRATION_EMAIL_NAME",
    "EA_REGISTRATION_EMAIL_FROM_FALLBACK",
    "EA_REGISTRATION_EMAIL_NAME_FALLBACK",
    "EA_REGISTRATION_EMAIL_FORCE_FALLBACK",
    "EA_GOOGLE_OAUTH_CLIENT_ID",
    "EA_GOOGLE_OAUTH_CLIENT_SECRET",
)
_REQUIRED_KEYS = (
    "EMAILIT_API_KEY",
    "EA_GOOGLE_OAUTH_CLIENT_ID",
    "EA_GOOGLE_OAUTH_CLIENT_SECRET",
)
_GENERATED_SECRET_KEYS = (
    "EA_GOOGLE_OAUTH_STATE_SECRET",
    "EA_PROVIDER_SECRET_KEY",
    "PROPERTYQUARRY_RELEASE_PROBE_SECRET",
)
_RELEASE_PROBE_CONFIG_KEYS = tuple(PROPERTYQUARRY_RELEASE_PROBE_DEFAULTS)
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SAFE_ENV_VALUE_RE = re.compile(r"[A-Za-z0-9_./:@%+,=-]*")
_PRINCIPAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,199}")


class AuthEnvProvisionError(RuntimeError):
    """Raised when a secure auth environment cannot be materialized."""


def _decode_env_value(raw_value: str) -> str:
    raw = str(raw_value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AuthEnvProvisionError("source_env_double_quote_invalid") from exc
        if not isinstance(value, str):
            raise AuthEnvProvisionError("source_env_value_invalid")
        return value
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw


def parse_env_file(path: Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        raise AuthEnvProvisionError("source_env_regular_file_required")
    values: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise AuthEnvProvisionError(f"source_env_assignment_invalid:{line_number}")
        key, raw_value = stripped.split("=", 1)
        normalized_key = key.strip()
        if not _ENV_KEY_RE.fullmatch(normalized_key):
            raise AuthEnvProvisionError(f"source_env_key_invalid:{line_number}")
        value = _decode_env_value(raw_value)
        if "\x00" in value or "\n" in value or "\r" in value:
            raise AuthEnvProvisionError(f"source_env_value_multiline:{normalized_key}")
        values[normalized_key] = value
    return values


def _encode_env_value(value: str) -> str:
    normalized = str(value or "")
    if "\x00" in normalized or "\n" in normalized or "\r" in normalized:
        raise AuthEnvProvisionError("output_env_value_multiline")
    if _SAFE_ENV_VALUE_RE.fullmatch(normalized):
        return normalized
    return json.dumps(normalized, ensure_ascii=True)


def _sender_domain(values: Mapping[str, str]) -> str:
    sender = (
        str(
            values.get("EA_REGISTRATION_EMAIL_FROM")
            or values.get("EA_EMAIL_DEFAULT_FROM")
            or ""
        )
        .strip()
        .lower()
    )
    if "@" not in sender:
        raise AuthEnvProvisionError("propertyquarry_sender_email_required")
    domain = sender.rsplit("@", 1)[-1].strip().rstrip(".")
    if domain != "propertyquarry.com" and not domain.endswith(".propertyquarry.com"):
        raise AuthEnvProvisionError("propertyquarry_sender_domain_required")
    return domain


def _usable_secret(value: object) -> str:
    normalized = str(value or "").strip()
    return normalized if len(normalized) >= 32 else ""


def _release_probe_configuration(source_values: Mapping[str, str]) -> dict[str, str]:
    configured = {
        key: str(source_values.get(key) or default).strip()
        for key, default in PROPERTYQUARRY_RELEASE_PROBE_DEFAULTS.items()
    }
    principal_id = configured["PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID"]
    if not _PRINCIPAL_ID_RE.fullmatch(principal_id):
        raise AuthEnvProvisionError("propertyquarry_release_probe_principal_invalid")
    try:
        configured["PROPERTYQUARRY_RELEASE_PROBE_ORIGIN"] = (
            normalized_propertyquarry_release_probe_origin(
                configured["PROPERTYQUARRY_RELEASE_PROBE_ORIGIN"]
            )
        )
    except ValueError as exc:
        raise AuthEnvProvisionError(
            "propertyquarry_release_probe_origin_invalid"
        ) from exc
    if not propertyquarry_release_probe_research_detail_route_valid(
        configured["PROPERTYQUARRY_RELEASE_PROBE_RESEARCH_DETAIL_ROUTE"]
    ):
        raise AuthEnvProvisionError(
            "propertyquarry_release_probe_research_detail_route_invalid"
        )
    if not propertyquarry_release_probe_shortlist_run_path_valid(
        configured["PROPERTYQUARRY_RELEASE_PROBE_SHORTLIST_RUN_PATH"]
    ):
        raise AuthEnvProvisionError(
            "propertyquarry_release_probe_shortlist_run_path_invalid"
        )
    return configured


def _dedicated_secret(
    existing_value: object,
    *,
    prohibited_values: set[str],
) -> str:
    existing = _usable_secret(existing_value)
    if existing and existing not in prohibited_values:
        return existing
    while True:
        generated = secrets.token_urlsafe(48)
        if generated not in prohibited_values:
            return generated


def build_auth_environment(
    source_values: Mapping[str, str],
    *,
    existing_values: Mapping[str, str] | None = None,
) -> dict[str, str]:
    missing = [
        key for key in _REQUIRED_KEYS if not str(source_values.get(key) or "").strip()
    ]
    if missing:
        raise AuthEnvProvisionError("source_auth_keys_missing:" + ",".join(missing))
    _sender_domain(source_values)

    result = {
        key: str(source_values.get(key) or "").strip()
        for key in _COPIED_KEYS
        if str(source_values.get(key) or "").strip()
    }
    result["EA_GOOGLE_OAUTH_REDIRECT_URI"] = PROPERTYQUARRY_GOOGLE_REDIRECT_URI
    result.update(_release_probe_configuration(source_values))
    existing = dict(existing_values or {})
    prohibited_secrets = {
        value
        for value in (
            str(raw_value or "").strip() for raw_value in source_values.values()
        )
        if _usable_secret(value)
    }
    for key in _GENERATED_SECRET_KEYS:
        result[key] = _dedicated_secret(
            existing.get(key),
            prohibited_values=prohibited_secrets,
        )
        prohibited_secrets.add(result[key])
    return result


def _atomic_write(path: Path, body: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AuthEnvProvisionError("output_path_symlink_rejected")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, mode)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def provision_auth_environment(
    *,
    source_env: Path,
    output_env: Path,
    receipt_path: Path,
) -> dict[str, object]:
    source_resolved = source_env.resolve(strict=False)
    output_resolved = output_env.resolve(strict=False)
    receipt_resolved = receipt_path.resolve(strict=False)
    if len({source_resolved, output_resolved, receipt_resolved}) != 3:
        raise AuthEnvProvisionError("auth_env_paths_must_be_distinct")
    source_values = parse_env_file(source_env)
    if output_env.is_symlink():
        raise AuthEnvProvisionError("output_path_symlink_rejected")
    existing_values = parse_env_file(output_env) if output_env.exists() else {}
    values = build_auth_environment(source_values, existing_values=existing_values)
    ordered_keys = tuple(
        key
        for key in (
            *_COPIED_KEYS,
            "EA_GOOGLE_OAUTH_REDIRECT_URI",
            *_RELEASE_PROBE_CONFIG_KEYS,
            *_GENERATED_SECRET_KEYS,
        )
        if key in values
    )
    body = "".join(f"{key}={_encode_env_value(values[key])}\n" for key in ordered_keys)
    _atomic_write(output_env, body)

    receipt: dict[str, object] = {
        "contract_name": "propertyquarry.runtime_auth_environment.v1",
        "status": "ready",
        "output_env": str(output_env),
        "output_mode": "0600",
        "configured_keys": list(ordered_keys),
        "sender_domain": _sender_domain(values),
        "google_redirect_uri": PROPERTYQUARRY_GOOGLE_REDIRECT_URI,
        "release_probe_configured": True,
        "release_probe_origin": values["PROPERTYQUARRY_RELEASE_PROBE_ORIGIN"],
        "release_probe_principal_id": values[
            "PROPERTYQUARRY_RELEASE_PROBE_PRINCIPAL_ID"
        ],
        "release_probe_route_values_redacted": True,
        "release_probe_research_detail_route_sha256": hashlib.sha256(
            values[
                "PROPERTYQUARRY_RELEASE_PROBE_RESEARCH_DETAIL_ROUTE"
            ].encode("utf-8")
        ).hexdigest(),
        "release_probe_shortlist_run_path_sha256": hashlib.sha256(
            values[
                "PROPERTYQUARRY_RELEASE_PROBE_SHORTLIST_RUN_PATH"
            ].encode("utf-8")
        ).hexdigest(),
        "emailit_key_fingerprint": hashlib.sha256(
            values["EMAILIT_API_KEY"].encode("utf-8")
        ).hexdigest()[:16],
        "google_client_fingerprint": hashlib.sha256(
            values["EA_GOOGLE_OAUTH_CLIENT_ID"].encode("utf-8")
        ).hexdigest()[:16],
        "dedicated_state_secret": values["EA_GOOGLE_OAUTH_STATE_SECRET"]
        != str(source_values.get("EA_GOOGLE_OAUTH_STATE_SECRET") or "").strip(),
        "dedicated_provider_secret": values["EA_PROVIDER_SECRET_KEY"]
        != str(source_values.get("EA_PROVIDER_SECRET_KEY") or "").strip(),
        "dedicated_release_probe_secret": values[
            "PROPERTYQUARRY_RELEASE_PROBE_SECRET"
        ]
        != str(source_values.get("PROPERTYQUARRY_RELEASE_PROBE_SECRET") or "").strip(),
        "unrelated_source_keys_copied": False,
    }
    _atomic_write(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-env", type=Path, required=True)
    parser.add_argument("--output-env", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    receipt = provision_auth_environment(
        source_env=args.source_env,
        output_env=args.output_env,
        receipt_path=args.receipt,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
