from __future__ import annotations

import html
import ipaddress
import json
import os
import re
from typing import Any
from urllib.parse import urlparse


_CLICKRANK_HOST_CONFIG = {
    "propertyquarry.com": ("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", ""),
    "www.propertyquarry.com": ("CLICKRANK_AI_PROPERTYQUARRY_SITE_ID", ""),
}

_RYBBIT_HOST_CONFIG = {
    "propertyquarry.com": ("RYBBIT_IO_PROPERTYQUARRY_SITE_ID", ""),
    "www.propertyquarry.com": ("RYBBIT_IO_PROPERTYQUARRY_SITE_ID", ""),
}

_CLICKRANK_PUBLIC_EXACT_PATHS = {
    "/",
    "/pricing",
    "/features",
    "/guides",
    "/markets",
    "/blog",
    "/compare",
}

_CLICKRANK_PUBLIC_PREFIXES = (
    "/features/",
    "/guides/",
    "/markets/",
    "/blog/",
    "/compare/",
)

_CLICKRANK_PRIVATE_PREFIXES = (
    "/app",
    "/api",
    "/v1",
    "/auth",
    "/admin",
    "/results",
    "/tours",
)

_CLICKRANK_SITE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,120}$")


def _normalize_hostname(hostname: str | None) -> str:
    return str(hostname or "").strip().lower().rstrip(".")


def _normalize_path(path: str | None) -> str:
    raw = str(path or "").strip()
    if not raw:
        return ""
    parsed_path = urlparse(raw).path if "://" in raw or "?" in raw else raw
    normalized = "/" + str(parsed_path or "").lstrip("/")
    if len(normalized) > 1:
        normalized = normalized.rstrip("/")
    return normalized


def clickrank_route_allowed(path: str | None) -> bool:
    normalized = _normalize_path(path)
    if not normalized:
        return False
    if any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in _CLICKRANK_PRIVATE_PREFIXES):
        return False
    if normalized in _CLICKRANK_PUBLIC_EXACT_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in _CLICKRANK_PUBLIC_PREFIXES)


