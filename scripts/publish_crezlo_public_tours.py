#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EA_DIR = ROOT / "ea"
if str(EA_DIR) not in sys.path:
    sys.path.insert(0, str(EA_DIR))

from browseract_ui_media import compose_slideshow_video, transcode_video_webm
from app.api.routes.public_tour_payloads import PrivateTourReceipt, build_public_tour_manifest
from property_tour_publication_gate import require_verified_crezlo_publication


DEFAULT_OUTPUT_DIR = Path("/docker/property/state/public_property_tours/crezlo")
DEFAULT_PUBLIC_BASE_URL = str(os.environ.get("PROPERTYQUARRY_PUBLIC_TOUR_BASE_URL", "https://propertyquarry.com/tours")).strip().rstrip("/")
VARIANT_ORDER = {
    "layout_first": 0,
    "light_and_view": 1,
    "shortlist_comparison": 2,
}


def load_json(path: Path) -> object:
    return json.loads(path.read_text())


def download_bytes(url: str) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "PropertyQuarry-Crezlo-Public-Tour/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.read(), str(response.headers.get("Content-Type") or "").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"tour_asset_http_error:{exc.code}:{detail[:240]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"tour_asset_transport_error:{exc.reason}") from exc


def guess_ext(*, url: str, content_type: str) -> str:
    guessed = mimetypes.guess_extension((content_type or "").split(";", 1)[0].strip())
    if guessed:
        return guessed
    suffix = Path(urllib.parse.urlparse(url).path).suffix
    return suffix or ".bin"


def coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except Exception:
            return {}
        if isinstance(loaded, dict):
            return dict(loaded)
    return {}


def coerce_list(value: object) -> list[object]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except Exception:
            return []
        if isinstance(loaded, list):
            return list(loaded)
    return []


def expanded_structured(value: object) -> dict[str, object]:
    base = coerce_dict(value)
    raw_text = str(base.get("raw_text") or "").strip()
    if raw_text.startswith("{") and raw_text.endswith("}"):
        try:
            loaded = json.loads(raw_text)
        except Exception:
            loaded = {}
        if isinstance(loaded, dict):
            merged = dict(loaded)
            merged.update(base)
            if "raw_text" in base:
                merged["raw_text"] = base["raw_text"]
            return merged
    return base


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
            packets[listing_id] = dict(entry)
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
                merged[run_key] = dict(entry)
    return merged


def variant_metadata(packet: dict[str, object], variant_key: str) -> dict[str, object]:
    variants = coerce_list(packet.get("tour_variants_json"))
    for entry in variants:
        if isinstance(entry, dict) and str(entry.get("variant_key") or "").strip() == variant_key:
            return dict(entry)
    return {}


def scene_rows(structured: dict[str, object]) -> list[dict[str, object]]:
    detail = coerce_dict(structured.get("tour_detail_json"))
    scenes = coerce_list(detail.get("scenes"))
    rows: list[dict[str, object]] = []
    for ordinal, entry in enumerate(scenes, start=1):
        if not isinstance(entry, dict):
            continue
        file_json = coerce_dict(entry.get("file"))
        image_url = str(file_json.get("path") or "").strip()
        if not image_url:
            continue
        meta = coerce_dict(file_json.get("meta"))
        rows.append(
            {
                "ordinal": ordinal,
                "scene_id": str(entry.get("id") or "").strip(),
                "name": str(entry.get("name") or file_json.get("name") or f"scene-{ordinal}").strip(),
                "image_url": image_url,
                "role": str(meta.get("role") or "photo").strip() or "photo",
                "source_url": str(meta.get("source_url") or image_url).strip(),
                "mime_type": str(file_json.get("mime_type") or "").strip(),
            }
        )
    if rows:
        return rows
    fallback = coerce_list(structured.get("file_records_json"))
    for ordinal, entry in enumerate(fallback, start=1):
        if not isinstance(entry, dict):
            continue
        image_url = str(entry.get("path") or "").strip()
        if not image_url:
            continue
        meta = coerce_dict(entry.get("meta"))
        rows.append(
            {
                "ordinal": ordinal,
                "scene_id": str(entry.get("id") or "").strip(),
                "name": str(entry.get("name") or f"scene-{ordinal}").strip(),
                "image_url": image_url,
                "role": str(meta.get("role") or "photo").strip() or "photo",
                "source_url": str(meta.get("source_url") or image_url).strip(),
                "mime_type": str(entry.get("mime_type") or "").strip(),
            }
        )
    return rows


