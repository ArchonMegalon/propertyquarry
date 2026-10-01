#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
for candidate in (ROOT, EA_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from app.services.brilliant_directories import build_brilliant_directories_verification_receipt  # noqa: E402
from scripts.propertyquarry_billing_handoff_probe import (  # noqa: E402
    billing_admin_login_attempt,
    billing_admin_login_surface_probe,
)


def _billing_dns_handoff_markdown(payload: dict[str, object]) -> str:
    handoff = payload.get("billing_handoff") if isinstance(payload.get("billing_handoff"), dict) else {}
    admin_probe = payload.get("admin_login_probe") if isinstance(payload.get("admin_login_probe"), dict) else {}
    record = handoff.get("required_dns_record") if isinstance(handoff.get("required_dns_record"), dict) else {}
    host = str(handoff.get("host") or record.get("name") or "billing.propertyquarry.com").strip()
    url = str(handoff.get("url") or "").strip()
    record_type = str(record.get("type") or "CNAME or A/AAAA").strip()
    target = str(record.get("target") or "the Brilliant Directories white-label billing host assigned to this account").strip()
    next_action = str(handoff.get("next_action") or f"create DNS for {host} before enabling the Brilliant Directories billing handoff").strip()
    configured = handoff.get("configured") is True
    ready = configured and handoff.get("host_resolves") is True and bool(url)
    host_resolves = "yes" if handoff.get("host_resolves") is True else "no"
    status_line = (
        "Brilliant Directories billing handoff DNS is ready for the configured white-label host."
        if ready
        else "Gold remains blocked until the Brilliant Directories billing handoff host resolves."
    )
    operator_line = (
        "Keep `/app/billing` pointed at the resolving HTTPS white-label billing handoff."
        if ready
        else "Do not enable `/app/billing` as an external redirect until this host resolves over HTTPS."
    )
    admin_lines: list[str] = []
    if admin_probe:
        admin_lines.extend(
            [
                "",
                "## Admin Repair Lane",
                "",
                f"- Admin login URL: `{str(admin_probe.get('login_url') or 'https://propertyquarry.directoryup.com/admin/login').strip()}`",
                f"- Admin login form reachable: `{'yes' if admin_probe.get('surface_ok') else 'no'}`",
                f"- Admin login reCAPTCHA required: `{'yes' if admin_probe.get('recaptcha_required') else 'no'}`",
                f"- Recovery URL: `{str(admin_probe.get('recovery_href') or 'not detected').strip()}`",
                f"- Local shared-credential attempt: `{str(admin_probe.get('shared_credential_status') or 'not_attempted').strip()}`",
                f"- Admin repair next action: {str(admin_probe.get('next_action') or 'seed the correct Brilliant Directories admin username/password or use the recovery link before changing member-login settings').strip()}",
                "",
            ]
        )
    return "\n".join(
        [
            "# PropertyQuarry Billing DNS Handoff",
            "",
            status_line,
            "",
            f"- Host: `{host}`",
            f"- URL: `{url or 'not configured'}`",
            f"- Resolves now: `{host_resolves}`",
            f"- Required DNS record type: `{record_type}`",
            f"- Required DNS target: `{target}`",
            f"- Next action: {next_action}",
            "",
            operator_line,
            "PropertyQuarry must remain the source of truth for entitlements; Brilliant Directories billing events stay advisory until reconciled.",
            *admin_lines,
        ]
    )


def _admin_login_probe() -> dict[str, object]:
    login_url = "https://propertyquarry.directoryup.com/admin/login"
    surface = billing_admin_login_surface_probe(login_url)
    shared_username = str(os.getenv("BROWSERACT_USERNAME") or "").strip()
    shared_password = str(os.getenv("BROWSERACT_PASSWORD") or "").strip()
    attempt: dict[str, object] = {}
    if shared_username and shared_password:
        attempt = billing_admin_login_attempt(
            username=shared_username,
            password=shared_password,
            login_url=login_url,
        )
    shared_status = "not_attempted"
    next_action = "seed the correct Brilliant Directories admin username/password or use the recovery link before changing member-login settings"
    if attempt:
        if attempt.get("authenticated") is True:
            shared_status = "authenticated"
            next_action = "disable member-login reCAPTCHA or configure live reCAPTCHA keys for the billing domain from the admin backend"
        else:
            shared_status = str(attempt.get("error") or "failed").strip()
            if attempt.get("recovery_href"):
                next_action = (
                    "the local shared account did not authenticate; use the backend recovery link or seed the correct "
                    "Brilliant Directories admin username/password before changing member-login settings"
                )
    return {
        "login_url": login_url,
        "surface_ok": bool(surface.get("ok")),
        "status_code": int(surface.get("status_code") or 0),
        "form_action": str(surface.get("form_action") or "").strip(),
        "recaptcha_required": bool(surface.get("recaptcha_required")),
        "recovery_href": str(surface.get("recovery_href") or "").strip(),
        "shared_credential_attempted": bool(attempt),
        "shared_credential_status": shared_status,
        "shared_credential_final_url": str(attempt.get("final_url") or "").strip(),
        "next_action": next_action,
    }


def _load_dotenv_defaults(path: Path) -> None:
    if os.getenv("PROPERTYQUARRY_SKIP_DOTENV"):
        return
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def main() -> int:
    _load_dotenv_defaults(ROOT / ".env")
    out_dir = Path(
        os.getenv("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_COMPLETION_DIR")
        or ROOT / "_completion" / "brilliant_directories"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "BRILLIANT_DIRECTORIES_PROVIDER_VERIFICATION.generated.json"
    payload = build_brilliant_directories_verification_receipt(verify_bridge_exchange=True)
    payload["admin_login_probe"] = _admin_login_probe()
    out_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "BRILLIANT_DIRECTORIES_BILLING_DNS_HANDOFF.md").write_text(
        _billing_dns_handoff_markdown(payload),
        encoding="utf-8",
    )
    print(out_path)
    return 0 if payload.get("status") in {"disabled", "dry_verified_configured"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
