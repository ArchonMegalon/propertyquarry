#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


RECEIPT_SCHEMA = "propertyquarry.rybbit_delivery_receipt.v1"
PROBE_EVENT_NAME = "propertyquarry_launch_probe"
RYBBIT_COLLECTOR_PATH = "/api/track"
REQUIRED_PRIVACY_CHECKS = (
    "collector_payload_parsed",
    "anonymous_event_no_attributes",
    "no_identify",
    "no_principal",
    "no_email",
    "no_private_candidate_listing_contact_fields",
    "no_custom_attributes",
    "private_app_paths_masked",
    "api_paths_skipped",
)
MAX_RYBBIT_API_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RYBBIT_BROWSER_RESPONSE_BYTES = 4 * 1024 * 1024
_CUSTOM_ATTRIBUTE_FIELD_TOKENS = {
    "attributes",
    "customattributes",
    "customproperties",
    "eventdata",
    "eventproperties",
    "properties",
    "props",
}
_PRIVATE_FIELD_FRAGMENTS = ("candidate", "listing", "contact")
_PRIVATE_PROPERTY_FIELD_TOKENS = {
    "propertyid",
    "propertyref",
    "propertyurl",
    "sourceurl",
    "streetaddress",
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_datetime(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: object) -> str:
    return _sha256_bytes(_text(value).encode("utf-8"))


def _json_sha256(value: object) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _origin(value: object) -> str:
    parsed = urllib.parse.urlparse(_text(value))
    if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.username or parsed.password:
        return ""
    return f"https://{parsed.netloc.lower()}"


def _same_origin(url: object, expected_origin: str) -> bool:
    observed = _origin(url)
    expected = _origin(expected_origin)
    return bool(observed and expected and observed == expected)


def _request_url(value: object) -> str:
    parsed = urllib.parse.urlsplit(_text(value))
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return ""
    return urllib.parse.urlunsplit(
        ("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )


def _is_json_content_type(value: object) -> bool:
    media_type = _text(value).split(";", 1)[0].strip().casefold()
    return media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )


def _positive_finite(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _walk(value: object):  # type: ignore[no-untyped-def]
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _contains_scalar(payload: object, expected: str) -> bool:
    normalized = _text(expected)
    return bool(normalized) and any(_text(item) == normalized for item in _walk(payload) if not isinstance(item, (dict, list)))


def _bool_field(payload: object, names: set[str]) -> bool | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).casefold() in names and isinstance(value, bool):
                return value
            found = _bool_field(value, names)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _bool_field(value, names)
            if found is not None:
                return found
    return None


def _event_record(payload: object, event_name: str) -> dict[str, object]:
    for value in _walk(payload):
        if not isinstance(value, dict):
            continue
        row = dict(value)
        name = _text(row.get("event_name") or row.get("eventName") or row.get("name"))
        if name == event_name:
            return row
    return {}


def _event_count(row: dict[str, object]) -> int:
    for key in ("count", "event_count", "eventCount", "total"):
        try:
            return max(int(row.get(key) or 0), 0)
        except (TypeError, ValueError):
            continue
    return 1 if row else 0


def _event_timestamp(row: dict[str, object]) -> datetime | None:
    for key in ("timestamp", "last_seen_at", "lastSeenAt", "last_seen"):
        parsed = _parse_datetime(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _integer(value: object, *, default: int = -1) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _request_payload_bytes(request: object) -> bytes:
    try:
        payload = getattr(request, "post_data_buffer", None)
    except Exception:
        payload = None
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, bytearray):
        return bytes(payload)
    try:
        post_data = getattr(request, "post_data", None)
    except Exception:
        post_data = None
    return post_data.encode("utf-8") if isinstance(post_data, str) else b""


def _event_name_value(payload: object, event_name: str) -> bool:
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).replace("-", "_").casefold()
            if normalized_key in {"event", "event_name", "eventname", "name"}:
                if not isinstance(value, (dict, list)) and _text(value) == event_name:
                    return True
            if _event_name_value(value, event_name):
                return True
    elif isinstance(payload, list):
        return any(_event_name_value(value, event_name) for value in payload)
    return False


def _strict_json_value(value: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON field")
            result[key] = item
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError("non-finite JSON value")

    return json.loads(
        value,
        object_pairs_hook=reject_duplicates,
        parse_constant=reject_constant,
    )


def _decoded_request_payload(payload: bytes) -> object | None:
    if not payload or len(payload) > MAX_RYBBIT_BROWSER_RESPONSE_BYTES:
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        decoded = _strict_json_value(text)
    except (ValueError, RecursionError):
        decoded = None
    if isinstance(decoded, (dict, list)):
        return decoded
    try:
        pairs = urllib.parse.parse_qsl(
            text,
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=256,
        )
    except ValueError:
        return None
    if not pairs:
        return None
    form: list[dict[str, object]] = []
    for key, value in pairs:
        try:
            nested = _strict_json_value(value)
        except (ValueError, RecursionError):
            nested = value
        form.append({key: nested})
    return form


def _field_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", _text(value).casefold())


def _custom_attributes_empty(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple, set)):
        return not value
    return False


def _collector_payload_privacy(payload: bytes, event_name: str) -> dict[str, bool]:
    decoded = _decoded_request_payload(payload)
    claims = {
        "collector_payload_parsed": decoded is not None,
        "anonymous_event_no_attributes": False,
        "no_identify": decoded is not None,
        "no_principal": decoded is not None,
        "no_email": decoded is not None,
        "no_private_candidate_listing_contact_fields": decoded is not None,
        "no_custom_attributes": decoded is not None,
    }
    if decoded is None:
        return claims

    pending = [decoded]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                token = _field_token(key)
                if "identify" in token:
                    claims["no_identify"] = False
                if "principal" in token:
                    claims["no_principal"] = False
                if "email" in token:
                    claims["no_email"] = False
                if (
                    any(fragment in token for fragment in _PRIVATE_FIELD_FRAGMENTS)
                    or token in _PRIVATE_PROPERTY_FIELD_TOKENS
                ):
                    claims["no_private_candidate_listing_contact_fields"] = False
                if token in _CUSTOM_ATTRIBUTE_FIELD_TOKENS and not _custom_attributes_empty(
                    value
                ):
                    claims["no_custom_attributes"] = False
                pending.append(value)
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, str):
            normalized = current.strip().casefold()
            if normalized == "identify":
                claims["no_identify"] = False
            if re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", current.strip()):
                claims["no_email"] = False

    claims["anonymous_event_no_attributes"] = bool(
        _event_name_value(decoded, event_name)
        and claims["no_identify"]
        and claims["no_principal"]
        and claims["no_email"]
        and claims["no_private_candidate_listing_contact_fields"]
        and claims["no_custom_attributes"]
    )
    return claims


