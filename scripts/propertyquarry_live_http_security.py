from __future__ import annotations

import ipaddress
import urllib.parse
from collections.abc import Iterable, Mapping


SENSITIVE_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "cf-access-client-id",
        "cf-access-client-secret",
        "cf-access-jwt-assertion",
        "cookie",
        "host",
        "proxy-authorization",
        "x-api-token",
        "x-ea-api-token",
        "x-ea-principal-id",
        "x-propertyquarry-release-probe-timestamp",
        "x-propertyquarry-release-probe-nonce",
        "x-propertyquarry-release-probe-signature",
        "x-forwarded-access-token",
    }
)


def normalized_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    scheme = str(parsed.scheme or "").lower()
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    if scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        raise ValueError("live_origin_invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("live_origin_port_invalid") from exc
    if port is not None and port < 1:
        raise ValueError("live_origin_port_invalid")
    default_port = 443 if scheme == "https" else 80
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{rendered_host}{rendered_port}"


def validated_live_base_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    origin = normalized_origin(value)
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("live_base_url_must_be_origin_only")
    hostname = str(parsed.hostname or "").lower().rstrip(".")
    loopback = hostname == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            loopback = False
    if str(parsed.scheme or "").lower() != "https" and not loopback:
        raise ValueError("live_base_url_requires_https")
    return origin


def url_matches_origin(url: str, authorized_origin: str) -> bool:
    try:
        return normalized_origin(url) == normalized_origin(authorized_origin)
    except ValueError:
        return False


def headers_for_authorized_origin(
    *,
    url: str,
    authorized_origin: str,
    headers: Mapping[str, str],
) -> dict[str, str]:
    if url_matches_origin(url, authorized_origin):
        return {str(key): str(value) for key, value in headers.items()}
    return {
        str(key): str(value)
        for key, value in headers.items()
        if str(key).strip().lower() not in SENSITIVE_REQUEST_HEADERS
    }


def redact_secret_values(value: object, *, secrets: Iterable[str]) -> str:
    redacted = str(value or "")
    normalized_secrets = sorted(
        {str(secret or "") for secret in secrets if len(str(secret or "")) >= 8},
        key=len,
        reverse=True,
    )
    for secret in normalized_secrets:
        redacted = redacted.replace(secret, "[redacted-secret]")
    return redacted
