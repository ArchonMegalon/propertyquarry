#!/usr/bin/env python3
"""Produce a bounded, credential-free observation of the public origin."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import stat
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parents[1]
for candidate in (ROOT, ROOT / "ea"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from scripts.propertyquarry_secure_file_io import atomic_write_bytes
from scripts.propertyquarry_strict_json import load_strict_json_object_snapshot


SCHEMA = "propertyquarry.ooda_public_origin_observation.v1"
HEALTH_SCHEMA = "propertyquarry.ooda_public_origin_observation_health.v1"
DEFAULT_ORIGIN = "https://propertyquarry.com"
DEFAULT_RECEIPT_PATH = Path(
    "_completion/smoke/property-public-origin-observation-latest.json"
)
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_INTERVAL_SECONDS = 300.0
MIN_INTERVAL_SECONDS = 60.0
MAX_INTERVAL_SECONDS = 1800.0
DEFAULT_HEALTH_MAX_AGE_SECONDS = 900.0
MAX_RECEIPT_BYTES = 65_536
PROBE_USER_AGENT = "PropertyQuarry-live-mobile-surface-smoke/1.0"
PUBLIC_PROBE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": PROBE_USER_AGENT,
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _observed_now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def normalized_public_origin(value: object) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("public_origin_not_admissible") from exc
    hostname = str(parsed.hostname or "").strip().lower().rstrip(".")
    if not (
        parsed.scheme.lower() == "https"
        and hostname
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
        and port in {None, 443}
    ):
        raise ValueError("public_origin_not_admissible")
    netloc = hostname if port is None else f"{hostname}:{port}"
    return urlunsplit(("https", netloc, "", "", ""))


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode("utf-8")


def _http_get_for_observation(
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
    follow_redirects: bool,
    authorized_origin: str,
) -> dict[str, Any]:
    origin = normalized_public_origin(authorized_origin)
    if follow_redirects or url != f"{origin}/":
        raise ValueError("public_origin_request_scope_not_admissible")
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=timeout_seconds) as response:
            body = response.read(MAX_RECEIPT_BYTES)
            return {
                "status_code": int(getattr(response, "status", 0) or 0),
                "headers": dict(response.headers.items()),
                "text": body.decode("utf-8", errors="replace"),
            }
    except HTTPError as exc:
        body = exc.read(MAX_RECEIPT_BYTES)
        return {
            "status_code": int(exc.code or 0),
            "headers": dict(exc.headers.items()),
            "text": body.decode("utf-8", errors="replace"),
        }


def _header_value(headers: dict[str, Any], name: str) -> str:
    normalized_name = name.strip().lower()
    for key, value in headers.items():
        if str(key).strip().lower() == normalized_name:
            return str(value or "").strip()
    return ""


def cloudflare_tunnel_failure_from_response(response: dict[str, Any]) -> bool:
    try:
        status_code = int(response.get("status_code") or 0)
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(
        status_code == 530
        and _header_value(
            dict(response.get("headers") or {}), "server"
        ).lower()
        == "cloudflare"
        and re.search(
            r"\berror\s*code\s*:\s*1033\b|\berror\s+1033\b",
            str(response.get("text") or ""),
            flags=re.IGNORECASE,
        )
    )


def _normalized_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def sanitize_public_origin_observation(
    payload: dict[str, Any],
) -> dict[str, Any]:
    source = dict(payload)
    expected_keys = {
        "schema",
        "generated_at",
        "status",
        "origin",
        "request",
        "observation",
        "response_content_recorded",
        "response_headers_recorded",
        "action_required",
        "interrupt_operator",
        "automatic_execution_allowed",
        "execution_authorized",
        "deployment_or_restart_authorized",
        "protected_operation_executed",
        "provider_quota_consumption_allowed",
        "delivery_authorized",
        "delivery_attempted",
        "sent",
    }
    request = source.get("request")
    observed = source.get("observation")
    status_value = str(source.get("status") or "").strip()
    generated_at = _normalized_timestamp(source.get("generated_at"))
    try:
        origin = normalized_public_origin(source.get("origin"))
    except ValueError as exc:
        raise ValueError("public_origin_observation_not_admissible") from exc
    if not (
        set(source) == expected_keys
        and source.get("schema") == SCHEMA
        and generated_at
        and status_value in {"blocked", "reachable", "indeterminate"}
        and isinstance(request, dict)
        and request
        == {
            "method": "GET",
            "path": "/",
            "credentials_sent": False,
            "redirects_followed": False,
            "tls_validation": "system_trust_store",
        }
        and isinstance(observed, dict)
        and set(observed)
        == {"http_status", "edge_provider", "error_code", "reason"}
        and all(
            source.get(key) is False
            for key in (
                "response_content_recorded",
                "response_headers_recorded",
                "action_required",
                "interrupt_operator",
                "automatic_execution_allowed",
                "execution_authorized",
                "deployment_or_restart_authorized",
                "protected_operation_executed",
                "provider_quota_consumption_allowed",
                "delivery_authorized",
                "delivery_attempted",
                "sent",
            )
        )
    ):
        raise ValueError("public_origin_observation_not_admissible")
    try:
        http_status = int(observed.get("http_status"))
        error_code = int(observed.get("error_code"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("public_origin_observation_not_admissible") from exc
    if isinstance(observed.get("http_status"), bool) or isinstance(
        observed.get("error_code"), bool
    ):
        raise ValueError("public_origin_observation_not_admissible")
    normalized_observation = {
        "http_status": http_status,
        "edge_provider": str(observed.get("edge_provider") or "").strip(),
        "error_code": error_code,
        "reason": str(observed.get("reason") or "").strip(),
    }
    if status_value == "blocked":
        expected_observation = {
            "http_status": 530,
            "edge_provider": "cloudflare",
            "error_code": 1033,
            "reason": "cloudflare_tunnel_unavailable",
        }
    elif status_value == "reachable":
        expected_observation = {
            "http_status": http_status,
            "edge_provider": "",
            "error_code": 0,
            "reason": "public_origin_reachable",
        }
    else:
        expected_observation = {
            "http_status": http_status,
            "edge_provider": "",
            "error_code": 0,
            "reason": str(observed.get("reason") or "").strip(),
        }
    if (
        normalized_observation != expected_observation
        or not 0 <= http_status <= 599
        or (status_value == "reachable" and not 200 <= http_status < 400)
        or (
            status_value == "indeterminate"
            and normalized_observation["reason"]
            not in {"network_error", "unexpected_http_status"}
        )
    ):
        raise ValueError("public_origin_observation_not_admissible")
    return {
        **source,
        "generated_at": generated_at,
        "origin": origin,
        "request": dict(request),
        "observation": normalized_observation,
    }


def build_public_origin_observation(
    *,
    origin: str = DEFAULT_ORIGIN,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    now: datetime | None = None,
    http_get: Callable[..., dict[str, Any]] = _http_get_for_observation,
) -> dict[str, Any]:
    normalized_origin = normalized_public_origin(origin)
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("public_origin_timeout_not_admissible") from exc
    if not math.isfinite(timeout) or not 1.0 <= timeout <= 30.0:
        raise ValueError("public_origin_timeout_not_admissible")

    status = "indeterminate"
    observation: dict[str, Any] = {
        "http_status": 0,
        "edge_provider": "",
        "error_code": 0,
        "reason": "network_error",
    }
    try:
        response = http_get(
            f"{normalized_origin}/",
            headers=dict(PUBLIC_PROBE_HEADERS),
            timeout_seconds=timeout,
            follow_redirects=False,
            authorized_origin=normalized_origin,
        )
        http_status = int(response.get("status_code") or 0)
        edge_failure = cloudflare_tunnel_failure_from_response(response)
        if edge_failure:
            status = "blocked"
            observation = {
                "http_status": 530,
                "edge_provider": "cloudflare",
                "error_code": 1033,
                "reason": "cloudflare_tunnel_unavailable",
            }
        elif 200 <= http_status < 400:
            status = "reachable"
            observation = {
                "http_status": http_status,
                "edge_provider": "",
                "error_code": 0,
                "reason": "public_origin_reachable",
            }
        else:
            observation = {
                "http_status": max(0, min(http_status, 599)),
                "edge_provider": "",
                "error_code": 0,
                "reason": "unexpected_http_status",
            }
    except Exception:
        # Error text is deliberately not retained: it can contain addresses,
        # proxy details, or other environment-specific data.
        pass

    return {
        "schema": SCHEMA,
        "generated_at": _observed_now(now).isoformat(),
        "status": status,
        "origin": normalized_origin,
        "request": {
            "method": "GET",
            "path": "/",
            "credentials_sent": False,
            "redirects_followed": False,
            "tls_validation": "system_trust_store",
        },
        "observation": observation,
        "response_content_recorded": False,
        "response_headers_recorded": False,
        "action_required": False,
        "interrupt_operator": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def write_public_origin_observation(
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    origin: str = DEFAULT_ORIGIN,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    now: datetime | None = None,
    http_get: Callable[..., dict[str, Any]] = _http_get_for_observation,
) -> dict[str, Any]:
    receipt = build_public_origin_observation(
        origin=origin,
        timeout_seconds=timeout_seconds,
        now=now,
        http_get=http_get,
    )
    atomic_write_bytes(
        Path(os.path.abspath(os.fspath(receipt_path.expanduser()))),
        canonical_json_bytes(receipt),
        overwrite=True,
    )
    return receipt


def inspect_public_origin_observation_receipt(
    *,
    receipt_path: Path = DEFAULT_RECEIPT_PATH,
    max_age_seconds: float = DEFAULT_HEALTH_MAX_AGE_SECONDS,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = Path(os.path.abspath(os.fspath(receipt_path.expanduser())))
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("public_origin_observation_receipt_not_admissible")
    payload, _raw, digest = load_strict_json_object_snapshot(
        path,
        field="public_origin_observation_receipt",
        maximum_bytes=MAX_RECEIPT_BYTES,
    )
    projection = sanitize_public_origin_observation(payload)
    try:
        age_limit = float(max_age_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("public_origin_health_max_age_not_admissible") from exc
    generated_at = datetime.fromisoformat(
        str(projection["generated_at"]).replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    age_seconds = (_observed_now(now) - generated_at).total_seconds()
    if not (
        math.isfinite(age_limit)
        and 60.0 <= age_limit <= 86400.0
        and math.isfinite(age_seconds)
        and -30.0 <= age_seconds <= age_limit
    ):
        raise ValueError("public_origin_observation_receipt_not_fresh")
    return {
        "schema": HEALTH_SCHEMA,
        "status": "ready",
        "updated_at": _observed_now(now).isoformat(),
        "blocking_reason": "",
        "source_generated_at": generated_at.isoformat(),
        "source_status": str(projection["status"]),
        "receipt_sha256": digest,
        "progress": {
            "receipt_integrity_verified": True,
            "source_contract_verified": True,
            "source_freshness_verified": True,
            "current_evidence_verified": True,
        },
        "action_required": False,
        "interrupt_operator": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "deployment_or_restart_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
    }


def run_observation_daemon(
    *,
    receipt_path: Path,
    origin: str,
    timeout_seconds: float,
    interval_seconds: float,
    stop_event: threading.Event | None = None,
) -> int:
    try:
        interval = float(interval_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("public_origin_interval_not_admissible") from exc
    if not math.isfinite(interval) or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS:
        raise ValueError("public_origin_interval_not_admissible")
    event = stop_event or threading.Event()
    while not event.is_set():
        write_public_origin_observation(
            receipt_path=receipt_path,
            origin=origin,
            timeout_seconds=timeout_seconds,
        )
        if event.wait(interval):
            break
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Write a credential-free, content-free observation of a public HTTPS origin."
        )
    )
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--write", type=Path, default=DEFAULT_RECEIPT_PATH)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true")
    mode.add_argument("--health", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument(
        "--max-age-seconds",
        type=float,
        default=DEFAULT_HEALTH_MAX_AGE_SECONDS,
    )
    args = parser.parse_args(argv)

    if args.health:
        try:
            report = inspect_public_origin_observation_receipt(
                receipt_path=args.write,
                max_age_seconds=args.max_age_seconds,
            )
        except Exception as exc:
            report = {
                "schema": HEALTH_SCHEMA,
                "status": "blocked",
                "updated_at": _observed_now().isoformat(),
                "blocking_reason": "public_origin_observation_health_failed",
                "error_type": type(exc).__name__,
                "progress": {"current_evidence_verified": False},
                "action_required": False,
                "interrupt_operator": False,
                "automatic_execution_allowed": False,
                "execution_authorized": False,
                "deployment_or_restart_authorized": False,
                "protected_operation_executed": False,
                "provider_quota_consumption_allowed": False,
                "delivery_authorized": False,
                "delivery_attempted": False,
                "sent": False,
            }
            print(json.dumps(report, sort_keys=True))
            return 1
        print(json.dumps(report, sort_keys=True))
        return 0

    if args.daemon:
        stop_event = threading.Event()

        def stop(_signum: int, _frame: object) -> None:
            stop_event.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        return run_observation_daemon(
            receipt_path=args.write,
            origin=args.origin,
            timeout_seconds=args.timeout_seconds,
            interval_seconds=args.interval_seconds,
            stop_event=stop_event,
        )

    receipt = write_public_origin_observation(
        receipt_path=args.write,
        origin=args.origin,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
