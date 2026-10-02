#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


ALLOWED_COUNTRIES = ("AT", "DE", "CR")
BLOCKED_COUNTRIES = ("UK", "AU", "PL")
MAX_RESPONSE_BODY_BYTES = 900_000
DOTENV_PATHS = (Path(__file__).resolve().parents[1] / ".env",)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _env_value(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _dotenv_value(name: str, *, dotenv_paths: tuple[Path, ...] = DOTENV_PATHS) -> str:
    normalized_name = str(name or "").strip()
    if not normalized_name:
        return ""
    for path in dotenv_paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() != normalized_name:
                continue
            normalized_value = value.strip().strip('"').strip("'")
            if normalized_value:
                return normalized_value
    return ""


def _default_api_token(*, dotenv_paths: tuple[Path, ...] = DOTENV_PATHS) -> str:
    return (
        _env_value("PROPERTYQUARRY_LIVE_API_TOKEN")
        or _env_value("EA_API_TOKEN")
        or _dotenv_value("PROPERTYQUARRY_LIVE_API_TOKEN", dotenv_paths=dotenv_paths)
        or _dotenv_value("EA_API_TOKEN", dotenv_paths=dotenv_paths)
    )


def _decode_json(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except Exception as exc:
        return {"_decode_error": f"{type(exc).__name__}: {exc}"}
    return dict(payload) if isinstance(payload, dict) else {"_decode_error": "json_payload_not_object"}


def _fetch_url(
    url: str,
    *,
    timeout_seconds: float,
    api_token: str,
    principal_id: str,
    host_header: str,
) -> dict[str, object]:
    headers = {
        "User-Agent": "PropertyQuarry-live-market-scope-smoke/1.0",
        "Accept": "application/json,*/*",
        "Host": host_header,
        "X-EA-Principal-ID": principal_id,
        "cf-ipcountry": "AT",
    }
    if api_token:
        headers["Authorization"] = f"Bearer {api_token}"
        headers["X-EA-API-Token"] = api_token
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_RESPONSE_BODY_BYTES)
            return {
                "status_code": int(response.status),
                "headers": dict(response.headers.items()),
                "body": body,
                "error": "",
            }
    except urllib.error.HTTPError as exc:
        return {
            "status_code": int(exc.code),
            "headers": dict(exc.headers.items()),
            "body": exc.read(MAX_RESPONSE_BODY_BYTES),
            "error": "",
        }
    except Exception as exc:
        return {
            "status_code": 0,
            "headers": {},
            "body": b"",
            "error": f"{type(exc).__name__}: {exc}",
        }


def build_live_market_scope_receipt(
    *,
    base_url: str,
    api_token: str,
    principal_id: str,
    host_header: str = "propertyquarry.com",
    timeout_seconds: float = 8.0,
    allowed_countries: tuple[str, ...] = ALLOWED_COUNTRIES,
    blocked_countries: tuple[str, ...] = BLOCKED_COUNTRIES,
    fetcher: Callable[[str, float], dict[str, object]] | None = None,
) -> dict[str, object]:
    effective_base = base_url.rstrip("/") + "/"

    def default_fetcher(url: str, timeout: float) -> dict[str, object]:
        return _fetch_url(
            url,
            timeout_seconds=timeout,
            api_token=api_token,
            principal_id=principal_id,
            host_header=host_header,
        )

    effective_fetcher = fetcher or default_fetcher
    checks: list[dict[str, object]] = []
    failed_count = 0

    for country in (*allowed_countries, *blocked_countries):
        url = urllib.parse.urljoin(effective_base, f"app/api/property/providers?country={urllib.parse.quote(country)}")
        result = effective_fetcher(url, timeout_seconds)
        status_code = int(result.get("status_code") or 0)
        body = _decode_json(bytes(result.get("body") or b""))
        providers = list(body.get("providers") or []) if isinstance(body.get("providers"), list) else []
        error_code = ""
        if isinstance(body.get("error"), dict):
            error_code = str(dict(body.get("error") or {}).get("code") or "").strip()
        allowed = country in allowed_countries
        row_checks = [
            {"name": "status", "ok": status_code == 200 if allowed else status_code == 400},
            {"name": "country_code", "ok": str(body.get("country_code") or "").strip().upper() == country if allowed else not str(body.get("country_code") or "").strip()},
            {"name": "providers_present", "ok": len(providers) > 0 if allowed else len(providers) == 0},
            {"name": "blocked_error_code", "ok": True if allowed else error_code == "unsupported_property_market"},
        ]
        ok = all(bool(item.get("ok")) for item in row_checks) and not str(result.get("error") or "").strip()
        if not ok:
            failed_count += 1
        checks.append(
            {
                "country": country,
                "expected": "allowed" if allowed else "blocked",
                "status_code": status_code,
                "provider_count": len(providers),
                "error_code": error_code,
                "ok": ok,
                "checks": row_checks,
                "fetch_error": str(result.get("error") or ""),
            }
        )

    return {
        "status": "pass" if failed_count == 0 else "fail",
        "generated_at": _utc_now_iso(),
        "base_url": base_url,
        "principal_id": principal_id,
        "allowed_countries": list(allowed_countries),
        "blocked_countries": list(blocked_countries),
        "failed_count": failed_count,
        "checks": checks,
        "notes": [
            "This smoke proves customer-facing provider catalog scope only.",
            "Provider definitions for other markets may remain internal, but customer search must not expose or dispatch them.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify live PropertyQuarry customer market scope.")
    parser.add_argument("--base-url", default=_env_value("PROPERTYQUARRY_LIVE_SMOKE_BASE_URL") or "http://localhost:8097")
    parser.add_argument("--host-header", default=_env_value("PROPERTYQUARRY_LIVE_HOST_HEADER") or "propertyquarry.com")
    parser.add_argument("--api-token", default=_default_api_token())
    parser.add_argument("--principal-id", default=_env_value("EA_PRINCIPAL_ID") or "cf-email:tibor.girschele@gmail.com")
    parser.add_argument("--timeout-seconds", type=float, default=float(_env_value("PROPERTYQUARRY_LIVE_MARKET_SCOPE_TIMEOUT_SECONDS") or "8"))
    parser.add_argument("--write", default="_completion/smoke/property-live-market-scope-latest.json")
    args = parser.parse_args()

    if not str(args.api_token or "").strip():
        raise SystemExit("EA_API_TOKEN or PROPERTYQUARRY_LIVE_API_TOKEN is required for live market scope smoke.")

    receipt = build_live_market_scope_receipt(
        base_url=str(args.base_url),
        api_token=str(args.api_token),
        principal_id=str(args.principal_id),
        host_header=str(args.host_header),
        timeout_seconds=float(args.timeout_seconds),
    )
    write_path = Path(args.write)
    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
