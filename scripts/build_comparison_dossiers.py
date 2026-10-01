#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont


ROOT = Path("/tmp/property_compare_delivery")
ROOT.mkdir(parents=True, exist_ok=True)
PDF_RENDER_VENV = Path("/tmp/pdfenv/bin/python")
EA_APP_ROOT = Path("/docker/property/ea")
DOCKER_PUBLIC_TOUR_VOLUME = Path("/var/lib/docker/volumes/property_propertyquarry_public_tours/_data")
if str(EA_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_APP_ROOT))


@dataclass
class ListingSpec:
    key: str
    title: str
    url: str
    compare_reason: str
    recommendation: str


LISTINGS: list[ListingSpec] = [
    ListingSpec(
        key="sachsenplatz",
        title="Sachsenplatz rental",
        url="https://www.willhaben.at/iad/immobilien/d/mietwohnungen/wien/wien-1200-brigittenau/moderne-sonnige-2-zimmer-wohnung-provisionsfrei-mit-balkon-und-loggia-am-sachsenplatz-1406309127/",
        compare_reason="Chosen as the lead review because it combines a real floorplan, outdoor space, and a cleaner remote-review basis than the peers.",
        recommendation="Investigate further",
    ),
    ListingSpec(
        key="brigittenau_buy",
        title="Brigittenau purchase",
        url="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1200-brigittenau/paerchenhit-2-zimmer-wohnung-68-m-u-bahn-naehe-in-1200-wien-1335192243/",
        compare_reason="Stronger room count and ownership upside, but it needs closer scrutiny on purchase overhead and long-term fit versus the rental lead.",
        recommendation="Compare on budget discipline",
    ),
    ListingSpec(
        key="donaustadt_buy",
        title="Donaustadt new-build benchmark",
        url="https://www.willhaben.at/iad/immobilien/d/eigentumswohnung/wien/wien-1220-donaustadt/2-zimmer-eigentumswohnung-mit-16-m-balkon-und-moderner-ausstattung-2117562412/",
        compare_reason="Useful benchmark for new-build efficiency and balcony quality, but the evidence pack is thinner and the move-in horizon is later.",
        recommendation="Use as benchmark",
    ),
]


def _run_packet_script(url: str) -> dict[str, Any]:
    raw = subprocess.check_output(
        ["python3", "scripts/willhaben_property_packet.py", url],
        cwd="/docker/property",
        text=True,
    )
    payload = json.loads(raw)
    if isinstance(payload, list):
        payload = payload[0]
    if not isinstance(payload, dict):
        raise RuntimeError(f"unexpected_packet_payload:{url}")
    return payload


def _safe_slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")