def _request_payload_binds_event(payload: bytes, event_name: str) -> bool:
    decoded = _decoded_request_payload(payload)
    return decoded is not None and _event_name_value(decoded, event_name)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # type: ignore[no-untyped-def]
        return None


def _http_json(
    *,
    url: str,
    expected_origin: str,
    api_key: str,
    timeout_seconds: float,
) -> tuple[int, dict[str, object], dict[str, object]]:
    requested_url = _request_url(url)
    if not requested_url or not _same_origin(requested_url, expected_origin):
        raise ValueError("Rybbit API request URL must be HTTPS and share the configured analytics origin")
    if not _positive_finite(timeout_seconds):
        raise ValueError("Rybbit API timeout must be finite and positive")
    request = urllib.request.Request(
        requested_url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "PropertyQuarry-Rybbit-Release-Probe/1",
        },
    )
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=timeout_seconds) as response:
        final_url = _request_url(response.geturl())
        if (
            not final_url
            or final_url != requested_url
            or not _same_origin(final_url, expected_origin)
        ):
            raise ValueError("Rybbit API response final URL differs from the authorized request URL")
        content_type = _text(response.headers.get("Content-Type"))
        if not _is_json_content_type(content_type):
            raise ValueError("Rybbit API response content type is not JSON")
        body = response.read(MAX_RYBBIT_API_RESPONSE_BYTES + 1)
        if len(body) > MAX_RYBBIT_API_RESPONSE_BYTES:
            raise ValueError("Rybbit API response exceeds the bounded release-proof limit")
        status_code = int(getattr(response, "status", 0) or 0)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Rybbit API response is not valid JSON") from exc
    if not isinstance(payload, dict):
        payload = {"rows": payload} if isinstance(payload, list) else {}
    metadata: dict[str, object] = {
        "response_sha256": _sha256_bytes(body),
        "response_size_bytes": len(body),
        "response_limit_bytes": MAX_RYBBIT_API_RESPONSE_BYTES,
        "content_type": content_type.split(";", 1)[0].strip().casefold(),
        "requested_url_origin": _origin(requested_url),
        "final_url_origin": _origin(final_url),
        "requested_url_sha256": _sha256_text(requested_url),
        "final_url_sha256": _sha256_text(final_url),
        "same_request_url": final_url == requested_url,
        "redirected": False,
    }
    return status_code, dict(payload), metadata