def write_scene_assets(target_dir: Path, scenes: list[dict[str, object]]) -> list[dict[str, object]]:
    published: list[dict[str, object]] = []
    for row in scenes:
        image_url = str(row.get("image_url") or "").strip()
        if not image_url:
            continue
        content, content_type = download_bytes(image_url)
        filename = f"scene-{int(row.get('ordinal') or len(published) + 1):02d}{guess_ext(url=image_url, content_type=content_type or str(row.get('mime_type') or ''))}"
        asset_path = target_dir / filename
        asset_path.write_bytes(content)
        published.append(
            {
                **row,
                "asset_relpath": filename,
                "mime_type": content_type.split(";", 1)[0].strip() or str(row.get("mime_type") or "").strip(),
            }
        )
    return published


def compose_tour_video(target_dir: Path, scenes: list[dict[str, object]], *, variant_key: str) -> dict[str, object]:
    image_paths = [target_dir / str(row.get("asset_relpath") or "") for row in scenes if str(row.get("asset_relpath") or "").strip()]
    subtitle_lines: list[str] = []
    for row in scenes:
        role = str(row.get("role") or "photo").strip()
        name = str(row.get("name") or "").strip()
        subtitle_lines.append(f"{role.title()} · {name}" if name else role.title())
    output_path = target_dir / "tour.mp4"
    srt_path = target_dir / "tour-subtitles.srt"
    aspect_width, aspect_height = (1080, 1920) if variant_key == "light_and_view" else (1280, 720)
    meta = compose_slideshow_video(
        image_paths,
        output_path,
        subtitle_lines=subtitle_lines,
        width=aspect_width,
        height=aspect_height,
        scene_seconds=2.5,
        transition_seconds=0.30,
        subtitle_srt_path=srt_path,
    )
    browser_path = target_dir / "tour.webm"
    transcode_video_webm(output_path, browser_path)
    meta["browser_path"] = str(browser_path)
    meta["browser_relpath"] = browser_path.name
    meta["fallback_relpath"] = output_path.name
    return meta


def public_tour_bundle_payload(*, payload: dict[str, object], bundle_dir: Path) -> dict[str, object]:
    slug = str(payload.get("slug") or "").strip()
    return build_public_tour_manifest(
        payload,
        expose_asset_relpaths=True,
        url_allowed=lambda _url: False,
        bundle_dir_resolver=lambda requested_slug: bundle_dir if str(requested_slug or "").strip() == slug else None,
    ).as_dict()


def private_tour_receipt(payload: dict[str, object]) -> dict[str, object]:
    return PrivateTourReceipt.from_payload(payload).as_dict()


