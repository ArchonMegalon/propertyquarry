from __future__ import annotations

import hashlib
import hmac
import re
import urllib.parse
from collections.abc import Iterable


PROPERTYQUARRY_RELEASE_PROBE_TIMESTAMP_HEADER = "x-propertyquarry-release-probe-timestamp"
PROPERTYQUARRY_RELEASE_PROBE_NONCE_HEADER = "x-propertyquarry-release-probe-nonce"
PROPERTYQUARRY_RELEASE_PROBE_SIGNATURE_HEADER = "x-propertyquarry-release-probe-signature"
PROPERTYQUARRY_RELEASE_PROBE_NONCE_SHA256_RESPONSE_HEADER = (
    "x-propertyquarry-release-probe-nonce-sha256"
)
PROPERTYQUARRY_RELEASE_PROBE_AUDIENCE = "propertyquarry-release-probe-v1"

PROPERTYQUARRY_RELEASE_PROBE_STATIC_PATHS = frozenset(
    {
        "/sign-in",
        "/app/properties",
        "/app/search",
        "/app/shortlist",
        "/app/agents",
        "/app/alerts",
        "/app/account",
        "/app/billing",
        "/app/settings/google",
        "/app/settings/access",
        "/app/settings/usage",
        "/app/settings/support",
        "/app/support",
        "/app/settings/trust",
        "/app/settings/invitations",
        "/app/research",
        "/app/properties/packets",
    }
)
PROPERTYQUARRY_RELEASE_PROBE_ACCOUNT_QUERIES = frozenset(
    {
        "billing=1",
        "settings_view=access",
        "settings_view=google",
        "settings_view=invitations",
        "settings_view=support",
        "settings_view=trust",
        "settings_view=usage",
    }
)
_PROPERTYQUARRY_RELEASE_PROBE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,199}$")


def normalized_propertyquarry_release_probe_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or "").strip())
    scheme = str(parsed.scheme or "").strip().lower()
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if (
        scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("propertyquarry_release_probe_origin_invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("propertyquarry_release_probe_origin_invalid") from exc
    if port is not None and port < 1:
        raise ValueError("propertyquarry_release_probe_origin_invalid")
    default_port = 443 if scheme == "https" else 80
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = f":{port}" if port is not None and port != default_port else ""
    return f"{scheme}://{rendered_host}{rendered_port}"


def normalized_propertyquarry_release_probe_route(value: str) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if not raw or "#" in raw:
        return None
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return None
    return str(parsed.path or "/"), str(parsed.query or "")


def propertyquarry_release_probe_research_detail_route_valid(value: str) -> bool:
    route = normalized_propertyquarry_release_probe_route(value)
    if route is None:
        return False
    path, query = route
    prefix = "/app/research/"
    candidate_ref = path.removeprefix(prefix) if path.startswith(prefix) else ""
    if not _PROPERTYQUARRY_RELEASE_PROBE_ID_RE.fullmatch(candidate_ref):
        return False
    try:
        query_pairs = urllib.parse.parse_qsl(
            query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError:
        return False
    if len(query_pairs) != 1 or query_pairs[0][0] != "run_id":
        return False
    run_id = str(query_pairs[0][1] or "")
    return bool(
        _PROPERTYQUARRY_RELEASE_PROBE_ID_RE.fullmatch(run_id)
        and query == urllib.parse.urlencode({"run_id": run_id})
    )


def propertyquarry_release_probe_shortlist_run_path_valid(value: str) -> bool:
    route = normalized_propertyquarry_release_probe_route(value)
    if route is None:
        return False
    path, query = route
    prefix = "/app/shortlist/run/"
    run_id = path.removeprefix(prefix) if path.startswith(prefix) else ""
    return not query and bool(_PROPERTYQUARRY_RELEASE_PROBE_ID_RE.fullmatch(run_id))


def propertyquarry_release_probe_request_allowed(
    *,
    path: str,
    query_string: str,
    configured_routes: Iterable[str] = (),
) -> bool:
    normalized_path = str(path or "/")
    normalized_query = str(query_string or "")
    if normalized_path in PROPERTYQUARRY_RELEASE_PROBE_STATIC_PATHS:
        if not normalized_query:
            return True
        return (
            normalized_path == "/app/account"
            and normalized_query in PROPERTYQUARRY_RELEASE_PROBE_ACCOUNT_QUERIES
        )
    dynamic_routes = set()
    for value in configured_routes:
        if not (
            propertyquarry_release_probe_research_detail_route_valid(value)
            or propertyquarry_release_probe_shortlist_run_path_valid(value)
        ):
            continue
        route = normalized_propertyquarry_release_probe_route(value)
        if route is not None:
            dynamic_routes.add(route)
    return (normalized_path, normalized_query) in dynamic_routes


def propertyquarry_release_probe_signature(
    *,
    secret: str,
    method: str,
    path: str,
    query_string: str,
    timestamp: int | str,
    nonce: str,
    origin: str = "",
) -> str:
    normalized_origin = (
        normalized_propertyquarry_release_probe_origin(origin)
        if str(origin or "").strip()
        else ""
    )
    canonical = "\n".join(
        (
            "v1",
            normalized_origin,
            str(method or "GET").strip().upper(),
            str(path or "/"),
            str(query_string or ""),
            str(timestamp).strip(),
            str(nonce or "").strip(),
            PROPERTYQUARRY_RELEASE_PROBE_AUDIENCE,
        )
    )
    return hmac.new(
        str(secret or "").encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