def _browser_probe(
    *,
    public_origin: str,
    analytics_origin: str,
    site_id: str,
    timeout_seconds: float,
) -> dict[str, object]:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - protected runner owns browser dependency
        raise RuntimeError("playwright is required for the Rybbit delivery probe") from exc
    captured: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(ignore_https_errors=False)
            page = context.new_page()

            def _capture(response) -> None:  # type: ignore[no-untyped-def]
                if not _same_origin(response.url, analytics_origin):
                    return
                request_payload = _request_payload_bytes(response.request)
                payload_privacy = _collector_payload_privacy(
                    request_payload,
                    PROBE_EVENT_NAME,
                )
                captured.append(
                    {
                        "response": response,
                        "url": str(response.url),
                        "status_code": int(response.status),
                        "method": str(response.request.method or "").upper(),
                        "request_payload_sha256": _sha256_bytes(request_payload),
                        "request_payload_size_bytes": len(request_payload),
                        "event_name_bound": _request_payload_binds_event(
                            request_payload,
                            PROBE_EVENT_NAME,
                        ),
                        "payload_privacy": payload_privacy,
                        "observed_at": _iso(_utc_now()),
                    }
                )

            page.on("response", _capture)
            page.goto(public_origin, wait_until="networkidle", timeout=max(int(timeout_seconds * 1000), 1000))
            script_attrs = page.locator(f'script[data-site-id="{site_id}"]').first.evaluate(
                "element => ({src: element.src, siteId: element.dataset.siteId, skip: element.dataset.skipPatterns || '', mask: element.dataset.maskPatterns || ''})"
            )
            page.wait_for_function(
                "() => window.rybbit && typeof window.rybbit.trackEvent === 'function'",
                timeout=10_000,
            )
            sent_at = _utc_now()
            page.evaluate(f"() => window.rybbit.trackEvent({json.dumps(PROBE_EVENT_NAME)})")
            page.wait_for_timeout(3_000)

            script_candidates = [
                row for row in captured if urllib.parse.urlparse(_text(row.get("url"))).path == "/api/script.js"
            ]
            collector_candidates = [
                row
                for row in captured
                if str(row.get("method") or "") == "POST"
                and urllib.parse.urlparse(_text(row.get("url"))).path
                == RYBBIT_COLLECTOR_PATH
                and 200 <= int(row.get("status_code") or 0) < 300
                and row.get("event_name_bound") is True
                and (_parse_datetime(row.get("observed_at")) or datetime.min.replace(tzinfo=timezone.utc)) >= sent_at
            ]
            if not script_candidates or not collector_candidates:
                raise RuntimeError("Rybbit script or post-event collector response was not observed")
            script_row = script_candidates[-1]
            collector_row = collector_candidates[-1]
            script_body = script_row["response"].body()  # type: ignore[index,union-attr]
            try:
                collector_body = collector_row["response"].body()  # type: ignore[index,union-attr]
            except Exception:
                # A successful no-content collector response has no body to retrieve,
                # but the response itself is still authoritative delivery evidence.
                collector_body = b""
            if (
                len(script_body) > MAX_RYBBIT_BROWSER_RESPONSE_BYTES
                or len(collector_body) > MAX_RYBBIT_BROWSER_RESPONSE_BYTES
            ):
                raise RuntimeError("Rybbit browser response exceeds the bounded release-proof limit")
        finally:
            browser.close()

    script_url = _text(script_row.get("url"))
    collector_url = _text(collector_row.get("url"))
    attrs_text = json.dumps(script_attrs, sort_keys=True).casefold()
    skip_text = _text(dict(script_attrs).get("skip"))
    mask_text = _text(dict(script_attrs).get("mask"))
    payload_privacy = dict(collector_row.get("payload_privacy") or {})
    return {
        "script": {
            "url": script_url,
            "status_code": int(script_row.get("status_code") or 0),
            "sha256": _sha256_bytes(script_body),
            "size_bytes": len(script_body),
            "site_id_bound": _text(dict(script_attrs).get("siteId")) == site_id,
        },
        "collector": {
            "url_origin": _origin(collector_url),
            "url_path": urllib.parse.urlparse(collector_url).path,
            "url_sha256": _sha256_text(collector_url),
            "method": _text(collector_row.get("method")),
            "status_code": int(collector_row.get("status_code") or 0),
            "response_sha256": _sha256_bytes(collector_body),
            "size_bytes": len(collector_body),
            "request_payload_sha256": _text(collector_row.get("request_payload_sha256")),
            "request_payload_size_bytes": _integer(
                collector_row.get("request_payload_size_bytes"),
                default=-1,
            ),
            "event_name_bound": collector_row.get("event_name_bound") is True,
            "observed_at": _text(collector_row.get("observed_at")),
        },
        "event": {
            "name": PROBE_EVENT_NAME,
            "sent_at": _iso(sent_at),
            "anonymous": payload_privacy.get("anonymous_event_no_attributes") is True,
            "attribute_count": 0
            if payload_privacy.get("no_custom_attributes") is True
            else 1,
        },
        "privacy": {
            "collector_payload_parsed": payload_privacy.get("collector_payload_parsed")
            is True,
            "anonymous_event_no_attributes": payload_privacy.get(
                "anonymous_event_no_attributes"
            )
            is True,
            "no_identify": payload_privacy.get("no_identify") is True
            and "identify" not in attrs_text,
            "no_principal": payload_privacy.get("no_principal") is True
            and "principal" not in attrs_text,
            "no_email": payload_privacy.get("no_email") is True
            and "email" not in attrs_text,
            "no_private_candidate_listing_contact_fields": payload_privacy.get(
                "no_private_candidate_listing_contact_fields"
            )
            is True,
            "no_custom_attributes": payload_privacy.get("no_custom_attributes") is True,
            "private_app_paths_masked": "/app/**" in mask_text,
            "api_paths_skipped": "/api/**" in skip_text or "/app/api/**" in skip_text,
        },
    }


