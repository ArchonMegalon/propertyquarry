#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


EMAILIT_API_BASE = "https://api.emailit.com/v2"


def _root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_env_file() -> Path:
    return _root() / ".env"


def _decode_env_value(raw_value: str) -> str:
    raw = str(raw_value or "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit("emailit_env_value_invalid") from exc
        if not isinstance(value, str):
            raise SystemExit("emailit_env_value_invalid")
        return value
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _decode_env_value(value)
    return values


def _request(*, method: str, url: str, api_key: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"emailit_http_error:{exc.code}:{detail[:1200]}") from exc


def _find_domain(*, api_key: str, domain_name: str) -> dict[str, object] | None:
    payload = _request(method="GET", url=f"{EMAILIT_API_BASE}/domains", api_key=api_key)
    for item in list(payload.get("data") or []):
        candidate = dict(item)
        if str(candidate.get("name") or "").strip().lower() != domain_name.lower():
            continue
        domain_id = str(candidate.get("id") or "").strip()
        if not domain_id:
            raise SystemExit("emailit_domain_id_missing")
        return _request(
            method="GET",
            url=f"{EMAILIT_API_BASE}/domains/{domain_id}",
            api_key=api_key,
        )
    return None


def _ensure_domain(*, api_key: str, domain_name: str) -> dict[str, object]:
    existing = _find_domain(api_key=api_key, domain_name=domain_name)
    if existing is not None:
        return existing
    return _request(
        method="POST",
        url=f"{EMAILIT_API_BASE}/domains",
        api_key=api_key,
        payload={"name": domain_name, "track_loads": False, "track_clicks": False},
    )


def _verify_domain(*, api_key: str, domain_id: str) -> dict[str, object]:
    try:
        return _request(
            method="POST",
            url=f"{EMAILIT_API_BASE}/domains/{domain_id}/verify",
            api_key=api_key,
            payload={},
        )
    except SystemExit as exc:
        if "emailit_http_error:400:" not in str(exc):
            raise
        return _request(
            method="GET",
            url=f"{EMAILIT_API_BASE}/domains/{domain_id}",
            api_key=api_key,
        )


def _dns_ready(domain: dict[str, object]) -> bool:
    required_fields = (
        "spf_status",
        "dkim_status",
        "mx_status",
        "return_path_status",
    )
    optional_fields = ("dmarc_status", "tracking_status", "inbound_status")
    for field in required_fields:
        if str(domain.get(field) or "").strip().lower() != "ok":
            return False
    for field in optional_fields:
        value = str(domain.get(field) or "").strip().lower()
        if value and value != "ok":
            return False
    return True


def _force_fallback(env_values: dict[str, str]) -> bool:
    return str(env_values.get("EA_REGISTRATION_EMAIL_FORCE_FALLBACK") or os.environ.get("EA_REGISTRATION_EMAIL_FORCE_FALLBACK") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _send_probe(
    *,
    api_key: str,
    sender_email: str,
    sender_name: str,
    recipient_email: str,
) -> dict[str, object]:
    return _request(
        method="POST",
        url=f"{EMAILIT_API_BASE}/emails",
        api_key=api_key,
        payload={
            "from": f"{sender_name} <{sender_email}>",
            "to": recipient_email,
            "subject": "PropertyQuarry Emailit probe",
            "text": "This is an automated PropertyQuarry Emailit probe.",
            "html": "",
            "reply_to": sender_email,
            "tracking": False,
            "meta": {"kind": "propertyquarry_emailit_probe"},
        },
    )


def main() -> int:
    if any(arg in {"--help", "-h"} for arg in sys.argv[1:]):
        print("Usage: python3 scripts/bootstrap_emailit_propertyquarry.py [--env-file PATH] [--verify] [--send-test-to EMAIL]")
        print()
    parser = argparse.ArgumentParser(description="Prepare and inspect the PropertyQuarry Emailit sending domain.")
    parser.add_argument("--env-file", default=str(_default_env_file()), help="Env file to inspect.")
    parser.add_argument("--domain", default="propertyquarry.com", help="Sending domain to inspect or create.")
    parser.add_argument("--verify", action="store_true", help="Trigger Emailit DNS verification after loading the domain.")
    parser.add_argument("--send-test-to", default="", help="Optional recipient email for a live send probe.")
    args = parser.parse_args()

    env_values = _parse_env(Path(args.env_file).expanduser().resolve())
    api_key = str(env_values.get("EMAILIT_API_KEY") or os.environ.get("EMAILIT_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("EMAILIT_API_KEY is missing.")

    domain = _ensure_domain(api_key=api_key, domain_name=str(args.domain or "").strip())
    if args.verify and (not str(domain.get("verified_at") or "").strip() or not _dns_ready(domain)):
        domain = _verify_domain(api_key=api_key, domain_id=str(domain.get("id") or "").strip())

    print(f"domain_id={str(domain.get('id') or '').strip()}")
    print(f"name={str(domain.get('name') or '').strip()}")
    print(f"verified_at={str(domain.get('verified_at') or '').strip()}")
    print(f"spf_status={str(domain.get('spf_status') or '').strip()}")
    print(f"dkim_status={str(domain.get('dkim_status') or '').strip()}")
    print(f"mx_status={str(domain.get('mx_status') or '').strip()}")
    print(f"return_path_status={str(domain.get('return_path_status') or '').strip()}")
    print(f"dmarc_status={str(domain.get('dmarc_status') or '').strip()}")
    print(f"tracking_status={str(domain.get('tracking_status') or '').strip()}")
    print(f"inbound_status={str(domain.get('inbound_status') or '').strip()}")
    print("dns_records=")
    for record in list(domain.get("dns_records") or []):
        row = dict(record)
        print(
            json.dumps(
                {
                    "type": row.get("type"),
                    "name": row.get("name"),
                    "value": row.get("value"),
                    "required": row.get("required"),
                    "status": row.get("status"),
                    "priority": row.get("priority"),
                    "error": row.get("error"),
                },
                ensure_ascii=False,
            )
        )
    if str(args.send_test_to or "").strip():
        sender_email = str(env_values.get("EA_REGISTRATION_EMAIL_FROM") or os.environ.get("EA_REGISTRATION_EMAIL_FROM") or "property@propertyquarry.com").strip()
        sender_name = str(env_values.get("EA_REGISTRATION_EMAIL_NAME") or os.environ.get("EA_REGISTRATION_EMAIL_NAME") or "PropertyQuarry").strip()
        if _force_fallback(env_values):
            sender_email = str(env_values.get("EA_REGISTRATION_EMAIL_FROM_FALLBACK") or os.environ.get("EA_REGISTRATION_EMAIL_FROM_FALLBACK") or sender_email).strip()
            sender_name = str(env_values.get("EA_REGISTRATION_EMAIL_NAME_FALLBACK") or os.environ.get("EA_REGISTRATION_EMAIL_NAME_FALLBACK") or sender_name).strip()
        probe = _send_probe(
            api_key=api_key,
            sender_email=sender_email,
            sender_name=sender_name,
            recipient_email=str(args.send_test_to).strip(),
        )
        print(f"send_probe_sender={sender_email}")
        print(f"send_probe_status={str(probe.get('status') or '').strip()}")
        print(f"send_probe_id={str(probe.get('id') or '').strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
