from __future__ import annotations

import secrets
import sys
import time
import urllib.parse
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))

from app.propertyquarry_release_probe import (
    PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER,
    PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER,
    normalized_propertyquarry_release_probe_origin,
    propertyquarry_release_probe_request_allowed,
    propertyquarry_release_probe_signature,
)
from scripts.propertyquarry_live_http_security import (
    headers_for_authorized_origin,
    url_matches_origin,
)


_LEGACY_AUTH_HEADERS = frozenset(
    {
        "authorization",
        "x-api-token",
        "x-ea-api-token",
        "x-ea-principal-id",
    }
)
_RELEASE_PROBE_HEADERS = frozenset(
    {
        PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER,
        PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER,
        PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER,
    }
)


def release_probe_path_allowed(
    url: str,
    *,
    configured_routes: tuple[str, ...] = (),
) -> bool:
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    return propertyquarry_release_probe_request_allowed(
        path=str(parsed.path or "/"),
        query_string=str(parsed.query or ""),
        configured_routes=configured_routes,
    )


def live_probe_request_headers(
    *,
    url: str,
    authorized_origin: str,
    headers: Mapping[str, str],
    release_probe_secret: str = "",
    method: str = "GET",
    timestamp: int | None = None,
    nonce: str = "",
    configured_routes: tuple[str, ...] = (),
) -> dict[str, str]:
    scoped = headers_for_authorized_origin(
        url=url,
        authorized_origin=authorized_origin,
        headers=headers,
    )
    scoped = {
        str(name): str(value)
        for name, value in scoped.items()
        if str(name or "").strip().lower() not in _RELEASE_PROBE_HEADERS
    }
    secret = str(release_probe_secret or "").strip()
    if not secret:
        return scoped

    scoped = {
        str(name): str(value)
        for name, value in scoped.items()
        if str(name or "").strip().lower() not in _LEGACY_AUTH_HEADERS
    }
    normalized_method = str(method or "GET").strip().upper()
    if normalized_method not in {"GET", "HEAD"}:
        return scoped
    if url_matches_origin(url, authorized_origin) and release_probe_path_allowed(
        url,
        configured_routes=configured_routes,
    ):
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        issued_at = int(time.time()) if timestamp is None else int(timestamp)
        request_nonce = str(nonce or "").strip() or secrets.token_urlsafe(24)
        signature = propertyquarry_release_probe_signature(
            secret=secret,
            origin=normalized_propertyquarry_release_probe_origin(authorized_origin),
            method=normalized_method,
            path=str(parsed.path or "/"),
            query_string=str(parsed.query or ""),
            timestamp=issued_at,
            nonce=request_nonce,
        )
        scoped[PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER] = str(issued_at)
        scoped[PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER] = request_nonce
        scoped[PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER] = signature
    return scoped