def _api_probe(
    *,
    analytics_origin: str,
    site_id: str,
    api_key: str,
    site_api_url: str,
    has_data_api_url: str,
    events_api_url: str,
    sent_at: datetime,
    timeout_seconds: float,
    arrival_timeout_seconds: float,
) -> dict[str, object]:
    for label, url in {
        "site": site_api_url,
        "has_data": has_data_api_url,
        "events": events_api_url,
    }.items():
        if not _same_origin(url, analytics_origin):
            raise ValueError(
                f"Rybbit {label} API URL must be HTTPS and share the configured analytics origin"
            )
    if not _positive_finite(arrival_timeout_seconds):
        raise ValueError("Rybbit event-arrival timeout must be finite and positive")
    site_status, site_payload, site_metadata = _http_json(
        url=site_api_url,
        expected_origin=analytics_origin,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    data_status, data_payload, data_metadata = _http_json(
        url=has_data_api_url,
        expected_origin=analytics_origin,
        api_key=api_key,
        timeout_seconds=timeout_seconds,
    )
    deadline = time.monotonic() + max(arrival_timeout_seconds, 1.0)
    event_status = 0
    event_payload: dict[str, object] = {}
    event_metadata: dict[str, object] = {}
    event_row: dict[str, object] = {}
    while time.monotonic() < deadline:
        event_status, event_payload, event_metadata = _http_json(
            url=events_api_url,
            expected_origin=analytics_origin,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )
        event_row = _event_record(event_payload, PROBE_EVENT_NAME)
        event_time = _event_timestamp(event_row)
        if event_row and event_time is not None and event_time >= sent_at:
            break
        time.sleep(min(5.0, max(deadline - time.monotonic(), 0.1)))
    event_time = _event_timestamp(event_row)
    return {
        "auth": {"kind": "bearer_api_key", "secret_in_receipt": False},
        "site": {
            "status_code": site_status,
            **site_metadata,
            "site_id_bound": _contains_scalar(site_payload, site_id),
        },
        "has_data": {
            "status_code": data_status,
            **data_metadata,
            "has_data": _bool_field(data_payload, {"hasdata", "has_data"}),
        },
        "events": {
            "status_code": event_status,
            **event_metadata,
            "event_name": PROBE_EVENT_NAME if event_row else "",
            "event_count": _event_count(event_row),
            "last_seen_at": _iso(event_time) if event_time else "",
            "observed_after_probe": bool(event_row and event_time is not None and event_time >= sent_at),
        },
    }


def build_receipt(
    *,
    candidate_sha: str,
    public_origin: str,
    analytics_origin: str,
    site_id: str,
    browser: dict[str, object],
    api: dict[str, object],
    generated_at: datetime | None = None,
) -> dict[str, object]:
    now = generated_at or _utc_now()
    receipt: dict[str, object] = {
        "schema": RECEIPT_SCHEMA,
        "status": "pass",
        "generated_at": _iso(now),
        "candidate_sha": _text(candidate_sha),
        "public_origin": _origin(public_origin),
        "analytics_origin": _origin(analytics_origin),
        "site_id_sha256": _sha256_text(site_id),
        "browser": browser,
        "api": api,
        "failures": [],
    }
    failures = verify_receipt(
        receipt,
        expected_candidate_sha=candidate_sha,
        expected_public_origin=public_origin,
        expected_analytics_origin=analytics_origin,
        expected_site_id_sha256=_sha256_text(site_id),
        max_age_minutes=15,
        now=now,
    )
    receipt["failures"] = failures
    receipt["status"] = "pass" if not failures else "fail"
    return receipt


def verify_receipt(
    receipt: dict[str, object],
    *,
    expected_candidate_sha: str,
    expected_public_origin: str,
    expected_analytics_origin: str,
    expected_site_id_sha256: str,
    max_age_minutes: float,
    now: datetime | None = None,
) -> list[str]:
    observed_at = now or _utc_now()
    failures: list[str] = []
    if receipt.get("schema") != RECEIPT_SCHEMA:
        failures.append("Rybbit receipt schema mismatch")
    candidate_sha = _text(receipt.get("candidate_sha"))
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_sha) or candidate_sha != _text(expected_candidate_sha):
        failures.append("Rybbit receipt candidate SHA mismatch")
    expected_public = _origin(expected_public_origin)
    expected_analytics = _origin(expected_analytics_origin)
    if not expected_public or _origin(receipt.get("public_origin")) != expected_public:
        failures.append("Rybbit receipt public origin mismatch")
    if not expected_analytics or _origin(receipt.get("analytics_origin")) != expected_analytics:
        failures.append("Rybbit receipt analytics origin mismatch")
    site_id_sha256 = _text(receipt.get("site_id_sha256"))
    if (
        not re.fullmatch(r"[0-9a-f]{64}", site_id_sha256)
        or site_id_sha256 != _text(expected_site_id_sha256)
    ):
        failures.append("Rybbit receipt site identity mismatch")
    if receipt.get("status") != "pass" or receipt.get("failures") != []:
        failures.append("Rybbit receipt does not declare a clean pass")
    generated_at = _parse_datetime(receipt.get("generated_at"))
    age_policy_valid = _positive_finite(max_age_minutes)
    if not age_policy_valid:
        failures.append("Rybbit receipt maximum age policy is invalid")
    if generated_at is None or generated_at > observed_at:
        failures.append("Rybbit receipt generated_at is invalid")
    elif age_policy_valid and (observed_at - generated_at).total_seconds() > max_age_minutes * 60.0:
        failures.append("Rybbit receipt is stale")
    browser = dict(receipt.get("browser") or {})
    script = dict(browser.get("script") or {})
    collector = dict(browser.get("collector") or {})
    event = dict(browser.get("event") or {})
    script_url = _text(script.get("url"))
    if (
        _integer(script.get("status_code"), default=0) != 200
        or not re.fullmatch(r"[0-9a-f]{64}", _text(script.get("sha256")))
        or _integer(script.get("size_bytes"), default=-1) <= 0
    ):
        failures.append("Rybbit tracking script was not delivered with digest evidence")
    if (
        script.get("site_id_bound") is not True
        or not _same_origin(script_url, expected_analytics)
        or urllib.parse.urlparse(script_url).path != "/api/script.js"
    ):
        failures.append("Rybbit tracking script is not bound to the expected site/origin")
    collector_status = _integer(collector.get("status_code"), default=0)
    if collector.get("method") != "POST" or not 200 <= collector_status < 300:
        failures.append("Rybbit collector did not accept the protected browser event")
    collector_path = _text(collector.get("url_path"))
    if _origin(collector.get("url_origin")) != expected_analytics:
        failures.append("Rybbit collector origin mismatch")
    if (
        collector_path != RYBBIT_COLLECTOR_PATH
        or not re.fullmatch(r"[0-9a-f]{64}", _text(collector.get("url_sha256")))
        or not re.fullmatch(r"[0-9a-f]{64}", _text(collector.get("response_sha256")))
        or _integer(collector.get("size_bytes"), default=-1) < 0
    ):
        failures.append("Rybbit collector URL or response digest evidence is invalid")
    if (
        collector.get("event_name_bound") is not True
        or _integer(collector.get("request_payload_size_bytes"), default=-1) <= 0
        or not re.fullmatch(r"[0-9a-f]{64}", _text(collector.get("request_payload_sha256")))
    ):
        failures.append("Rybbit collector request payload is not bound to the exact probe event")
    event_sent_at = _parse_datetime(event.get("sent_at"))
    collector_observed_at = _parse_datetime(collector.get("observed_at"))
    if (
        event_sent_at is None
        or collector_observed_at is None
        or collector_observed_at < event_sent_at
        or (generated_at is not None and (event_sent_at > generated_at or collector_observed_at > generated_at))
    ):
        failures.append("Rybbit browser event timing evidence is invalid")
    if (
        event.get("name") != PROBE_EVENT_NAME
        or event.get("anonymous") is not True
        or _integer(event.get("attribute_count"), default=-1) != 0
    ):
        failures.append("Rybbit browser probe must emit the anonymous launch taxonomy event without attributes")
    privacy = dict(browser.get("privacy") or {})
    for check in REQUIRED_PRIVACY_CHECKS:
        if privacy.get(check) is not True:
            failures.append(f"Rybbit privacy check failed: {check}")
    api = dict(receipt.get("api") or {})
    auth = dict(api.get("auth") or {})
    site = dict(api.get("site") or {})
    has_data = dict(api.get("has_data") or {})
    events = dict(api.get("events") or {})
    if auth.get("kind") != "bearer_api_key" or auth.get("secret_in_receipt") is not False:
        failures.append("Rybbit API proof must use a redacted authenticated API lane")
    for label, row in (("site", site), ("has_data", has_data), ("events", events)):
        if _integer(row.get("status_code"), default=0) != 200 or not re.fullmatch(
            r"[0-9a-f]{64}",
            _text(row.get("response_sha256")),
        ):
            failures.append(f"Rybbit {label} API response evidence is invalid")
        response_size = _integer(row.get("response_size_bytes"), default=-1)
        response_limit = _integer(row.get("response_limit_bytes"), default=-1)
        if (
            response_size <= 0
            or response_limit != MAX_RYBBIT_API_RESPONSE_BYTES
            or response_size > response_limit
            or not _is_json_content_type(row.get("content_type"))
        ):
            failures.append(f"Rybbit {label} API response bounds or content type is invalid")
        requested_url_sha = _text(row.get("requested_url_sha256"))
        final_url_sha = _text(row.get("final_url_sha256"))
        if (
            _origin(row.get("requested_url_origin")) != expected_analytics
            or _origin(row.get("final_url_origin")) != expected_analytics
            or not re.fullmatch(r"[0-9a-f]{64}", requested_url_sha)
            or final_url_sha != requested_url_sha
            or row.get("same_request_url") is not True
            or row.get("redirected") is not False
        ):
            failures.append(f"Rybbit {label} API URL provenance is invalid")
    if site.get("site_id_bound") is not True or has_data.get("has_data") is not True:
        failures.append("Rybbit dashboard API does not prove the expected site has data")
    api_last_seen_at = _parse_datetime(events.get("last_seen_at"))
    if (
        events.get("event_name") != PROBE_EVENT_NAME
        or _integer(events.get("event_count"), default=0) < 1
        or events.get("observed_after_probe") is not True
        or event_sent_at is None
        or api_last_seen_at is None
        or api_last_seen_at < event_sent_at
        or (generated_at is not None and api_last_seen_at > generated_at)
    ):
        failures.append("Rybbit events API did not prove arrival of the protected browser event")
    return failures