def _download(url: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    response = requests.get(url, timeout=90, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    path.write_bytes(response.content)
    return path


def _load_font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def _hero_image(packet: dict[str, Any], out_dir: Path) -> Path:
    media = list(packet.get("media_urls_json") or [])
    if not media:
        raise RuntimeError("hero_media_missing")
    hero_url = str(media[0]).strip()
    ext = Path(hero_url.split("?")[0]).suffix or ".jpg"
    return _download(hero_url, out_dir / f"hero{ext}")


def _render_pdf_first_page(pdf_url: str, out_path: Path) -> Path:
    if not PDF_RENDER_VENV.exists():
        raise RuntimeError("pdf_render_venv_missing")
    pdf_path = out_path.with_suffix(".pdf")
    _download(pdf_url, pdf_path)
    helper = out_path.parent / "render_pdf_page.py"
    helper.write_text(
        textwrap.dedent(
            """
            import sys
            import pypdfium2 as pdfium
            from PIL import Image

            pdf_path, out_path = sys.argv[1], sys.argv[2]
            doc = pdfium.PdfDocument(pdf_path)
            page = doc[0]
            image = page.render(scale=2.0).to_pil()
            image.save(out_path)
            """
        ),
        encoding="utf-8",
    )
    subprocess.run([str(PDF_RENDER_VENV), str(helper), str(pdf_path), str(out_path)], check=True)
    return out_path


def _floorplan_asset(packet: dict[str, Any], out_dir: Path) -> Path:
    floorplans = [str(value).strip() for value in list(packet.get("floorplan_urls_json") or []) if str(value).strip()]
    if floorplans:
        url = floorplans[0]
        ext = Path(url.split("?")[0]).suffix or ".jpg"
        return _download(url, out_dir / f"floorplan{ext}")
    facts = dict(packet.get("property_facts_json") or {})
    attribute_map = dict(facts.get("attribute_map") or {})
    doc_urls = attribute_map.get("INFOLINK/URL") or []
    if doc_urls:
        return _render_pdf_first_page(str(doc_urls[0]).strip(), out_dir / "floorplan.png")
    raise RuntimeError("floorplan_source_missing")


def _wrap(text: str, width: int) -> list[str]:
    normalized = " ".join(str(text or "").split()).strip()
    return textwrap.wrap(normalized, width=width) if normalized else []


def _draw_chip(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, *, fill: tuple[int, int, int], font: ImageFont.ImageFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0] + 26
    height = bbox[3] - bbox[1] + 16
    x, y = xy
    draw.rounded_rectangle((x, y, x + width, y + height), radius=14, fill=fill)
    draw.text((x + 13, y + 8), text, fill=(247, 247, 244), font=font)
    return width


def _make_diorama(packet: dict[str, Any], listing: ListingSpec, hero_path: Path, floorplan_path: Path, out_path: Path) -> Path:
    canvas = Image.new("RGB", (1600, 900), (236, 231, 224))
    hero = Image.open(hero_path).convert("RGB").resize((1600, 900))
    hero = hero.filter(ImageFilter.GaussianBlur(radius=2.2))
    canvas.paste(hero, (0, 0))
    overlay = Image.new("RGBA", canvas.size, (10, 14, 17, 146))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay)

    floorplan = Image.open(floorplan_path).convert("RGB")
    floorplan.thumbnail((760, 560))
    floorplan = floorplan.rotate(-8, expand=True, resample=Image.Resampling.BICUBIC)
    panel = Image.new("RGBA", (floorplan.width + 28, floorplan.height + 28), (248, 245, 240, 255))
    panel.paste(floorplan, (14, 14))
    shadow = panel.filter(ImageFilter.GaussianBlur(radius=18))
    canvas.alpha_composite(shadow, (736, 188))
    canvas.alpha_composite(panel, (714, 162))

    draw = ImageDraw.Draw(canvas)
    title_font = _load_font(44, bold=True)
    body_font = _load_font(22)
    small_font = _load_font(18)
    chip_font = _load_font(17, bold=True)
    draw.text((84, 86), "PropertyQuarry", fill=(239, 231, 220), font=_load_font(22, bold=True))
    draw.text((84, 128), listing.title, fill=(251, 247, 240), font=title_font)

    facts = dict(packet.get("property_facts_json") or {})
    chips = [
        facts.get("rooms_label") or "",
        f"{facts.get('area_sqm') or ''} m²".replace(".0 ", " "),
        str(facts.get("total_rent_eur") or facts.get("price") or facts.get("price_for_display") or "").replace(".0", ""),
    ]
    chip_x = 84
    for chip in [c for c in chips if c]:
        chip_x += _draw_chip(draw, (chip_x, 212), chip, fill=(151, 102, 78), font=chip_font) + 12

    summary = listing.compare_reason
    lines = _wrap(summary, 42)
    y = 288
    for line in lines[:4]:
        draw.text((84, y), line, fill=(232, 228, 224), font=body_font)
        y += 34

    draw.text((84, 478), "Diorama preview", fill=(254, 242, 228), font=_load_font(24, bold=True))
    for idx, line in enumerate(
        [
            "Layout-first preview with route emphasis,",
            "floor-plan context, and a direct review path into the",
            "hosted 3D tour. Walkthroughs appear only after proof.",
        ]
    ):
        draw.text((84, 522 + idx * 28), line, fill=(225, 221, 218), font=small_font)

    draw.rounded_rectangle((82, 690, 366, 760), radius=22, fill=(248, 244, 237))
    draw.text((108, 712), "Open 3D tour", fill=(42, 42, 42), font=_load_font(24, bold=True))

    # A subtle route ribbon across the floorplan card.
    route = [(800, 638), (910, 580), (1032, 542), (1188, 500), (1294, 450)]
    draw.line(route, fill=(190, 88, 61), width=12, joint="curve")
    for point in route:
        draw.ellipse((point[0] - 11, point[1] - 11, point[0] + 11, point[1] + 11), fill=(250, 246, 240), outline=(190, 88, 61), width=4)

    final = canvas.convert("RGB")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    final.save(out_path, quality=92)
    return out_path


def _build_flythrough_video(listing: ListingSpec, hero_path: Path, floorplan_path: Path, out_path: Path) -> Path:
    width, height, fps, seconds = 1280, 720, 24, 12
    total_frames = fps * seconds
    hero = cv2.cvtColor(np.array(Image.open(hero_path).convert("RGB")), cv2.COLOR_RGB2BGR)
    floor = cv2.cvtColor(np.array(Image.open(floorplan_path).convert("RGB")), cv2.COLOR_RGB2BGR)
    writer = cv2.VideoWriter(str(out_path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
    route = [(0.18, 0.80), (0.35, 0.64), (0.52, 0.52), (0.72, 0.42), (0.84, 0.30)]
    route_px = [(int(floor.shape[1] * x), int(floor.shape[0] * y)) for x, y in route]
    for index in range(total_frames):
        t = index / max(total_frames - 1, 1)
        zoom = 1.0 + 0.24 * t
        crop_w = int(hero.shape[1] / zoom)
        crop_h = int(hero.shape[0] / zoom)
        center_x = int(hero.shape[1] * (0.46 + 0.08 * math.sin(t * math.pi * 0.8)))
        center_y = int(hero.shape[0] * (0.44 + 0.05 * math.cos(t * math.pi * 0.7)))
        x0 = max(0, min(hero.shape[1] - crop_w, center_x - crop_w // 2))
        y0 = max(0, min(hero.shape[0] - crop_h, center_y - crop_h // 2))
        frame = hero[y0 : y0 + crop_h, x0 : x0 + crop_w]
        frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_CUBIC)
        angle = math.sin(t * math.pi * 2.0) * 1.2
        matrix = cv2.getRotationMatrix2D((width / 2, height / 2), angle, 1.0)
        frame = cv2.warpAffine(frame, matrix, (width, height), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (width, 94), (20, 24, 28), -1)
        frame = cv2.addWeighted(overlay, 0.32, frame, 0.68, 0)
        cv2.putText(frame, "PropertyQuarry Flythrough", (52, 58), cv2.FONT_HERSHEY_SIMPLEX, 1.02, (245, 241, 232), 2, cv2.LINE_AA)
        cv2.putText(frame, listing.title[:72], (52, 86), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (223, 219, 210), 1, cv2.LINE_AA)

        mini = cv2.resize(floor, (330, 220), interpolation=cv2.INTER_AREA)
        mini_overlay = mini.copy()
        cv2.rectangle(mini_overlay, (0, 0), (330, 220), (22, 22, 22), -1)
        mini = cv2.addWeighted(mini, 0.88, mini_overlay, 0.12, 0)
        scaled_route = [(int(x / floor.shape[1] * 330), int(y / floor.shape[0] * 220)) for x, y in route_px]
        cv2.polylines(mini, [np.array(scaled_route, dtype=np.int32)], False, (62, 107, 216), 3, cv2.LINE_AA)
        pos = t * (len(scaled_route) - 1)
        seg = min(int(pos), len(scaled_route) - 2)
        local_t = pos - seg
        x = int(scaled_route[seg][0] + (scaled_route[seg + 1][0] - scaled_route[seg][0]) * local_t)
        y = int(scaled_route[seg][1] + (scaled_route[seg + 1][1] - scaled_route[seg][1]) * local_t)
        cv2.circle(mini, (x, y), 7, (242, 242, 242), -1, cv2.LINE_AA)
        cv2.circle(mini, (x, y), 12, (190, 88, 61), 2, cv2.LINE_AA)
        frame[height - 252 : height - 32, width - 370 : width - 40] = mini
        cv2.rectangle(frame, (width - 376, height - 258), (width - 34, height - 26), (247, 243, 236), 2)
        writer.write(frame)
    writer.release()
    return out_path


def _write_bundle_with_video(bundle: dict[str, Any], *, video_path: Path, review_label: str) -> dict[str, Any]:
    public_dir = Path(str(os.getenv("EA_PUBLIC_TOUR_DIR") or "/docker/property/state/public_property_tours")).expanduser()
    slug = str(bundle.get("slug") or "").strip()
    bundle_dir = public_dir / slug
    if not bundle_dir.exists():
        raise RuntimeError(f"hosted_bundle_missing:{slug}")
    relpath = "tour.mp4"
    target = bundle_dir / relpath
    target.write_bytes(video_path.read_bytes())
    manifest_path = bundle_dir / "tour.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["video_relpath"] = relpath
    payload["video_label"] = review_label
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def _listing_review_url(url: str) -> str:
    from urllib.parse import quote
    return f"https://propertyquarry.com/app/research/{quote('property-scout:' + url, safe='')}?mode=review"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-telegram", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("EA_RUNTIME_PROFILE", os.getenv("EA_RUNTIME_PROFILE", "prod"))
    try:
        if DOCKER_PUBLIC_TOUR_VOLUME.exists():
            os.environ.setdefault("EA_PUBLIC_TOUR_DIR", str(DOCKER_PUBLIC_TOUR_VOLUME))
    except PermissionError:
        pass

    from app.container import build_container
    from app.product.service import ProductService
    from app.product import service as product_service_mod
    from app.services.fliplink.service import build_fliplink_packet_service
    from app.services.telegram_delivery import send_telegram_document_for_principal, send_telegram_message_for_principal

    principal_id = "cf-email:tibor.girschele@gmail.com"
    container = build_container()
    packet_service = build_fliplink_packet_service(container)
    product_service = ProductService(container=container)

    listings_payloads: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    output_rows: list[dict[str, Any]] = []
    for spec in LISTINGS:
        packet = _run_packet_script(spec.url)
        listing_dir = ROOT / spec.key
        listing_dir.mkdir(parents=True, exist_ok=True)
        hero_path = _hero_image(packet, listing_dir)
        floorplan_path = _floorplan_asset(packet, listing_dir)
        diorama_path = _make_diorama(packet, spec, hero_path, floorplan_path, listing_dir / "diorama.jpg")
        video_path = _build_flythrough_video(spec, hero_path, floorplan_path, listing_dir / "tour.mp4")
        facts = dict(packet.get("property_facts_json") or {})
        floorplan_urls = [str(value).strip() for value in list(packet.get("floorplan_urls_json") or []) if str(value).strip()]
        if not floorplan_urls:
            doc_urls = list(dict(facts.get("attribute_map") or {}).get("INFOLINK/URL") or [])
            if doc_urls:
                floorplan_urls = [str(doc_urls[0]).strip()]
        bundle = product_service_mod._write_hosted_floorplan_property_tour_bundle(
            principal_id=principal_id,
            title=str(packet.get("title") or spec.title),
            listing_id=str(packet.get("listing_id") or spec.key),
            property_url=spec.url,
            variant_key="layout_first",
            floorplan_urls=floorplan_urls,
            property_facts_json=facts,
            source_host="willhaben.at",
            source_ref=f"willhaben:{packet.get('listing_id') or spec.key}",
            external_id=str(packet.get("listing_id") or spec.key),
            recipient_email="tibor.girschele@gmail.com",
        )
        bundle = _write_bundle_with_video(bundle, video_path=video_path, review_label="Flythrough · interior route")
        tour_url = str(bundle.get("public_url") or bundle.get("hosted_url") or "").strip()
        flythrough_url = f"{tour_url}?pane=flythrough-pane" if tour_url else ""
        packet["tour_url"] = f"{tour_url}?pane=floorplan-pane" if tour_url else ""
        packet["flythrough_url"] = flythrough_url
        packet["vendor_tour_url"] = packet["tour_url"]
        packet["review_url"] = _listing_review_url(spec.url)
        packet["diorama_scene"] = {
            "image_url": diorama_path.resolve().as_uri(),
            "video_url": flythrough_url,
            "tour_url": packet["tour_url"],
            "summary": "A diorama preview introduces the floor-plan route, then hands off into the hosted tour and walkthrough.",
            "privacy_mode": "share_safe",
        }
        packet["compare_reason"] = spec.compare_reason
        packet["recommendation"] = spec.recommendation
        packet["source_virtual_tour_url"] = ""
        listings_payloads.append({"spec": spec, "packet": packet, "bundle": bundle, "video_path": video_path, "diorama_path": diorama_path})
        comparison_rows.append(
            {
                "title": str(packet.get("title") or spec.title),
                "price": facts.get("total_rent_eur") or facts.get("price") or facts.get("price_for_display"),
                "rooms": facts.get("rooms") or facts.get("rooms_label"),
                "area_sqm": facts.get("area_sqm"),
                "recommendation": spec.recommendation,
                "compare_reason": spec.compare_reason,
            }
        )

    for item in listings_payloads:
        spec = item["spec"]
        packet = item["packet"]
        publication = packet_service.render_packet(
            principal_id=principal_id,
            property_ref=f"property-scout:{spec.url}",
            packet_kind="owner_review",
            include_exact_address=False,
            include_floorplan=True,
            include_photos=True,
            source_payload=packet,
            actor="codex_compare_delivery",
        )
        public_pdf_url = product_service._public_property_packet_pdf_url(
            publication_id=str(publication.get("publication_id") or "").strip(),
            source_pdf_sha256=str(publication.get("source_pdf_sha256") or "").strip(),
        )
        output_rows.append(
            {
                "kind": "single",
                "key": spec.key,
                "title": packet.get("title"),
                "publication_id": publication.get("publication_id"),
                "pdf_path": publication.get("source_pdf_artifact_ref"),
                "public_pdf_url": public_pdf_url,
                "tour_url": packet.get("tour_url"),
                "flythrough_url": packet.get("flythrough_url"),
                "review_url": packet.get("review_url"),
            }
        )

    primary = listings_payloads[0]["packet"]
    comparison_packet = dict(primary)
    comparison_packet["title"] = "Vienna comparison dossier"
    comparison_packet["compare_reason"] = "This comparison dossier weighs the rental lead against two ownership alternatives and shows where the evidence is strongest or still too thin."
    comparison_packet["comparison_rows"] = comparison_rows
    comparison_packet["review_url"] = "https://propertyquarry.com/app/properties"
    comparison_packet["diorama_scene"] = {
        "image_url": listings_payloads[0]["diorama_path"].resolve().as_uri(),
        "video_url": str(listings_payloads[0]["packet"].get("flythrough_url") or ""),
        "tour_url": str(listings_payloads[0]["packet"].get("tour_url") or ""),
        "summary": "The lead dossier keeps the floorplan route and diorama visible while the comparison page explains why it beat the alternatives.",
        "privacy_mode": "share_safe",
    }
    comparison_publication = packet_service.render_packet(
        principal_id=principal_id,
        property_ref="comparison:vienna-three-dossiers",
        packet_kind="shortlist_brochure",
        include_exact_address=False,
        include_floorplan=True,
        include_photos=True,
        source_payload=comparison_packet,
        actor="codex_compare_delivery",
    )
    comparison_pdf_url = product_service._public_property_packet_pdf_url(
        publication_id=str(comparison_publication.get("publication_id") or "").strip(),
        source_pdf_sha256=str(comparison_publication.get("source_pdf_sha256") or "").strip(),
    )
    output_rows.append(
        {
            "kind": "comparison",
            "key": "vienna_compare",
            "title": comparison_packet["title"],
            "publication_id": comparison_publication.get("publication_id"),
            "pdf_path": comparison_publication.get("source_pdf_artifact_ref"),
            "public_pdf_url": comparison_pdf_url,
            "tour_url": comparison_packet.get("tour_url"),
            "flythrough_url": comparison_packet.get("flythrough_url"),
            "review_url": comparison_packet.get("review_url"),
        }
    )

    manifest = ROOT / "manifest.json"
    manifest.write_text(json.dumps(output_rows, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.send_telegram:
        tool_runtime = container.tool_runtime
        intro_lines = ["PropertyQuarry delivery", ""]
        for row in output_rows:
            intro_lines.append(f"{row['title']}")
            if row.get("tour_url"):
                intro_lines.append(f"3D tour: {row['tour_url']}")
            if row.get("flythrough_url"):
                intro_lines.append(f"Flythrough: {row['flythrough_url']}")
            intro_lines.append("")
        send_telegram_message_for_principal(
            tool_runtime,
            principal_id=principal_id,
            text="\n".join(intro_lines).strip(),
        )
        for row in output_rows:
            pdf_path = str(row.get("pdf_path") or "").strip()
            if pdf_path:
                send_telegram_document_for_principal(
                    tool_runtime,
                    principal_id=principal_id,
                    document_ref=pdf_path,
                    caption=f"{row['title']} · PropertyQuarry dossier",
                )

    print(json.dumps(output_rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