def _clickrank_enabled() -> bool:
    for env_name in ("EA_ENABLE_CLICKRANK", "EA_PUBLIC_CLICKRANK_ENABLED"):
        if str(os.getenv(env_name) or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _rybbit_enabled() -> bool:
    if str(os.getenv("EA_PUBLIC_RYBBIT_RENDER_IN_CLICKRANK") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    for env_name in ("EA_ENABLE_RYBBIT", "EA_PUBLIC_RYBBIT_ENABLED"):
        if str(os.getenv(env_name) or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def _configured_public_base_hostname() -> str:
    configured = str(
        os.getenv("PROPERTYQUARRY_PUBLIC_APP_BASE_URL")
        or os.getenv("PROPERTYQUARRY_PUBLIC_BASE_URL")
        or ""
    )
    return _normalize_hostname(urlparse(configured).hostname or "")


def _hostname_can_fallback_to_public_base_url(hostname: str) -> bool:
    normalized = _normalize_hostname(hostname)
    if not normalized:
        return True
    if normalized == "localhost":
        return False
    if normalized.endswith((".internal", ".local", ".localhost")):
        return True
    if "." not in normalized:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return address.is_private and not address.is_loopback


def request_hostname(request: Any) -> str:
    if request is None:
        return ""
    headers = getattr(request, "headers", {})
    forwarded_host = str(headers.get("x-forwarded-host") or "").split(",", 1)[0].split(":", 1)[0].strip()
    if forwarded_host:
        return _normalize_hostname(forwarded_host)
    header_host = str(headers.get("host") or "").split(":", 1)[0].strip()
    if header_host:
        normalized_header_host = _normalize_hostname(header_host)
        configured_base_host = _configured_public_base_hostname()
        has_proxy_signal = bool(headers.get("x-forwarded-for") or headers.get("cf-connecting-ip") or headers.get("cf-ray"))
        if (
            normalized_header_host not in _CLICKRANK_HOST_CONFIG
            and _hostname_can_fallback_to_public_base_url(normalized_header_host)
            and configured_base_host in _CLICKRANK_HOST_CONFIG
            and has_proxy_signal
        ):
            return configured_base_host
        return normalized_header_host
    url = getattr(request, "url", None)
    return _normalize_hostname(getattr(url, "hostname", ""))


def request_path(request: Any) -> str:
    if request is None:
        return ""
    url = getattr(request, "url", None)
    return _normalize_path(getattr(url, "path", "") or getattr(request, "scope", {}).get("path", ""))


def clickrank_site_id_for_hostname(hostname: str | None) -> str:
    if not _clickrank_enabled():
        return ""
    normalized = _normalize_hostname(hostname)
    config = _CLICKRANK_HOST_CONFIG.get(normalized)
    if config is None and _hostname_can_fallback_to_public_base_url(normalized):
        configured_base_host = _configured_public_base_hostname()
        config = _CLICKRANK_HOST_CONFIG.get(configured_base_host)
    if not config:
        return ""
    env_name, fallback = config
    site_id = str(os.getenv(env_name) or fallback).strip()
    if not _CLICKRANK_SITE_ID_PATTERN.fullmatch(site_id):
        return ""
    return site_id


def rybbit_site_id_for_hostname(hostname: str | None) -> str:
    if not _rybbit_enabled():
        return ""
    normalized = _normalize_hostname(hostname)
    config = _RYBBIT_HOST_CONFIG.get(normalized)
    if config is None and _hostname_can_fallback_to_public_base_url(normalized):
        configured_base_host = _configured_public_base_hostname()
        config = _RYBBIT_HOST_CONFIG.get(configured_base_host)
    if not config:
        return ""
    env_name, fallback = config
    return str(os.getenv(env_name) or fallback).strip()


def _rybbit_script_attributes(site_id: str) -> list[str]:
    script_src = str(os.getenv("EA_PUBLIC_RYBBIT_SCRIPT_SRC") or "https://app.rybbit.io/api/script.js").strip()
    attributes = [
        f'src="{html.escape(script_src, quote=True)}"',
        "async",
        "defer",
        f'data-site-id="{html.escape(site_id, quote=True)}"',
    ]
    optional_map = {
        "EA_PUBLIC_RYBBIT_TAG": "data-tag",
        "EA_PUBLIC_RYBBIT_DEBOUNCE": "data-debounce",
        "EA_PUBLIC_RYBBIT_SKIP_PATTERNS": "data-skip-patterns",
        "EA_PUBLIC_RYBBIT_MASK_PATTERNS": "data-mask-patterns",
    }
    for env_name, attr_name in optional_map.items():
        raw_value = str(os.getenv(env_name) or "").strip()
        if not raw_value:
            continue
        if attr_name in {"data-skip-patterns", "data-mask-patterns"}:
            try:
                parsed = json.loads(raw_value)
                if isinstance(parsed, list):
                    raw_value = json.dumps([str(item) for item in parsed], ensure_ascii=True, separators=(",", ":"))
            except json.JSONDecodeError:
                pass
        attributes.append(f'{attr_name}="{html.escape(raw_value, quote=True)}"')
    return attributes


def clickrank_head_snippet(
    hostname: str | None,
    path: str | None = None,
    *,
    consent_granted: bool = False,
) -> str:
    if consent_granted is not True:
        return ""
    if not clickrank_route_allowed(path):
        return ""
    snippets: list[str] = []
    clickrank_site_id = clickrank_site_id_for_hostname(hostname)
    if clickrank_site_id:
        snippets.append(
            "<script>\n"
            'var clickRankAi = document.createElement("script");\n'
            f'clickRankAi.src = "https://js.clickrank.ai/seo/{clickrank_site_id}/script?" + new Date().getTime();\n'
            "clickRankAi.async = true;\n"
            "document.head.appendChild(clickRankAi);\n"
            "</script>"
        )
    rybbit_site_id = rybbit_site_id_for_hostname(hostname)
    if rybbit_site_id:
        snippets.append("<script " + " ".join(_rybbit_script_attributes(rybbit_site_id)) + "></script>")
    return "\n".join(snippets)
