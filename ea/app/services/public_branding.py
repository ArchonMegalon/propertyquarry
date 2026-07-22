from __future__ import annotations

import os
import urllib.parse
from typing import Any

from app.services.public_clickrank import request_hostname


def _normalized_hosts_from_env(name: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = str(os.getenv(name) or "").strip()
    values = [entry.strip().lower().rstrip(".") for entry in raw.split(",") if entry.strip()]
    if values:
        return tuple(dict.fromkeys(values))
    return default


PROPERTYQUARRY_HOSTS = _normalized_hosts_from_env(
    "PROPERTYQUARRY_PUBLIC_HOSTS",
    default=("propertyquarry.com", "www.propertyquarry.com"),
)


def _propertyquarry_brand() -> dict[str, str]:
    return {
        "key": "propertyquarry",
        "name": "PropertyQuarry",
        "mark": "PQ",
        "create_label": "Email sign-in",
        "sign_in_label": "Sign in",
        "workspace_label": "Property research account",
        "app_home": "/app/search",
        "public_base_url": str(
            os.getenv("PROPERTY_PUBLIC_BASE_URL")
            or os.getenv("PROPERTYQUARRY_PUBLIC_BASE_URL")
            or "https://propertyquarry.com"
        ).strip().rstrip("/"),
        "repo_url": "https://github.com/ArchonMegalon/propertyquarry",
    }


def _hostname_is_local_development(hostname: str | None) -> bool:
    normalized = str(hostname or "").strip().lower().rstrip(".")
    if not normalized:
        return False
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        return True
    if normalized.endswith(".localhost"):
        return True
    return False


def _request_origin(request: Any) -> str:
    forwarded_host = str(getattr(request, "headers", {}).get("x-forwarded-host") or "").strip()
    forwarded_proto = str(getattr(request, "headers", {}).get("x-forwarded-proto") or "").strip()
    host = forwarded_host.split(",", 1)[0].strip() if forwarded_host else str(getattr(getattr(request, "url", None), "netloc", "") or "").strip()
    proto = forwarded_proto.split(",", 1)[0].strip() if forwarded_proto else str(getattr(getattr(request, "url", None), "scheme", "") or "").strip()
    if host and proto:
        return f"{proto}://{host}".rstrip("/")
    return str(getattr(request, "base_url", "") or "").rstrip("/")


def brand_from_hostname(hostname: str | None) -> dict[str, str]:
    _ = hostname
    return _propertyquarry_brand()


def request_brand(request: Any) -> dict[str, str]:
    brand = brand_from_hostname(request_hostname(request))
    hostname = request_hostname(request)
    if str(brand.get("key") or "").strip() == "propertyquarry" and _hostname_is_local_development(hostname):
        local_origin = _request_origin(request)
        parsed = urllib.parse.urlparse(local_origin)
        if parsed.scheme and parsed.netloc:
            brand = dict(brand)
            brand["public_base_url"] = local_origin
    return brand
