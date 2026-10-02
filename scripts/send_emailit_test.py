#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


EMAILIT_API_BASE = "https://api.emailit.com/v2/emails"


def _idempotency_key(*, from_email: str, to_email: str, subject: str) -> str:
    seed = json.dumps(
        {
            "from_email": str(from_email or "").strip().lower(),
            "to_email": str(to_email or "").strip().lower(),
            "subject": str(subject or "").strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"propertyquarry-emailit-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send a direct Emailit test email.")
    parser.add_argument("--from-email", required=True, help="Sender address to use.")
    parser.add_argument("--from-name", default="PropertyQuarry", help="Sender display name.")
    parser.add_argument("--to-email", required=True, help="Recipient address.")
    parser.add_argument("--subject", default="PropertyQuarry Emailit test", help="Email subject.")
    parser.add_argument(
        "--text",
        default="This is a direct PropertyQuarry Emailit delivery test.",
        help="Plaintext body.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("EMAILIT_API_KEY", ""),
        help="Emailit API key. Defaults to EMAILIT_API_KEY.",
    )
    return parser.parse_args()


def send_email(*, api_key: str, from_email: str, from_name: str, to_email: str, subject: str, text: str) -> dict[str, object]:
    if not str(api_key or "").strip():
        raise RuntimeError("emailit_api_key_missing")
    payload = {
        "from": f"{from_name} <{from_email}>",
        "to": to_email,
        "subject": subject,
        "text": text,
        "html": f"<pre>{text}</pre>",
        "reply_to": from_email,
        "tracking": False,
        "meta": {
            "kind": "propertyquarry_emailit_test",
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "from_email": from_email,
            "to_email": to_email,
        },
    }
    request = urllib.request.Request(
        EMAILIT_API_BASE,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": _idempotency_key(from_email=from_email, to_email=to_email, subject=subject),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"emailit_http_error:{exc.code}:{detail[:1200]}") from exc
    return dict(json.loads(body or "{}"))


def main() -> int:
    args = parse_args()
    try:
        result = send_email(
            api_key=str(args.api_key or "").strip(),
            from_email=str(args.from_email or "").strip(),
            from_name=str(args.from_name or "").strip() or "PropertyQuarry",
            to_email=str(args.to_email or "").strip(),
            subject=str(args.subject or "").strip() or "PropertyQuarry Emailit test",
            text=str(args.text or "").strip() or "This is a direct PropertyQuarry Emailit delivery test.",
        )
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "sent", "result": result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