def _atomic_write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Produce candidate-bound browser+API Rybbit delivery evidence for PropertyQuarry.")
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--public-origin", required=True)
    parser.add_argument("--analytics-origin", required=True)
    parser.add_argument("--site-id-env", default="PROPERTYQUARRY_RYBBIT_SITE_ID")
    parser.add_argument("--api-key-env", default="PROPERTYQUARRY_RYBBIT_API_KEY")
    parser.add_argument("--site-api-url-env", default="PROPERTYQUARRY_RYBBIT_SITE_API_URL")
    parser.add_argument("--has-data-api-url-env", default="PROPERTYQUARRY_RYBBIT_HAS_DATA_API_URL")
    parser.add_argument("--events-api-url-env", default="PROPERTYQUARRY_RYBBIT_EVENTS_API_URL")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--arrival-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--write", required=True)
    args = parser.parse_args()
    candidate_sha = _text(args.candidate_sha)
    public_origin = _origin(args.public_origin)
    analytics_origin = _origin(args.analytics_origin)
    site_id = _text(os.getenv(str(args.site_id_env)))
    api_key = _text(os.getenv(str(args.api_key_env)))
    required_urls = {
        "site_api_url": _text(os.getenv(str(args.site_api_url_env))),
        "has_data_api_url": _text(os.getenv(str(args.has_data_api_url_env))),
        "events_api_url": _text(os.getenv(str(args.events_api_url_env))),
    }
    timeout_seconds = float(args.timeout_seconds)
    arrival_timeout_seconds = float(args.arrival_timeout_seconds)
    failures: list[str] = []
    if not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        failures.append("candidate SHA must be a full lowercase Git SHA")
    if not public_origin or not analytics_origin:
        failures.append("public and analytics origins must be safe HTTPS origins")
    if not site_id or not api_key:
        failures.append("Rybbit site ID and API key environment bindings are required")
    if any(not value for value in required_urls.values()):
        failures.append("all three Rybbit API URL environment bindings are required")
    elif any(not _same_origin(value, analytics_origin) for value in required_urls.values()):
        failures.append("all three Rybbit API URLs must be HTTPS on the analytics origin")
    if not _positive_finite(timeout_seconds) or not _positive_finite(arrival_timeout_seconds):
        failures.append("Rybbit timeout policies must be finite and positive")
    if failures:
        receipt: dict[str, object] = {
            "schema": RECEIPT_SCHEMA,
            "status": "fail",
            "generated_at": _iso(_utc_now()),
            "candidate_sha": candidate_sha,
            "public_origin": public_origin,
            "analytics_origin": analytics_origin,
            "site_id_sha256": _sha256_text(site_id),
            "failures": failures,
        }
    else:
        try:
            browser = _browser_probe(
                public_origin=public_origin,
                analytics_origin=analytics_origin,
                site_id=site_id,
                timeout_seconds=max(timeout_seconds, 1.0),
            )
            sent_at = _parse_datetime(dict(browser.get("event") or {}).get("sent_at")) or _utc_now()
            api = _api_probe(
                analytics_origin=analytics_origin,
                site_id=site_id,
                api_key=api_key,
                site_api_url=required_urls["site_api_url"],
                has_data_api_url=required_urls["has_data_api_url"],
                events_api_url=required_urls["events_api_url"],
                sent_at=sent_at,
                timeout_seconds=max(timeout_seconds, 1.0),
                arrival_timeout_seconds=max(arrival_timeout_seconds, 1.0),
            )
            receipt = build_receipt(
                candidate_sha=candidate_sha,
                public_origin=public_origin,
                analytics_origin=analytics_origin,
                site_id=site_id,
                browser=browser,
                api=api,
            )
        except Exception as exc:
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "status": "fail",
                "generated_at": _iso(_utc_now()),
                "candidate_sha": candidate_sha,
                "public_origin": public_origin,
                "analytics_origin": analytics_origin,
                "site_id_sha256": _sha256_text(site_id),
                "failures": [type(exc).__name__],
            }
    _atomic_write(Path(args.write), receipt)
    if receipt.get("status") != "pass":
        for failure in list(receipt.get("failures") or []):
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "pass", "receipt": str(args.write), "schema": RECEIPT_SCHEMA}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
