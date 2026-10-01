#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


_MODEL_SID_PATTERN = re.compile(r"^[A-Za-z0-9]{11}$")
_GRAPH_ENDPOINT = "https://my.matterport.com/api/mp/models/graph"
_PREFETCH_QUERY_HASH = "100e6059ba15186f36a825e6224513a77cf3ca0c32be2abe0aee47649abed382"


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _request(url: str, *, timeout_seconds: float, accept: str) -> dict[str, object]:
    request = Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "PropertyQuarry-Launch-Gate/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return {
                "http_status": int(response.status),
                "final_url": response.geturl(),
                "content_type": str(response.headers.get("Content-Type") or ""),
                "body": response.read(),
                "transport_error": "",
            }
    except HTTPError as exc:
        return {
            "http_status": int(exc.code),
            "final_url": exc.geturl(),
            "content_type": str(exc.headers.get("Content-Type") or ""),
            "body": exc.read(),
            "transport_error": "",
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "http_status": 0,
            "final_url": url,
            "content_type": "",
            "body": b"",
            "transport_error": type(exc).__name__,
        }


def _graph_url(model_sid: str) -> str:
    variables = {
        "viewLookup": "default",
        "modelId": model_sid,
        "includeDisabled": False,
        "includeLayers": False,
    }
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": _PREFETCH_QUERY_HASH,
        }
    }
    return f"{_GRAPH_ENDPOINT}?{urlencode({
        'operationName': 'GetModelViewPrefetch',
        'variables': json.dumps(variables, separators=(',', ':')),
        'extensions': json.dumps(extensions, separators=(',', ':')),
    })}"


def _graph_error_rows(payload: dict[str, Any]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in list(payload.get("errors") or []):
        if not isinstance(raw, dict):
            continue
        extensions = dict(raw.get("extensions") or {})
        rows.append(
            {
                "code": str(extensions.get("code") or "unknown"),
                "http_code": int(extensions.get("httpCode") or 0),
                "path": [str(item) for item in list(raw.get("path") or [])],
            }
        )
    return rows


def build_model_availability_receipt(
    *,
    model_sid: str,
    show_probe: dict[str, object],
    graph_probe: dict[str, object],
    graph_payload: dict[str, Any],
    checked_at: str | None = None,
) -> dict[str, object]:
    data = dict(graph_payload.get("data") or {})
    model = dict(data.get("model") or {})
    error_rows = _graph_error_rows(graph_payload)
    graph_model_id = str(model.get("id") or "").strip()
    show_http_status = int(show_probe.get("http_status") or 0)
    graph_http_status = int(graph_probe.get("http_status") or 0)
    model_not_found = any(
        row.get("code") == "not.found" and "model" in list(row.get("path") or [])
        for row in error_rows
    )
    view_not_found = any(
        row.get("code") == "not.found" and "view" in list(row.get("path") or [])
        for row in error_rows
    )
    blockers: list[str] = []
    if show_http_status != 200:
        blockers.append("matterport_show_page_unavailable")
    if str(show_probe.get("transport_error") or ""):
        blockers.append("matterport_show_transport_error")
    if graph_http_status != 200:
        blockers.append("matterport_model_lookup_unavailable")
    if str(graph_probe.get("transport_error") or ""):
        blockers.append("matterport_model_lookup_transport_error")
    if model_not_found or not graph_model_id:
        blockers.append("matterport_model_not_found")
    elif graph_model_id != model_sid:
        blockers.append("matterport_model_sid_mismatch")
    if view_not_found:
        blockers.append("matterport_default_view_not_found")
    blockers = list(dict.fromkeys(blockers))
    model_available = not blockers
    return {
        "contract_name": "propertyquarry.matterport_model_availability.v1",
        "checked_at": checked_at or _utc_now(),
        "status": "pass" if model_available else "blocked",
        "model_sid": model_sid,
        "model_available": model_available,
        "public_url": f"https://my.matterport.com/show/?m={model_sid}",
        "show_http_status": show_http_status,
        "show_final_url": str(show_probe.get("final_url") or ""),
        "show_content_type": str(show_probe.get("content_type") or ""),
        "graph_http_status": graph_http_status,
        "graph_model_id": graph_model_id,
        "graph_error_rows": error_rows,
        "blockers": blockers,
        "truth_boundary": (
            "This receipt proves current public model lookup only. It does not prove sweep topology, "
            "route coverage, SDK motion quality, or launch readiness."
        ),
    }


def probe_model_availability(model_sid: str, *, timeout_seconds: float = 30.0) -> dict[str, object]:
    normalized_sid = str(model_sid or "").strip()
    if not _MODEL_SID_PATTERN.fullmatch(normalized_sid):
        raise ValueError("matterport_model_sid_invalid")
    show_probe = _request(
        f"https://my.matterport.com/show/?m={normalized_sid}",
        timeout_seconds=timeout_seconds,
        accept="text/html,application/xhtml+xml",
    )
    graph_probe = _request(
        _graph_url(normalized_sid),
        timeout_seconds=timeout_seconds,
        accept="application/json",
    )
    try:
        graph_payload = json.loads(bytes(graph_probe.get("body") or b"").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        graph_payload = {}
    if not isinstance(graph_payload, dict):
        graph_payload = {}
    return build_model_availability_receipt(
        model_sid=normalized_sid,
        show_probe=show_probe,
        graph_probe=graph_probe,
        graph_payload=dict(graph_payload),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe current public Matterport model availability.")
    parser.add_argument("--model-sid", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--write", required=True)
    args = parser.parse_args()

    receipt = probe_model_availability(
        str(args.model_sid),
        timeout_seconds=max(1.0, float(args.timeout_seconds)),
    )
    output_path = Path(args.write).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt.get("status") == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
