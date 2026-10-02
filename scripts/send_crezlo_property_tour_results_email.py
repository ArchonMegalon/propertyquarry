#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


EMAILIT_API_BASE = "https://api.emailit.com/v2/emails"
DEFAULT_SENDER_EMAIL = "sprachenzentrum@myexternalbrain.com"
DEFAULT_SENDER_NAME = "Sprachenzentrum"
VARIANT_ORDER = {
    "layout_first": 0,
    "light_and_view": 1,
    "shortlist_comparison": 2,
}


def load_json(path: Path) -> object:
    return json.loads(path.read_text())


def load_packets(path: Path) -> dict[str, dict[str, object]]:
    raw = load_json(path)
    if not isinstance(raw, list):
        raise SystemExit(f"packet_file_invalid:{path}")
    packets: dict[str, dict[str, object]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        listing_id = str(entry.get("listing_id") or "").strip()
        if listing_id:
            packets[listing_id] = entry
    return packets


def load_manifest_rows(paths: list[Path]) -> dict[str, dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for path in paths:
        raw = load_json(path)
        if not isinstance(raw, list):
            raise SystemExit(f"manifest_invalid:{path}")
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            run_key = str(entry.get("run_key") or "").strip()
            if run_key:
                merged[run_key] = entry
    return merged


def load_public_index(path: Path | None) -> dict[tuple[str, str], dict[str, object]]:
    if path is None:
        return {}
    raw = load_json(path)
    if not isinstance(raw, list):
        raise SystemExit(f"public_index_invalid:{path}")
    rows: dict[tuple[str, str], dict[str, object]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        listing_id = str(entry.get("listing_id") or "").strip()
        variant_key = str(entry.get("variant_key") or "").strip()
        hosted_url = str(entry.get("hosted_url") or "").strip()
        if listing_id and variant_key and hosted_url:
            rows[(listing_id, variant_key)] = dict(entry)
    return rows


def variant_metadata(packet: dict[str, object], variant_key: str) -> dict[str, object]:
    raw = packet.get("tour_variants_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and str(entry.get("variant_key") or "").strip() == variant_key:
                return dict(entry)
    return {}


def property_facts(packet: dict[str, object]) -> dict[str, object]:
    raw = packet.get("property_facts_json")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    return dict(raw or {})


def variant_rank(value: str) -> int:
    return VARIANT_ORDER.get(str(value or "").strip(), 999)


def build_subject(*, listing_id: str, title: str, variant_key: str) -> str:
    compact_title = " ".join(str(title or "").split())
    variant_label = variant_key.replace("_", " ")
    return f"[EA Property Tour] {listing_id} · {variant_label} · {compact_title}"[:220]


def build_text_body(
    *,
    packet: dict[str, object],
    variant: dict[str, object],
    row: dict[str, object],
) -> str:
    facts = property_facts(packet)
    public_browser_url = str(row.get("hosted_url") or row.get("public_url") or "").strip()
    lines = [
        f"Property: {packet.get('title') or ''}",
        f"Listing ID: {packet.get('listing_id') or ''}",
        f"Variant: {row.get('variant_key') or ''}",
        f"Listing URL: {packet.get('property_url') or ''}",
        f"Tour ID: {row.get('tour_id') or ''}",
        f"Public Tour URL: {public_browser_url}",
        f"Crezlo URL: {row.get('public_url') or ''}",
        f"Editor URL: {row.get('editor_url') or ''}",
        "",
        "Property facts:",
        f"- Size: {facts.get('living_area_sqm') or '?'} m²",
        f"- Rooms: {facts.get('rooms') or '?'}",
        f"- Total rent EUR: {facts.get('total_rent_eur') or '?'}",
        f"- Availability: {facts.get('availability_text') or '?'}",
        "",
        "Tour steering:",
        f"- Scene strategy: {variant.get('scene_strategy') or ''}",
        f"- Theme: {variant.get('theme_name') or ''}",
        f"- Tour style: {variant.get('tour_style') or ''}",
        f"- Audience: {variant.get('audience') or ''}",
        f"- Creative brief: {variant.get('creative_brief') or ''}",
        f"- CTA: {variant.get('call_to_action') or ''}",
    ]
    return "\n".join(lines).strip() + "\n"


def build_html_body(
    *,
    packet: dict[str, object],
    variant: dict[str, object],
    row: dict[str, object],
) -> str:
    facts = property_facts(packet)
    public_browser_url = str(row.get("hosted_url") or row.get("public_url") or "").strip()
    return f"""
<html>
  <body>
    <h2>{packet.get('title') or ''}</h2>
    <p><strong>Listing ID:</strong> {packet.get('listing_id') or ''}<br>
    <strong>Variant:</strong> {row.get('variant_key') or ''}<br>
    <strong>Listing:</strong> <a href="{packet.get('property_url') or ''}">{packet.get('property_url') or ''}</a><br>
    <strong>Tour ID:</strong> {row.get('tour_id') or ''}<br>
    <strong>Public Tour URL:</strong> <a href="{public_browser_url}">{public_browser_url}</a><br>
    <strong>Crezlo URL:</strong> <a href="{row.get('public_url') or ''}">{row.get('public_url') or ''}</a><br>
    <strong>Editor URL:</strong> <a href="{row.get('editor_url') or ''}">{row.get('editor_url') or ''}</a></p>
    <h3>Property Facts</h3>
    <ul>
      <li>Size: {facts.get('living_area_sqm') or '?'} m²</li>
      <li>Rooms: {facts.get('rooms') or '?'}</li>
      <li>Total rent EUR: {facts.get('total_rent_eur') or '?'}</li>
      <li>Availability: {facts.get('availability_text') or '?'}</li>
    </ul>
    <h3>Tour Steering</h3>
    <ul>
      <li>Scene strategy: {variant.get('scene_strategy') or ''}</li>
      <li>Theme: {variant.get('theme_name') or ''}</li>
      <li>Tour style: {variant.get('tour_style') or ''}</li>
      <li>Audience: {variant.get('audience') or ''}</li>
      <li>Creative brief: {variant.get('creative_brief') or ''}</li>
      <li>CTA: {variant.get('call_to_action') or ''}</li>
    </ul>
  </body>
</html>
""".strip()


def send_email(
    *,
    api_key: str,
    idempotency_key: str,
    sender_name: str,
    sender_email: str,
    recipient_email: str,
    subject: str,
    text_body: str,
    html_body: str,
    metadata: dict[str, object],
) -> dict[str, object]:
    payload = {
        "from": f"{sender_name} <{sender_email}>",
        "to": recipient_email,
        "subject": subject,
        "text": text_body,
        "html": html_body,
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
    for attempt in range(1, 8):
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
                    payload = json.loads(detail)
                    retry_after = int(payload.get("retry_after") or retry_after)
                except Exception:
                    retry_after = 1
                time.sleep(max(1, retry_after))
                continue
            raise SystemExit(f"emailit_send_failed:{last_error}")
    raise SystemExit(f"emailit_send_failed:{last_error or 'rate_limit_retry_exhausted'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Send Crezlo property tour result emails through Emailit.")
    parser.add_argument("--packets", required=True, help="Path to willhaben_property_packet JSON.")
    parser.add_argument(
        "--manifest",
        action="append",
        required=True,
        help="One or more manifest JSON files. Later files override earlier rows by run_key.",
    )
    parser.add_argument("--to", required=True, help="Recipient email address.")
    parser.add_argument("--from-email", default=os.environ.get("EA_EMAIL_DEFAULT_FROM", DEFAULT_SENDER_EMAIL))
    parser.add_argument("--from-name", default=os.environ.get("EA_EMAIL_DEFAULT_NAME", DEFAULT_SENDER_NAME))
    parser.add_argument("--output", required=True, help="Path to write send receipts JSON.")
    parser.add_argument("--public-index", default="", help="Optional hosted public-tour index JSON.")
    parser.add_argument("--idempotency-prefix", default="crezlo-tour-result", help="Emailit idempotency key prefix.")
    parser.add_argument("--api-key", default=os.environ.get("EMAILIT_API_KEY", ""))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = str(args.api_key or "").strip()
    if not api_key:
        raise SystemExit("emailit_api_key_missing")
    packets = load_packets(Path(args.packets))
    rows = load_manifest_rows([Path(value) for value in args.manifest])
    public_index = load_public_index(Path(args.public_index)) if str(args.public_index or "").strip() else {}
    ordered_rows = sorted(
        rows.values(),
        key=lambda row: (
            str(row.get("listing_id") or ""),
            variant_rank(str(row.get("variant_key") or "")),
            str(row.get("run_key") or ""),
        ),
    )
    receipts: list[dict[str, object]] = []
    for row in ordered_rows:
        listing_id = str(row.get("listing_id") or "").strip()
        if not listing_id:
            continue
        packet = packets.get(listing_id)
        if not packet:
            raise SystemExit(f"packet_missing_for_listing:{listing_id}")
        variant_key = str(row.get("variant_key") or "").strip()
        public_row = public_index.get((listing_id, variant_key))
        if public_row:
            row = {**public_row, **row}
        variant = variant_metadata(packet, variant_key)
        subject = build_subject(listing_id=listing_id, title=str(packet.get("title") or ""), variant_key=variant_key)
        text_body = build_text_body(packet=packet, variant=variant, row=row)
        html_body = build_html_body(packet=packet, variant=variant, row=row)
        run_key = str(row.get("run_key") or f"{listing_id}__{variant_key}")
        response = send_email(
            api_key=api_key,
            idempotency_key=f"{str(args.idempotency_prefix or 'crezlo-tour-result').strip() or 'crezlo-tour-result'}-{run_key}",
            sender_name=str(args.from_name or "EA").strip() or "EA",
            sender_email=str(args.from_email or "").strip(),
            recipient_email=str(args.to or "").strip(),
            subject=subject,
            text_body=text_body,
            html_body=html_body,
            metadata={
                "source": "crezlo_property_tour_batch",
                "run_key": run_key,
                "listing_id": listing_id,
                "variant_key": variant_key,
                "tour_id": row.get("tour_id"),
            },
        )
        public_browser_url = str(row.get("hosted_url") or row.get("public_url") or "").strip()
        receipts.append(
            {
                "run_key": run_key,
                "listing_id": listing_id,
                "variant_key": variant_key,
                "subject": subject,
                "tour_id": row.get("tour_id"),
                "public_browser_url": public_browser_url,
                "hosted_url": row.get("hosted_url"),
                "public_url": row.get("public_url"),
                "editor_url": row.get("editor_url"),
                "emailit_response": response,
            }
        )
        time.sleep(0.6)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(receipts, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": "ok", "count": len(receipts), "output": str(output_path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
