from __future__ import annotations

import json
import posixpath
import re
import urllib.parse
from collections.abc import Mapping
from datetime import datetime


GOVERNED_PRATER_SEARCH_RUN_ID = "98bed75e984549c6bd4371d602662ab8"
GOVERNED_PRATER_CANDIDATE_REF = "053ad185e1c44b2e"
GOVERNED_PRATER_EXTERNAL_ID = "1807240910"
GOVERNED_PRATER_SOURCE_REF = "property-scout:1807240910"
GOVERNED_PRATER_PROVIDER_KEY = "willhaben"
GOVERNED_PRATER_LISTING_URL = (
    "https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/"
    "wien-1020-leopoldstadt/"
    "naehe-prater-und-messe-wien-i-u1-u2-i-ruhelage-i-garage-i-"
    "maisonette-i-voll-moebliert-i-in-der-vorgartenstrasse-1807240910/"
)
GOVERNED_PRATER_SLUG = (
    "prater-messe-maisonette-ai-360-053ad185e1c44b2e"
)
GOVERNED_PRATER_CONTROL_URL = (
    "https://propertyquarry.com/tours/"
    "prater-messe-maisonette-ai-360-053ad185e1c44b2e/control"
)
GOVERNED_PRATER_TOUR_SHA256 = (
    "c3795ca2956c18e3e8b1749611660052dac794a08dec7f47db212b51049cf849"
)
GOVERNED_PRATER_SOURCE_TREE_SHA256 = (
    "fe2bdc9162d82236d70d0e74deb283bb06186026fd2c31c90431711cb87a775c"
)
GOVERNED_PRATER_CORE_MANIFEST_SHA256 = (
    "15e9b6bac56c47363da0fe49b99697215833d9ea6c94ae43253bde4e288c401d"
)
GOVERNED_PRATER_MATERIALIZATION_RECEIPT_SHA256 = (
    "accba9c5b5575020d9cd6fcc299ed9653f6d8f094d58598e7bfc13db0061daba"
)
GOVERNED_PRATER_CANDIDATE_MARKER_SHA256 = (
    "bf436b0645e44b203fe9b0c2f01c88d1ddce25aa7b1a45d04fa27b805eaf73fd"
)
GOVERNED_PUBLIC_TOUR_VOLUME_NAME = (
    "property_propertyquarry_governed_public_tours"
)
GOVERNED_PUBLIC_TOUR_MOUNT_TARGET = (
    "/data/governed_public_property_tours"
)
GOVERNED_PRATER_REVOCATION_FILENAME = (
    ".prater-messe-maisonette-ai-360-053ad185e1c44b2e.revoked.v1.json"
)
GOVERNED_PRATER_REVOCATION_SCHEMA = (
    "propertyquarry.governed-public-tour-revocation.v1"
)
GOVERNED_PRATER_REVOCATION_AUTHORITY = "propertyquarry-release-control"
GOVERNED_PRATER_REVOCATION_STATUS = "revoked"
GOVERNED_PRATER_REVOCATION_VERSION = 1
GOVERNED_PRATER_REVOCATION_MAX_BYTES = 4096
GOVERNED_PRATER_REVOCATION_MODE = 0o444
GOVERNED_PRATER_REVOCATION_REQUIRED_UID = 0
GOVERNED_PRATER_REVOCATION_REQUIRED_GID = 0
GOVERNED_PRATER_CLOSEOUT_REQUEST_PATH = (
    "/run/propertyquarry-release-control/ai-panorama-install/"
    "prater-ai-panorama-closeout-request.v1.json"
)
GOVERNED_PRATER_REVOCATION_KEYS = frozenset(
    {
        "authority",
        "revocation_id",
        "revoked_at",
        "schema",
        "slug",
        "status",
        "tour_sha256",
        "version",
    }
)
_REVOCATION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
_REVOKED_AT_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z\Z"
)


def governed_prater_slug_reserved(value: object) -> bool:
    return type(value) is str and value == GOVERNED_PRATER_SLUG


def governed_prater_control_url_reserved(value: object) -> bool:
    return type(value) is str and value == GOVERNED_PRATER_CONTROL_URL


def governed_prater_url_namespace_reserved(value: object) -> bool:
    """Recognize every URL spelling that can resolve below the protected slug."""

    if type(value) is not str or not value:
        return False
    if len(value) > 4096 or "\x00" in value:
        return True
    decoded_value = value
    if re.search(r"%(?![0-9A-Fa-f]{2})", decoded_value):
        return True
    for _iteration in range(len(decoded_value) + 1):
        decoded = urllib.parse.unquote(decoded_value)
        if decoded == decoded_value:
            break
        decoded_value = decoded
    else:
        return True
    if GOVERNED_PRATER_SLUG in decoded_value:
        return True
    try:
        path = urllib.parse.urlsplit(decoded_value).path
    except ValueError:
        return True
    path = path.replace("\\", "/")
    path = f"/{path.lstrip('/')}"
    normalized = posixpath.normpath(path)
    protected = f"/tours/{GOVERNED_PRATER_SLUG}"
    return normalized == protected or normalized.startswith(f"{protected}/")


def canonical_governed_prater_revocation_bytes(
    payload: Mapping[str, object],
) -> bytes:
    revoked_at = payload.get("revoked_at") if type(payload) is dict else None
    if (
        type(payload) is not dict
        or set(payload) != GOVERNED_PRATER_REVOCATION_KEYS
        or payload.get("schema") != GOVERNED_PRATER_REVOCATION_SCHEMA
        or type(payload.get("version")) is not int
        or payload.get("version") != GOVERNED_PRATER_REVOCATION_VERSION
        or payload.get("authority") != GOVERNED_PRATER_REVOCATION_AUTHORITY
        or payload.get("status") != GOVERNED_PRATER_REVOCATION_STATUS
        or payload.get("slug") != GOVERNED_PRATER_SLUG
        or payload.get("tour_sha256") != GOVERNED_PRATER_TOUR_SHA256
        or type(payload.get("revocation_id")) is not str
        or _REVOCATION_ID_RE.fullmatch(payload["revocation_id"]) is None
        or type(revoked_at) is not str
        or _REVOKED_AT_RE.fullmatch(revoked_at) is None
    ):
        raise ValueError("governed_prater_revocation_invalid")
    try:
        parsed_revoked_at = datetime.fromisoformat(
            f"{revoked_at[:-1]}+00:00"
        )
    except ValueError as exc:
        raise ValueError("governed_prater_revocation_invalid") from exc
    if parsed_revoked_at.utcoffset() is None or (
        parsed_revoked_at.utcoffset().total_seconds() != 0
    ):
        raise ValueError("governed_prater_revocation_invalid")
    try:
        return (
            json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("governed_prater_revocation_invalid") from exc


def validate_governed_prater_revocation_bytes(
    raw: bytes,
) -> dict[str, object]:
    if (
        type(raw) is not bytes
        or not raw
        or len(raw) > GOVERNED_PRATER_REVOCATION_MAX_BYTES
    ):
        raise ValueError("governed_prater_revocation_invalid")

    def _strict_object(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("governed_prater_revocation_invalid")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=_strict_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("governed_prater_revocation_invalid")
            ),
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("governed_prater_revocation_invalid") from exc
    if type(payload) is not dict:
        raise ValueError("governed_prater_revocation_invalid")
    canonical = canonical_governed_prater_revocation_bytes(payload)
    if raw != canonical:
        raise ValueError("governed_prater_revocation_invalid")
    return payload
