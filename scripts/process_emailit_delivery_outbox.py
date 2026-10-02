#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_HOST = "http://127.0.0.1:8090"
DEFAULT_SENDER_EMAIL = "sprachenzentrum@myexternalbrain.com"
DEFAULT_SENDER_NAME = "Sprachenzentrum"
EMAILIT_API_BASE = "https://api.emailit.com/v2/emails"


def request_json(
    *,
    method: str,
    url: str,
    api_token: str,
    payload: dict[str, object] | None = None,
    principal_id: str | None = None,
) -> object:
    data = None
    headers = {"Content-Type": "application/json"}
    if api_token:
        headers["authorization"] = f"Bearer {api_token}"
    if principal_id:
        headers["x-ea-principal-id"] = principal_id
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"request_failed:{method}:{url}:{exc.code}:{detail[:600]}")
    return json.loads(body)


def emailit_send(
    *,
    api_key: str,
    sender_email: str,
    sender_name: str,
    recipient_email: str,
    subject: str,
    content: str,
    metadata: dict[str, object],
    idempotency_key: str,
) -> dict[str, object]:
    payload = {
        "from": f"{sender_name} <{sender_email}>",
        "to": recipient_email,
        "subject": subject,
        "text": content,
        "html": f"<pre>{content}</pre>",
        "reply_to": sender_email,
        "tracking": False,
        "meta": metadata,
    }
    request = urllib.request.Request(
        EMAILIT_API_BASE,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        },
        method="POST",
    )
    last_error = ""
    for _ in range(7):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read().decode("utf-8", errors="replace")
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last_error = f"{exc.code}:{detail[:600]}"
            if exc.code == 429:
                retry_after = 1
                try:
                    retry_after = int(json.loads(detail).get("retry_after") or 1)
                except Exception:
                    retry_after = 1
                time.sleep(max(1, retry_after))
                continue
            raise RuntimeError(last_error) from exc
    raise RuntimeError(last_error or "emailit_retry_exhausted")


def pending_delivery(host: str, api_token: str, limit: int) -> list[dict[str, object]]:
    url = f"{host.rstrip('/')}/v1/delivery/outbox/pending?{urllib.parse.urlencode({'limit': limit})}"
    body = request_json(method="GET", url=url, api_token=api_token)
    return [dict(row) for row in (body or []) if isinstance(row, dict)]


def connector_bindings(host: str, api_token: str, principal_id: str) -> dict[str, dict[str, object]]:
    url = f"{host.rstrip('/')}/v1/connectors/bindings?{urllib.parse.urlencode({'limit': 500})}"
    body = request_json(method="GET", url=url, api_token=api_token, principal_id=principal_id)
    rows = [dict(row) for row in (body or []) if isinstance(row, dict)]
    return {str(row.get("binding_id") or "").strip(): row for row in rows if str(row.get("binding_id") or "").strip()}


def mark_sent(host: str, api_token: str, delivery_id: str, receipt_json: dict[str, object]) -> dict[str, object]:
    url = f"{host.rstrip('/')}/v1/delivery/outbox/{delivery_id}/sent"
    return dict(
        request_json(
            method="POST",
            url=url,
            api_token=api_token,
            payload={"receipt_json": receipt_json},
        )
        or {}
    )


def mark_failed(host: str, api_token: str, delivery_id: str, error: str, *, dead_letter: bool) -> dict[str, object]:
    url = f"{host.rstrip('/')}/v1/delivery/outbox/{delivery_id}/failed"
    return dict(
        request_json(
            method="POST",
            url=url,
            api_token=api_token,
            payload={"error": error[:1000], "retry_in_seconds": 60, "dead_letter": dead_letter},
        )
        or {}
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Process pending EA email deliveries through Emailit.")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--api-token", default=os.environ.get("EA_API_TOKEN", ""))
    parser.add_argument("--emailit-api-key", default=os.environ.get("EMAILIT_API_KEY", ""))
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--default-from-email", default=os.environ.get("EA_EMAIL_DEFAULT_FROM", DEFAULT_SENDER_EMAIL))
    parser.add_argument("--default-from-name", default=os.environ.get("EA_EMAIL_DEFAULT_NAME", DEFAULT_SENDER_NAME))
    parser.add_argument("--only-principal", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not str(args.api_token or "").strip():
        raise SystemExit("ea_api_token_missing")
    if not str(args.emailit_api_key or "").strip():
        raise SystemExit("emailit_api_key_missing")
    rows = pending_delivery(args.host, args.api_token, args.limit)
    bindings_cache: dict[str, dict[str, dict[str, object]]] = {}
    processed: list[dict[str, object]] = []
    for row in rows:
        if str(row.get("channel") or "").strip().lower() != "email":
            continue
        metadata = dict(row.get("metadata") or {})
        principal_id = str(metadata.get("principal_id") or "").strip()
        if args.only_principal and principal_id != str(args.only_principal or "").strip():
            continue
        binding_id = str(metadata.get("binding_id") or "").strip()
        if not principal_id or not binding_id:
            mark_failed(args.host, args.api_token, str(row.get("delivery_id") or ""), "email_delivery_metadata_missing", dead_letter=False)
            processed.append({"delivery_id": row.get("delivery_id"), "status": "failed_missing_metadata"})
            continue
        if principal_id not in bindings_cache:
            bindings_cache[principal_id] = connector_bindings(args.host, args.api_token, principal_id)
        binding = dict(bindings_cache[principal_id].get(binding_id) or {})
        auth_metadata = dict(binding.get("auth_metadata_json") or {})
        sender_email = str(metadata.get("from_email") or auth_metadata.get("sender_email") or args.default_from_email).strip()
        sender_name = str(metadata.get("from_name") or auth_metadata.get("sender_name") or args.default_from_name).strip()
        subject = str(metadata.get("subject") or "EA delivery").strip() or "EA delivery"
        delivery_id = str(row.get("delivery_id") or "").strip()
        try:
            receipt = emailit_send(
                api_key=str(args.emailit_api_key),
                sender_email=sender_email,
                sender_name=sender_name,
                recipient_email=str(row.get("recipient") or "").strip(),
                subject=subject,
                content=str(row.get("content") or ""),
                metadata={
                    "delivery_id": delivery_id,
                    "principal_id": principal_id,
                    "binding_id": binding_id,
                    "connector_name": str(binding.get("connector_name") or ""),
                },
                idempotency_key=str(row.get("idempotency_key") or f"delivery-{delivery_id}"),
            )
            mark_sent(args.host, args.api_token, delivery_id, receipt_json={"transport": "emailit", "response": receipt})
            processed.append({"delivery_id": delivery_id, "status": "sent", "subject": subject, "emailit_id": receipt.get("id")})
        except Exception as exc:
            updated = mark_failed(
                args.host,
                args.api_token,
                delivery_id,
                str(exc),
                dead_letter=int(row.get("attempt_count") or 0) >= 3,
            )
            processed.append(
                {
                    "delivery_id": delivery_id,
                    "status": str(updated.get("status") or "failed"),
                    "error": str(exc),
                }
            )
    print(json.dumps({"status": "ok", "processed": processed}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