def sort_rows(rows: dict[str, dict[str, object]]) -> list[dict[str, object]]:
    def key(row: dict[str, object]) -> tuple[str, int, str]:
        variant_key = str(row.get("variant_key") or "").strip()
        return (
            str(row.get("listing_id") or "").strip(),
            VARIANT_ORDER.get(variant_key, 999),
            str(row.get("run_key") or "").strip(),
        )

    return sorted(rows.values(), key=key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish browser-viewable property tour snapshots from Crezlo run artifacts.")
    parser.add_argument("--packets", required=True)
    parser.add_argument("--manifest", action="append", required=True)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--public-base-url", default=DEFAULT_PUBLIC_BASE_URL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    packets = load_packets(Path(args.packets))
    rows = load_manifest_rows([Path(value) for value in args.manifest])
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    index_rows: list[dict[str, object]] = []
    for row in sort_rows(rows):
        listing_id = str(row.get("listing_id") or "").strip()
        packet = packets.get(listing_id)
        if packet is None:
            raise SystemExit(f"packet_missing:{listing_id}")
        run_path = Path(str(row.get("path") or "").strip())
        run_json = coerce_dict(load_json(run_path))
        output_json = coerce_dict(run_json.get("output_json"))
        structured = expanded_structured(output_json.get("structured_output_json"))
        require_verified_crezlo_publication(structured)
        slug = str(structured.get("slug") or row.get("run_key") or "").strip()
        if not slug:
            raise SystemExit(f"slug_missing:{run_path}")
        variant_key = str(row.get("variant_key") or structured.get("variant_key") or "").strip()
        variant = variant_metadata(packet, variant_key)
        facts = coerce_dict(packet.get("property_facts_json"))
        scenes = scene_rows(structured)
        target_dir = output_dir / slug
        target_dir.mkdir(parents=True, exist_ok=True)
        hosted_url = f"{str(args.public_base_url).rstrip('/')}/{slug}"
        published_scenes = write_scene_assets(target_dir, scenes)
        video_meta = compose_tour_video(target_dir, published_scenes, variant_key=variant_key) if published_scenes else {}
        full_payload = {
            "slug": slug,
            "hosted_url": hosted_url,
            "run_key": str(row.get("run_key") or "").strip(),
            "listing_id": listing_id,
            "listing_url": str(packet.get("property_url") or "").strip(),
            "property_url": str(packet.get("property_url") or "").strip(),
            "source_ref": listing_id,
            "external_id": str(row.get("run_key") or "").strip(),
            "title": str(packet.get("title") or structured.get("tour_title") or "").strip(),
            "display_title": str(coerce_dict(structured.get("tour_detail_json")).get("display_title") or "").strip(),
            "tour_title": str(structured.get("tour_title") or "").strip(),
            "tour_id": str(structured.get("tour_id") or row.get("tour_id") or "").strip(),
            "variant_key": variant_key,
            "variant_label": variant_key.replace("_", " "),
            "scene_strategy": str(structured.get("scene_strategy") or variant.get("scene_strategy") or "").strip(),
            "scene_count": len(published_scenes),
            "video_relpath": str(video_meta.get("browser_relpath") or "") if video_meta else "",
            "video_fallback_relpath": str(video_meta.get("fallback_relpath") or "") if video_meta else "",
            "video_duration_seconds": video_meta.get("duration_seconds"),
            "facts": {
                "rooms": facts.get("rooms"),
                "area_sqm": facts.get("area_sqm"),
                "total_rent_eur": facts.get("total_rent_eur"),
                "availability": facts.get("availability"),
                "address_lines": facts.get("address_lines") or packet.get("address_lines") or [],
                "teaser_attributes": facts.get("teaser_attributes") or [],
            },
            "brief": {
                "theme_name": str(variant.get("theme_name") or "").strip(),
                "tour_style": str(variant.get("tour_style") or "").strip(),
                "audience": str(variant.get("audience") or "").strip(),
                "creative_brief": str(variant.get("creative_brief") or "").strip(),
                "call_to_action": str(variant.get("call_to_action") or "").strip(),
            },
            "editor_url": str(row.get("editor_url") or structured.get("editor_url") or "").strip(),
            "crezlo_public_url": str(row.get("public_url") or structured.get("public_url") or "").strip(),
            "scenes": published_scenes,
        }
        published = public_tour_bundle_payload(payload=full_payload, bundle_dir=target_dir)
        private_receipt = private_tour_receipt(full_payload)
        out_path = target_dir / "tour.json"
        out_path.write_text(json.dumps(published, indent=2, ensure_ascii=False) + "\n")
        (target_dir / "tour.private.json").write_text(json.dumps(private_receipt, indent=2, ensure_ascii=False) + "\n")
        index_rows.append(
            {
                "slug": slug,
                "listing_id": listing_id,
                "variant_key": variant_key,
                "hosted_url": hosted_url,
                "path": str(out_path),
            }
        )
    index_path = output_dir / "index.json"
    index_path.write_text(json.dumps(index_rows, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"status": "ok", "count": len(index_rows), "index": str(index_path)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
