from __future__ import annotations

import hashlib
import html as html_lib
import io
import json
import math
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from app.product.projections import compact_text
from app.telemetry import inject_traceparent, start_span

try:
    from PIL import Image, ImageFilter, ImageStat
except Exception:  # pragma: no cover - optional OCR fallback
    Image = None
    ImageFilter = None
    ImageStat = None

try:
    import pytesseract
except Exception:  # pragma: no cover - optional OCR fallback
    pytesseract = None
from app.services.property_market_catalog import (
    normalize_property_platform,
    property_platform_keys,
    property_provider_for_platform,
    provider_host_markers,
    provider_listing_markers_for_host,
)
_PROPERTY_SCOUT_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0 Safari/537.36"
_PROPERTY_SCOUT_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")
_PROPERTY_SCOUT_FLOORPLAN_ASSET_EXTENSIONS = (*_PROPERTY_SCOUT_IMAGE_EXTENSIONS, ".pdf")
_PROPERTY_SCOUT_LISTING_HOSTS = provider_host_markers()
_PROPERTY_SCOUT_ARCHIVE_EXTENSIONS = (".zip",)
_PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_MAX_BYTES = 40 * 1024 * 1024
_PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_MEMBER_MAX_BYTES = 16 * 1024 * 1024
_PROPERTY_SCOUT_FLOORPLAN_MARKERS = (
    "floorplan", "floor plan", "grundriss", "lageplan", "plan", "plan_top", "plan top", "raumskizze",
)
_PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_CONTEXT_MARKERS = (*_PROPERTY_SCOUT_FLOORPLAN_MARKERS, "pdf", "download", "dokument", "beilage", "anlage", "unterlagen")
_PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_MEMBER_MARKERS = (*_PROPERTY_SCOUT_FLOORPLAN_MARKERS, "pdf", "grundrissplan", "wohnungsplan")
_PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_HOST_MARKERS = (
    "justimmo.at",
    "mmo.at",
    "storage.justimmo.at",
    "siedlungsunion.at",
    "gesiba.at",
    "wbv-gpa.at",
    "frieden.at",
    "sozialbau.at",
    "edikte.justiz.gv.at",
    "edikte2.justiz.gv.at",
)
_PROPERTY_SCOUT_360_HOST_MARKERS = (
    "matterport.com",
    "my.matterport.com",
    "360tour",
    "3d-tour",
    "3dtour",
    "3d-tour",
    "virtualtour",
    "virtual-tour",
    "tourmkr.com",
    "eye-spy360.com",
    "ogulo.com",
    "aroundmedia.com",
    "immoviewer.com",
    "giraffe360.com",
    "panoee.com",
    "cloudpano.com",
    "kuula.co",
    "roundme.com",
    "teliportme.com",
    "vieweet.com",
    "3d-wohnung",
    "360grad",
    "360grad-tour",
    "youvisit.com",
    "peek3d",
    "peek3d.app",
    "3dlook.at",
    "360.homestaging",
    "feelestate.com",
    "immobilien360",
    "3d.laendleanzeiger.at",
    "360.kalandra.at",
)
_URL_TEXT_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_WILLHABEN_HOST_MARKERS = ("willhaben.at",)


def _is_willhaben_property_url(value: object) -> bool:
    from .outbound_url_security import canonical_http_hostname, hostname_matches_allowlist

    normalized = str(value or "").strip()
    if not normalized:
        return False
    host = canonical_http_hostname(normalized)
    return bool(host) and hostname_matches_allowlist(host, _WILLHABEN_HOST_MARKERS)


_PROPERTY_HTML_FRAGMENT_HIDDEN_BLOCK_RE = re.compile(
    r"<!--[\s\S]*?-->"
    r"|<\s*script\b(?![^>]*type\s*=\s*['\"]?application/(?:ld\+)?json\b)[\s\S]*?<\s*/\s*script\s*>"
    r"|<\s*(style|template|noscript)\b[\s\S]*?<\s*/\s*\1\s*>"
    r"|<(?P<hidden_tag>[a-zA-Z0-9:-]+)\b(?=[^>]*(?:display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0|aria-hidden\s*=\s*['\"]?true|hidden\b|type\s*=\s*['\"]?hidden))[^>]*>[\s\S]*?<\s*/\s*(?P=hidden_tag)\s*>",
    flags=re.IGNORECASE,
)


def _property_html_fragment_text(fragment: object) -> str:
    visible_fragment = _PROPERTY_HTML_FRAGMENT_HIDDEN_BLOCK_RE.sub(" ", str(fragment or ""))
    value = html_lib.unescape(re.sub(r"<[^>]+>", " ", visible_fragment, flags=re.IGNORECASE | re.DOTALL))
    return " ".join(value.split())


def _property_scout_image_looks_like_floorplan(payload: bytes) -> tuple[bool, dict[str, object]]:
    if Image is None or ImageFilter is None or ImageStat is None:
        return False, {"status": "image_library_unavailable"}
    if not payload or len(payload) > 3 * 1024 * 1024:
        return False, {"status": "image_too_large_or_empty", "size_bytes": len(payload or b"")}
    try:
        image = Image.open(io.BytesIO(payload))
        image.load()
    except Exception:
        return False, {"status": "image_decode_failed"}
    width, height = image.size
    if width < 180 or height < 120:
        return False, {"status": "image_too_small", "width": width, "height": height}
    image.thumbnail((720, 720))
    gray = image.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    edge_mean = float(ImageStat.Stat(edges).mean[0])
    rgb = image.convert("RGB")
    sample = rgb.resize((96, 96))
    sample_pixels = sample.get_flattened_data() if hasattr(sample, "get_flattened_data") else sample.getdata()
    pixels = list(sample_pixels)
    if not pixels:
        return False, {"status": "image_empty_sample"}
    dark_ratio = sum(1 for r, g, b in pixels if (r + g + b) / 3 < 96) / len(pixels)
    light_ratio = sum(1 for r, g, b in pixels if (r + g + b) / 3 > 206) / len(pixels)
    saturation_values = [(max(r, g, b) - min(r, g, b)) for r, g, b in pixels]
    avg_saturation = sum(saturation_values) / len(saturation_values)
    ocr_text = ""
    if pytesseract is not None and shutil.which("tesseract"):
        try:
            ocr_text = pytesseract.image_to_string(gray, config="--psm 11")[:800]
        except Exception:
            ocr_text = ""
    normalized_ocr = re.sub(r"[^a-z0-9äöüß]+", " ", ocr_text.lower()).strip()
    room_word_hits = sum(
        1
        for marker in (
            "zimmer", "kueche", "küche", "bad", "wc", "flur", "gang", "balkon", "terrasse",
            "bedroom", "bath", "kitchen", "living", "room", "floor plan", "floorplan", "grundriss",
        )
        if marker in normalized_ocr
    )
    # Tiny broker marks and wordmark logos can have the same high-contrast,
    # low-saturation geometry as a plan.  Gallery floorplans are expected to
    # retain enough spatial resolution for review; keep the OCR path below as
    # the escape hatch for genuinely labelled small plan thumbnails.
    plan_like_geometry = (
        min(width, height) >= 240
        and edge_mean >= 10.0
        and light_ratio >= 0.42
        and 0.01 <= dark_ratio <= 0.42
        and avg_saturation <= 62.0
    )
    light_scan_like_plan = (
        edge_mean >= 13.0
        and light_ratio >= 0.78
        and dark_ratio <= 0.08
        and avg_saturation <= 12.0
        and min(width, height) >= 280
    )
    ocr_like_plan = room_word_hits >= 2 and light_ratio >= 0.30 and avg_saturation <= 95.0
    return bool(plan_like_geometry or light_scan_like_plan or ocr_like_plan), {
        "status": "classified",
        "width": width,
        "height": height,
        "edge_mean": round(edge_mean, 2),
        "dark_ratio": round(dark_ratio, 3),
        "light_ratio": round(light_ratio, 3),
        "avg_saturation": round(avg_saturation, 2),
        "room_word_hits": room_word_hits,
        "ocr_used": bool(ocr_text),
        "classifier": "geometry_or_ocr_floorplan_v2",
    }


def _property_public_app_base_url() -> str:
    explicit = str(os.getenv("PROPERTYQUARRY_PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    return "https://propertyquarry.com"


def _float_or_none(value: object) -> float | None:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        return float(text.replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _extract_urls_from_text(value: object) -> tuple[str, ...]:
    rows: list[str] = []
    seen: set[str] = set()
    for raw in _URL_TEXT_RE.findall(str(value or "")):
        normalized = str(raw or "").strip().rstrip(").,;]>")
        if normalized and normalized not in seen:
            seen.add(normalized)
            rows.append(normalized)
    return tuple(rows)

def _property_scout_platform_from_url(url: str) -> str:
    from .outbound_url_security import canonical_http_hostname, hostname_matches_allowlist

    parsed = urllib.parse.urlparse(urllib.parse.urldefrag(str(url or "").strip())[0])
    host = canonical_http_hostname(urllib.parse.urlunparse(parsed))
    if not host:
        return ""
    for platform in property_platform_keys():
        provider = property_provider_for_platform(platform)
        if provider is not None and hostname_matches_allowlist(host, provider.host_markers):
            return platform
    return ""

def _property_scout_clean_url(url: str) -> str:
    normalized = urllib.parse.urldefrag(str(url or "").strip().strip("\"'"))[0]
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    if not str(parsed.query or "").strip():
        return normalized
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    cleaned_query = [(key, str(value or "").strip("\"'")) for key, value in query]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(cleaned_query, doseq=True)))

def _property_scout_is_supported_listing_url(url: str) -> bool:
    from .outbound_url_security import canonical_http_hostname, hostname_matches_allowlist

    normalized = _property_scout_clean_url(url)
    if not normalized:
        return False
    parsed = urllib.parse.urlparse(normalized)
    host = canonical_http_hostname(normalized)
    if not host:
        return False
    path = parsed.path.lower()
    combined = f"{path}?{parsed.query.lower()}" if str(parsed.query or "").strip() else path
    if any(path.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg", ".css", ".js", ".json")):
        return False
    if _is_willhaben_property_url(normalized):
        query = urllib.parse.parse_qs(parsed.query)
        return "/iad/immobilien/d/" in path or (path == "/iad/object" and bool(query.get("adId") or query.get("adid")))
    if hostname_matches_allowlist(host, ("edikte.justiz.gv.at", "edikte2.justiz.gv.at")):
        return "/alldoc/" in path or "/0/" in path or path.endswith("!opendocument")
    if hostname_matches_allowlist(host, ("gesiba.at",)):
        query = urllib.parse.parse_qs(parsed.query)
        return path.startswith("/immobilien/wohnungen/objekt") and bool(query.get("objektnummer"))
    if hostname_matches_allowlist(host, ("findmyhome.at",)):
        return bool(re.fullmatch(r"/\d+", path)) and str(parsed.query or "").strip().lower().startswith("tl=")
    if not hostname_matches_allowlist(host, _PROPERTY_SCOUT_LISTING_HOSTS):
        return False
    for marker in provider_listing_markers_for_host(host):
        normalized_marker = str(marker or "").lower()
        if not normalized_marker or normalized_marker not in combined:
            continue
        marker_path = urllib.parse.urlparse(normalized_marker).path or normalized_marker.split("?", 1)[0]
        marker_path = marker_path.strip()
        if marker_path.endswith("/"):
            path_remainder = path[len(marker_path) :].strip("/") if path.startswith(marker_path) else ""
            if not path_remainder or path_remainder.lower() in {"view", "map", "search", "results"}:
                continue
        return True
    return False


def _willhaben_ad_id_from_url(url: str) -> str:
    normalized = _property_scout_clean_url(url)
    parsed = urllib.parse.urlparse(normalized)
    query = urllib.parse.parse_qs(parsed.query)
    for key in ("adId", "adid"):
        value = str((query.get(key) or [""])[0] or "").strip()
        if value:
            return value
    matches = re.findall(r"(\d{6,})(?:/)?$", urllib.parse.unquote(parsed.path or ""))
    return str(matches[-1] or "").strip() if matches else ""


def _property_scout_prefer_willhaben_detail_urls(urls: list[str]) -> tuple[str, ...]:
    detail_by_ad_id: dict[str, str] = {}
    detail_without_ad_id: list[str] = []
    object_urls: list[str] = []
    for url in urls:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path or "").lower()
        if "/iad/immobilien/d/" in path:
            ad_id = _willhaben_ad_id_from_url(url)
            if ad_id:
                detail_by_ad_id.setdefault(ad_id, url)
            else:
                detail_without_ad_id.append(url)
            continue
        object_urls.append(url)
    rows: list[str] = []
    seen: set[str] = set()
    for url in (*detail_by_ad_id.values(), *detail_without_ad_id):
        if url and url not in seen:
            seen.add(url)
            rows.append(url)
    for url in object_urls:
        ad_id = _willhaben_ad_id_from_url(url)
        if ad_id and ad_id in detail_by_ad_id:
            continue
        if url and url not in seen:
            seen.add(url)
            rows.append(url)
    return tuple(rows)


def _property_scout_source_requested_min_area_m2(source_spec: dict[str, object] | None) -> float:
    payload = dict(source_spec or {})
    pushdown = dict(payload.get("provider_filter_pushdown") or {}) if isinstance(payload.get("provider_filter_pushdown"), dict) else {}
    requested = dict(pushdown.get("requested") or {}) if isinstance(pushdown.get("requested"), dict) else {}
    applied = dict(pushdown.get("applied") or {}) if isinstance(pushdown.get("applied"), dict) else {}
    return _float_or_none(requested.get("min_area_m2")) or _float_or_none(applied.get("min_area_m2")) or 0.0

def _property_area_text_to_sqm(value: object) -> float | None:
    text = compact_text(str(value or "").strip(), fallback="", limit=80)
    if not text:
        return None
    match = re.search(r"(\d+(?:[.,]\d+)?)", text)
    if not match:
        return None
    try:
        return float(str(match.group(1) or "").replace(".", "").replace(",", "."))
    except Exception:
        return None

def _property_scout_extract_wbv_gpa_listing_urls(*, source_url: str, html: str, min_area_m2: float = 0.0) -> tuple[str, ...]:
    rows: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<div[^>]*class="[^"]*objects__list__rows__item[^"]*"[^>]*data-space="([^"]+)"[^>]*>.*?<a[^>]*href="([^"]+/wohnung/[^"]+)"',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(str(html or "")):
        area_sqm = _property_area_text_to_sqm(match.group(1))
        if min_area_m2 > 0.0 and (not isinstance(area_sqm, float) or area_sqm < min_area_m2):
            continue
        candidate = urllib.parse.urldefrag(urllib.parse.urljoin(source_url, str(match.group(2) or "").strip()))[0]
        if candidate and candidate not in seen and _property_scout_is_supported_listing_url(candidate):
            seen.add(candidate)
            rows.append(candidate)
    return tuple(rows)

def _property_scout_extract_frieden_listing_urls(*, source_url: str, html: str, min_area_m2: float = 0.0) -> tuple[str, ...]:
    rows: list[str] = []
    seen: set[str] = set()
    route_match = re.search(r"window\.__ROUTE_DATA__\s*=\s*(\{.*?\})\s*(?:</script>|$)", str(html or ""), flags=re.IGNORECASE | re.DOTALL)
    if route_match:
        try:
            route_data = json.loads(str(route_match.group(1) or ""))
        except Exception:
            route_data = {}
        model = dict(route_data.get("model") or {}) if isinstance(route_data, dict) else {}
        units = list(dict(model.get("units") or {}).get("items") or []) if isinstance(model.get("units"), dict) else []
        for unit in units:
            if not isinstance(unit, dict):
                continue
            area_sqm = _float_or_none(unit.get("usableArea")) or 0.0
            if min_area_m2 > 0.0 and area_sqm < min_area_m2:
                continue
            unit_id = str(unit.get("id") or "").strip()
            if not unit_id:
                continue
            candidate = urllib.parse.urldefrag(
                urllib.parse.urljoin(source_url, f"/immobiliensuche/{unit_id}?returnUrl=%2Fimmobiliensuche")
            )[0]
            if candidate and candidate not in seen and _property_scout_is_supported_listing_url(candidate):
                seen.add(candidate)
                rows.append(candidate)
    return tuple(rows)

def _property_scout_extract_findmyhome_listing_urls(*, source_url: str, html: str) -> tuple[str, ...]:
    rows: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"""<h3[^>]*class=["'][^"']*obj_list[^"']*["'][^>]*>.*?<a[^>]*href=['"]([^'"]+/\d+\?tl=\d+)['"]""",
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(str(html or "")):
        candidate = _property_scout_clean_url(urllib.parse.urljoin(source_url, str(match.group(1) or "").strip()))
        if candidate and candidate not in seen and _property_scout_is_supported_listing_url(candidate):
            seen.add(candidate)
            rows.append(candidate)
    return tuple(rows)


def _property_scout_extract_ohne_makler_listing_urls(*, source_url: str, html: str) -> tuple[str, ...]:
    rows: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"""href=["']([^"']*/immobilie/\d+/?)["']""", str(html or ""), re.IGNORECASE):
        candidate = _property_scout_clean_url(urllib.parse.urljoin(source_url, str(match.group(1) or "").strip()))
        parsed = urllib.parse.urlparse(candidate)
        if not re.fullmatch(r"/immobilie/\d+/?", parsed.path or "", re.IGNORECASE):
            continue
        if candidate and candidate not in seen and _property_scout_is_supported_listing_url(candidate):
            seen.add(candidate)
            rows.append(candidate)
    return tuple(rows)


def _property_scout_extract_neubaukompass_listing_urls(*, source_url: str, html: str) -> tuple[str, ...]:
    rows: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"""href=["']([^"']*/property/[^"'/?#]+/?(?:\?[^"']*)?)["']""", str(html or ""), re.IGNORECASE):
        candidate = _property_scout_clean_url(urllib.parse.urljoin(source_url, str(match.group(1) or "").strip()))
        parsed = urllib.parse.urlparse(candidate)
        if not re.fullmatch(r"/property/[^/]+/?", parsed.path or "", re.IGNORECASE):
            continue
        if candidate and candidate not in seen and _property_scout_is_supported_listing_url(candidate):
            seen.add(candidate)
            rows.append(candidate)
    return tuple(rows)

def _property_scout_extract_listing_urls(*, source_url: str, html: str, source_spec: dict[str, object] | None = None) -> tuple[str, ...]:
    parsed_source = urllib.parse.urlparse(str(source_url or "").strip())
    source_query = urllib.parse.parse_qs(parsed_source.query)
    sozialbau_scope = str((source_query.get("pq_scope") or [""])[0]).strip().lower()
    requested_min_area_m2 = _property_scout_source_requested_min_area_m2(source_spec)
    source_host = parsed_source.netloc.lower()
    if "findmyhome.at" in source_host:
        rows = _property_scout_extract_findmyhome_listing_urls(source_url=source_url, html=html)
        if rows:
            return rows
    if "wbv-gpa.at" in source_host:
        rows = _property_scout_extract_wbv_gpa_listing_urls(source_url=source_url, html=html, min_area_m2=requested_min_area_m2)
        if rows:
            return rows
    if "frieden.at" in source_host:
        rows = _property_scout_extract_frieden_listing_urls(source_url=source_url, html=html, min_area_m2=requested_min_area_m2)
        if rows:
            return rows
    if "ohne-makler.net" in source_host:
        rows = _property_scout_extract_ohne_makler_listing_urls(source_url=source_url, html=html)
        if rows:
            return rows
    if "neubaukompass.com" in source_host:
        rows = _property_scout_extract_neubaukompass_listing_urls(source_url=source_url, html=html)
        if rows:
            return rows
    if "angebote.sozialbau.at" in parsed_source.netloc.lower() and sozialbau_scope in {"in_bau", "in_planung"}:
        rows: list[str] = []
        seen_rows: set[str] = set()
        for match in re.finditer(r"<tr[^>]*data-ri=\"\d+\"[^>]*>(.*?)</tr>", str(html or ""), re.IGNORECASE | re.DOTALL):
            row_html = str(match.group(1) or "")
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.IGNORECASE | re.DOTALL)
            if len(cells) < 6:
                continue
            offer_type = compact_text(_property_html_fragment_text(cells[0]), fallback="", limit=24)
            anchor_match = re.search(r"<a[^>]*>(.*?)</a>", cells[1], re.IGNORECASE | re.DOTALL)
            address_fragment = str(anchor_match.group(1) or "") if anchor_match else str(cells[1] or "")
            address_lines = [
                compact_text(_property_html_fragment_text(part), fallback="", limit=160)
                for part in re.split(r"<br\s*/?>", address_fragment, flags=re.IGNORECASE)
                if compact_text(_property_html_fragment_text(part), fallback="", limit=160)
            ]
            if not address_lines:
                continue
            postal_name = compact_text(address_lines[0], fallback="", limit=80)
            street_address = compact_text(address_lines[-1], fallback="", limit=160)
            unit_count = re.sub(r"[^\d]", "", _property_html_fragment_text(cells[2]))
            move_in = compact_text(_property_html_fragment_text(cells[3]), fallback="", limit=80)
            registration_count = re.sub(r"[^\d]", "", _property_html_fragment_text(cells[4]))
            map_href_match = re.search(r'href="https://www\.google\.com/maps/place/([0-9\.\-]+),([0-9\.\-]+)', row_html, re.IGNORECASE)
            params = {
                "pq_listing": "1",
                "offer_type": offer_type,
                "postal_name": postal_name,
                "street_address": street_address,
                "unit_count": unit_count,
                "move_in": move_in,
                "registration_count": registration_count,
                "pq_scope": sozialbau_scope,
            }
            if map_href_match:
                params["map_lat"] = str(map_href_match.group(1) or "")
                params["map_lng"] = str(map_href_match.group(2) or "")
            listing_url = urllib.parse.urlunparse(
                parsed_source._replace(
                    query=urllib.parse.urlencode({key: value for key, value in params.items() if str(value or "").strip()})
                )
            )
            if listing_url and listing_url not in seen_rows:
                seen_rows.add(listing_url)
                rows.append(listing_url)
        return tuple(rows)
    candidates: list[str] = []
    seen: set[str] = set()
    normalized_html = (
        str(html or "")
        .replace("\\u002F", "/")
        .replace("\\/", "/")
        .replace("&amp;", "&")
    )
    raw_urls = list(_extract_urls_from_text(normalized_html))
    for match in re.finditer(r"""href=["']([^"']+)["']""", normalized_html, re.IGNORECASE):
        try:
            raw_urls.append(urllib.parse.urljoin(source_url, match.group(1).strip()))
        except ValueError:
            continue
    raw_urls.extend(_property_scout_extract_html_attr_urls(source_url=source_url, html=normalized_html, attr_name="data-href"))
    raw_urls.extend(_property_scout_extract_html_attr_urls(source_url=source_url, html=normalized_html, attr_name="data-url"))
    for match in re.finditer(r"""location(?:\.href)?\s*=\s*["']([^"']+)["']""", normalized_html, re.IGNORECASE):
        try:
            raw_urls.append(urllib.parse.urljoin(source_url, match.group(1).strip()))
        except ValueError:
            continue
    path_markers = provider_listing_markers_for_host(parsed_source.netloc.lower())
    required_upstream_platform = normalize_property_platform(str((source_query.get("pq_upstream") or [""])[0]).strip())
    if path_markers:
        escaped_markers = sorted((re.escape(marker) for marker in path_markers if str(marker or "").strip()), key=len, reverse=True)
        if escaped_markers:
            path_pattern = re.compile(
                r'((?:https?:)?//[^"\']+|(?:'
                + "|".join(escaped_markers)
                + r')[^"\'\s<>{}]*)',
                re.IGNORECASE,
            )
            for match in path_pattern.finditer(normalized_html):
                try:
                    raw_urls.append(urllib.parse.urljoin(source_url, str(match.group(1) or "").strip()))
                except ValueError:
                    continue
    for raw_url in raw_urls:
        normalized = _property_scout_clean_url(raw_url)
        if not normalized or normalized in seen:
            continue
        if not _property_scout_is_supported_listing_url(normalized):
            continue
        if required_upstream_platform:
            upstream_platform = _property_scout_platform_from_url(normalized)
            if upstream_platform != required_upstream_platform:
                continue
        seen.add(normalized)
        candidates.append(normalized)
    if "willhaben.at" in source_host:
        return _property_scout_prefer_willhaben_detail_urls(candidates)
    return tuple(candidates)

def _property_scout_extract_meta_content(html: str, property_name: str) -> str:
    values = _property_scout_extract_meta_contents(html, property_name)
    return compact_text(values[0], fallback="", limit=400) if values else ""

def _property_scout_extract_meta_contents(html: str, property_name: str) -> tuple[str, ...]:
    pattern = re.compile(
        r'<meta[^>]+(?:property|name)=["\']'
        + re.escape(property_name)
        + r'["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    )
    values: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(str(html or "")):
        value = html_lib.unescape(str(match.group(1) or "")).strip()
        if value and value not in seen:
            seen.add(value)
            values.append(value)
    return tuple(values)

def _property_scout_extract_html_attr_urls(*, source_url: str, html: str, attr_name: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        rf"""{re.escape(attr_name)}=["']([^"']+)["']""",
        re.IGNORECASE,
    )
    for match in pattern.finditer(str(html or "").replace("&amp;", "&")):
        raw = str(match.group(1) or "").strip()
        if not raw:
            continue
        try:
            normalized = urllib.parse.urljoin(source_url, raw)
        except ValueError:
            continue
        parsed = urllib.parse.urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        if normalized not in seen:
            seen.add(normalized)
            values.append(normalized)
    return tuple(values)


def _property_scout_media_asset_identity(url: str) -> str:
    normalized = urllib.parse.urldefrag(str(url or "").strip())[0]
    if not normalized:
        return ""
    parsed = urllib.parse.urlparse(normalized)
    path = urllib.parse.unquote(parsed.path or "")
    if "/~/" in path:
        path = path.split("/~/", 1)[0]
    return urllib.parse.urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def _property_scout_media_url_priority(url: str) -> tuple[int, int, int]:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    combined = urllib.parse.unquote(f"{parsed.netloc}{parsed.path}?{parsed.query}").lower()
    score = 0
    if any(marker in combined for marker in ("immoimporte", "storage.justimmo.at", "/thumb/", "gallery", "listing", "property", "expose")):
        score += 6
    if any(marker in combined for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS):
        score += 8
    if "format:jpg" in combined or "format=jpg" in combined or combined.endswith(".jpg"):
        score += 2
    if "format:png" in combined or "format=png" in combined or combined.endswith(".png"):
        score += 1
    if any(marker in combined for marker in ("favicon", "apple-touch-icon", "/icons/", "/icon/", "/logo", "sprite")):
        score -= 12
    if any(marker in combined for marker in ("avatar", "broker", "agentcard", "businesscard", "card")):
        score -= 3
    return (score, -len(parsed.path or ""), -len(parsed.query or ""))


def _property_scout_prioritized_media_urls(urls: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    deduped_by_identity: dict[str, str] = {}
    for raw_url in urls:
        normalized = urllib.parse.urldefrag(str(raw_url or "").strip())[0]
        if not normalized:
            continue
        identity = _property_scout_media_asset_identity(normalized) or normalized
        incumbent = deduped_by_identity.get(identity)
        if not incumbent:
            deduped_by_identity[identity] = normalized
            continue
        if _property_scout_media_url_priority(normalized) > _property_scout_media_url_priority(incumbent):
            deduped_by_identity[identity] = normalized
    ranked = sorted(
        deduped_by_identity.values(),
        key=lambda value: _property_scout_media_url_priority(value),
        reverse=True,
    )
    return tuple(ranked)


def _property_scout_is_asset_url(url: str, *, extensions: tuple[str, ...]) -> bool:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    path = urllib.parse.unquote(parsed.path or "").lower()
    combined = urllib.parse.unquote(f"{path}?{parsed.query}").lower()
    if path.endswith(extensions):
        return True
    if any(segment.endswith(extensions) for segment in path.split("/") if segment):
        return True
    return any(
        marker in combined
        for extension in extensions
        for marker in (f"format:{extension.lstrip('.')}", f"format={extension.lstrip('.')}")
    )

def _property_scout_is_archive_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(str(url or "").strip())
    path = urllib.parse.unquote(parsed.path or "").lower()
    return bool(path.endswith(_PROPERTY_SCOUT_ARCHIVE_EXTENSIONS))

def _property_scout_is_floorplan_archive_candidate_url(*, url: str, context: str) -> bool:
    normalized = urllib.parse.urldefrag(str(url or "").strip())[0]
    if not normalized:
        return False
    if _property_scout_is_archive_url(normalized):
        return True
    parsed = urllib.parse.urlparse(normalized)
    host = parsed.netloc.lower()
    combined = urllib.parse.unquote(f"{host}{parsed.path}?{parsed.query}").lower()
    lowered_context = str(context or "").lower()
    provider_host = any(marker in host for marker in _PROPERTY_SCOUT_LISTING_HOSTS)
    direct_provider_floorplan_pdf = (
        urllib.parse.unquote(parsed.path or "").lower().endswith(".pdf")
        and provider_host
        and any(marker in f"{combined} {lowered_context}" for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS)
    )
    if direct_provider_floorplan_pdf:
        return True
    if urllib.parse.unquote(parsed.path or "").lower().endswith(".pdf") and any(
        marker in f"{combined} {lowered_context}" for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS
    ):
        return True
    if urllib.parse.unquote(parsed.path or "").lower().endswith(".pdf") and any(
        marker in host for marker in _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_HOST_MARKERS
    ):
        if any(marker in lowered_context for marker in ("pdf", "download", "dokument", "unterlag", "plan", "oeffnen", "öffnen")):
            return True
    if not any(marker in host for marker in _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_HOST_MARKERS):
        return False
    if any(marker in combined for marker in ("zip", "alldoc", "download", "dokument", "beilage", "anlage", "unterlag", "gutachten")):
        return True
    return any(marker in lowered_context for marker in _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_CONTEXT_MARKERS)

def _property_scout_download_bytes(
    url: str,
    *,
    timeout_seconds: float = 12.0,
    max_bytes: int = _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_MAX_BYTES,
) -> tuple[bytes, str]:
    with start_span("provider_or_render_boundary") as telemetry_context:
        headers = {
            "User-Agent": _PROPERTY_SCOUT_USER_AGENT,
            "Accept": "application/zip,application/octet-stream,*/*;q=0.8",
        }
        inject_traceparent(headers, telemetry_context)
        request = urllib.request.Request(
            str(url or "").strip(),
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=float(timeout_seconds)) as response:
            try:
                content_length = int(str(response.headers.get("Content-Length") or "0").strip() or "0")
            except Exception:
                content_length = 0
            if content_length and content_length > max_bytes:
                raise ValueError("property_floorplan_archive_too_large")
            payload = response.read(max_bytes + 1)
            if len(payload) > max_bytes:
                raise ValueError("property_floorplan_archive_too_large")
            return payload, str(response.headers.get("Content-Type") or "").strip()

def _property_scout_public_asset_slug(*, source_url: str, archive_url: str) -> str:
    digest = hashlib.sha256(f"{source_url}|{archive_url}".encode("utf-8")).hexdigest()[:16]
    return f"property-assets-{digest}"

def _property_scout_public_asset_filename(*, member_name: str, ordinal: int) -> str:
    decoded_member_name = urllib.parse.unquote(str(member_name or "floorplan.pdf"))
    suffix = Path(decoded_member_name).suffix.lower()
    if suffix not in _PROPERTY_SCOUT_FLOORPLAN_ASSET_EXTENSIONS:
        suffix = ".pdf"
    stem = Path(decoded_member_name or "floorplan").stem
    safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip(".-").lower()[:72] or "floorplan"
    return f"floorplan-{ordinal:02d}-{safe_stem}{suffix}"

def _property_scout_zip_member_is_floorplan_candidate(member_name: str) -> bool:
    normalized = urllib.parse.unquote(str(member_name or "")).replace("_", " ").replace("-", " ").lower()
    suffix = Path(normalized).suffix.lower()
    if suffix not in _PROPERTY_SCOUT_FLOORPLAN_ASSET_EXTENSIONS:
        return False
    return any(marker in normalized for marker in _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_MEMBER_MARKERS)

def _property_scout_extract_floorplan_urls_from_archive(
    *,
    source_url: str,
    archive_url: str,
    context: str,
) -> tuple[str, ...]:
    try:
        payload, content_type = _property_scout_download_bytes(archive_url)
    except Exception:
        return ()
    archive_path = urllib.parse.unquote(urllib.parse.urlparse(str(archive_url or "")).path or "").lower()
    if payload.startswith(b"%PDF") or "application/pdf" in str(content_type or "").lower() or archive_path.endswith(".pdf"):
        if not payload or len(payload) > _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_MEMBER_MAX_BYTES:
            return ()
        lowered_context = str(context or "").lower()
        parsed_host = urllib.parse.urlparse(str(archive_url or "")).netloc.lower()
        archive_combined = urllib.parse.unquote(f"{parsed_host}{archive_path} {lowered_context}").lower()
        direct_provider_floorplan_pdf = (
            archive_path.endswith(".pdf")
            and any(marker in parsed_host for marker in _PROPERTY_SCOUT_LISTING_HOSTS)
            and any(marker in archive_combined for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS)
        )
        trusted_floorplan_document_context = any(marker in lowered_context for marker in _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_CONTEXT_MARKERS) or any(
            marker in parsed_host for marker in _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_HOST_MARKERS
        ) or direct_provider_floorplan_pdf or (
            archive_path.endswith(".pdf")
            and any(marker in f"{archive_combined} {lowered_context}" for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS)
        )
        if not trusted_floorplan_document_context:
            return ()
        slug = _property_scout_public_asset_slug(source_url=source_url, archive_url=archive_url)
        public_root = Path(str(os.getenv("EA_PUBLIC_TOUR_DIR") or "/docker/property/state/public_property_tours")).expanduser()
        target_dir = public_root / slug
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = _property_scout_public_asset_filename(
            member_name=Path(urllib.parse.urlparse(str(archive_url or "")).path or "floorplan.pdf").name or "floorplan.pdf",
            ordinal=1,
        )
        target = target_dir / filename
        target.write_bytes(payload)
        public_base = _property_public_app_base_url().rstrip("/")
        return (
            f"{public_base}/tours/files/"
            f"{urllib.parse.quote(slug, safe='')}/"
            f"{urllib.parse.quote(filename, safe='-._~')}",
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (zipfile.BadZipFile, ValueError):
        return ()
    strong_context = any(marker in str(context or "").lower() for marker in _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_CONTEXT_MARKERS)
    selected_members: list[zipfile.ZipInfo] = []
    fallback_members: list[zipfile.ZipInfo] = []
    try:
        for info in archive.infolist()[:120]:
            if info.is_dir() or info.file_size <= 0 or info.file_size > _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_MEMBER_MAX_BYTES:
                continue
            suffix = Path(str(info.filename or "")).suffix.lower()
            if suffix not in _PROPERTY_SCOUT_FLOORPLAN_ASSET_EXTENSIONS:
                continue
            if _property_scout_zip_member_is_floorplan_candidate(info.filename):
                selected_members.append(info)
            elif strong_context:
                fallback_members.append(info)
        if not selected_members:
            selected_members = fallback_members[:3]
        selected_members = selected_members[:6]
        if not selected_members:
            return ()
        slug = _property_scout_public_asset_slug(source_url=source_url, archive_url=archive_url)
        public_root = Path(str(os.getenv("EA_PUBLIC_TOUR_DIR") or "/docker/property/state/public_property_tours")).expanduser()
        target_dir = public_root / slug
        target_dir.mkdir(parents=True, exist_ok=True)
        public_base = _property_public_app_base_url().rstrip("/")
        urls: list[str] = []
        seen: set[str] = set()
        for ordinal, info in enumerate(selected_members, start=1):
            try:
                content = archive.read(info)
            except Exception:
                continue
            if not content or len(content) > _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_MEMBER_MAX_BYTES:
                continue
            filename = _property_scout_public_asset_filename(member_name=info.filename, ordinal=ordinal)
            target = target_dir / filename
            target.write_bytes(content)
            public_url = (
                f"{public_base}/tours/files/"
                f"{urllib.parse.quote(slug, safe='')}/"
                f"{urllib.parse.quote(filename, safe='-._~')}"
            )
            if public_url not in seen:
                seen.add(public_url)
                urls.append(public_url)
        return tuple(urls)
    finally:
        archive.close()

def _property_scout_extract_detail_media_urls(*, source_url: str, html: str) -> tuple[str, ...]:
    normalized_html = str(html or "").replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    raw_urls: list[str] = []
    raw_urls.extend(_extract_urls_from_text(normalized_html))
    raw_urls.extend(_property_scout_extract_meta_contents(normalized_html, "og:image"))
    for attr_name in ("src", "href", "data-src", "data-original", "data-lazy-src", "data-background-image"):
        raw_urls.extend(_property_scout_extract_html_attr_urls(source_url=source_url, html=normalized_html, attr_name=attr_name))
    media_urls: list[str] = []
    seen: set[str] = set()
    for raw_url in raw_urls:
        try:
            normalized = urllib.parse.urljoin(source_url, str(raw_url or "").strip())
        except ValueError:
            continue
        normalized = urllib.parse.urldefrag(normalized)[0]
        if not normalized or normalized in seen:
            continue
        if not _property_scout_is_asset_url(normalized, extensions=_PROPERTY_SCOUT_IMAGE_EXTENSIONS):
            continue
        seen.add(normalized)
        media_urls.append(normalized)
    return _property_scout_prioritized_media_urls(media_urls)

def _property_public_preview_cache_key(
    *,
    property_url: str,
    listing_id: str = "",
    property_facts: dict[str, object] | None = None,
) -> str:
    normalized_url = urllib.parse.urldefrag(str(property_url or "").strip())[0]
    facts = dict(property_facts or {})
    coarse_parts = [
        normalized_url,
        str(listing_id or "").strip(),
        str(facts.get("provider_channel") or facts.get("source_platform") or "").strip().lower(),
        str(facts.get("property_type") or "").strip().lower(),
        str(facts.get("postal_name") or facts.get("location") or "").strip().lower(),
        str(facts.get("rooms") or facts.get("rooms_label") or "").strip(),
        str(facts.get("area_sqm") or facts.get("area_label") or "").strip(),
        str(facts.get("total_rent_eur") or facts.get("purchase_price_eur") or "").strip(),
    ]
    return hashlib.sha256("|".join(coarse_parts).encode("utf-8", errors="ignore")).hexdigest()


def _property_public_preview_safe_facts(
    property_facts: dict[str, object] | None,
) -> dict[str, object]:
    """Project coarse scalar facts into the cross-principal preview cache.

    Distance values and their signed receipts are one inseparable trust unit:
    without the exact listing coordinates the receipt cannot be revalidated,
    while keeping the nested receipt would also disclose the coordinates that
    this public cache is designed to omit.  Public previews therefore retain
    coarse listing facts only; principal-scoped search/detail paths re-fetch
    exact nearby facts when they are needed.
    """
    allowed_scalar_keys = {
        "accessible",
        "area_label",
        "area_m2",
        "area_sqm",
        "availability_date",
        "availability_label",
        "available_from",
        "available_from_iso",
        "balcony_available",
        "barrier_free",
        "bathrooms",
        "bedrooms",
        "buy_price_display",
        "buy_price_eur",
        "category",
        "cold_rent_display",
        "cold_rent_eur",
        "condition",
        "country_code",
        "currency",
        "deal_type",
        "elevator_available",
        "energy_class",
        "floor",
        "floorplan_available",
        "floorplan_count",
        "floorplans_count",
        "furnished",
        "garden_available",
        "has_balcony",
        "has_elevator",
        "has_floorplan",
        "has_garden",
        "has_parking",
        "has_terrace",
        "heating_type",
        "is_new_build",
        "kaufpreis_display",
        "kaufpreis_eur",
        "listing_mode",
        "listing_type",
        "living_area_m2",
        "living_area_sqm",
        "marketing_type",
        "monthly_rent_eur",
        "monthly_rent_display",
        "move_in",
        "move_in_date",
        "object_type",
        "offer_type",
        "parking_available",
        "pets_allowed",
        "postal_code",
        "postal_name",
        "price",
        "price_display",
        "price_eur",
        "property_subtype",
        "property_type",
        "provider_channel",
        "purchase_price_display",
        "purchase_price_eur",
        "quiet_layout_signal",
        "region_code",
        "rent_display",
        "rent_eur",
        "reserve_price_eur",
        "rooms",
        "rooms_label",
        "source_family",
        "source_platform",
        "terrace_available",
        "total_rent_display",
        "total_rent_eur",
        "transaction_type",
        "usable_area_m2",
        "usable_area_sqm",
        "valuation_eur",
        "warm_rent_display",
        "warm_rent_eur",
        "wohnflaeche_sqm",
        "wohnfläche_sqm",
        "year_built",
    }
    unsafe_serialized_geo = re.compile(
        r"(?:map[_ -]?(?:lat|lng)|listing_(?:lat|lng|lon|latitude|longitude)|"
        r"poi_(?:lat|lng|lon|latitude|longitude)|location_(?:lat|lng|lon|latitude|longitude)|"
        r"property_fact_evidence|provider_attestation|exact_address|street_address|"
        r"house_number|[\"']?(?:lat|lng|lon|latitude|longitude)[\"']?\s*[:=])",
        flags=re.IGNORECASE,
    )
    unsafe_credential_text = re.compile(
        r"(?:api[_ -]?key|authorization|bearer|cookie|credential|internal|"
        r"oauth|password|secret|session|token)",
        flags=re.IGNORECASE,
    )
    coordinate_pair_text = re.compile(
        r"[-+]?\d{1,3}\.\d+\s*[,;/]\s*[-+]?\d{1,3}\.\d+",
    )
    exact_street_text = re.compile(
        r"(?:\b\d{1,5}[A-Za-z]?\s+[A-Za-zÀ-ž.' -]+\s+"
        r"(?:avenue|boulevard|drive|gasse|lane|platz|road|strasse|straße|street|weg)\b|"
        r"\b[A-Za-zÀ-ž.' -]*(?:gasse|platz|strasse|straße|street|weg)\s+\d{1,5}[A-Za-z]?\b)",
        flags=re.IGNORECASE,
    )
    provider_identifier_keys = {
        "provider_channel",
        "source_family",
        "source_platform",
    }
    bounded_coarse_place_keys = {"postal_name"}
    numeric_fact_keys = {
        "area_m2",
        "area_sqm",
        "bathrooms",
        "bedrooms",
        "buy_price_eur",
        "cold_rent_eur",
        "floorplan_count",
        "floorplans_count",
        "kaufpreis_eur",
        "living_area_m2",
        "living_area_sqm",
        "monthly_rent_eur",
        "price_eur",
        "purchase_price_eur",
        "rent_eur",
        "reserve_price_eur",
        "rooms",
        "total_rent_eur",
        "usable_area_m2",
        "usable_area_sqm",
        "valuation_eur",
        "warm_rent_eur",
        "wohnflaeche_sqm",
        "wohnfläche_sqm",
        "year_built",
    }
    boolean_fact_keys = {
        "accessible",
        "balcony_available",
        "barrier_free",
        "elevator_available",
        "floorplan_available",
        "furnished",
        "garden_available",
        "has_balcony",
        "has_elevator",
        "has_floorplan",
        "has_garden",
        "has_parking",
        "has_terrace",
        "is_new_build",
        "parking_available",
        "pets_allowed",
        "terrace_available",
    }
    projected: dict[str, object] = {}
    for raw_key, value in dict(property_facts or {}).items():
        key = str(raw_key or "").strip().casefold()
        if key not in allowed_scalar_keys or value is None or isinstance(
            value,
            (dict, list, tuple, set, bytes),
        ):
            continue
        if not isinstance(value, (str, int, float, bool)):
            continue
        if key in boolean_fact_keys:
            if isinstance(value, bool):
                pass
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isfinite(float(value)) or float(value) not in {0.0, 1.0}:
                    continue
                value = bool(int(value))
            elif isinstance(value, str):
                normalized_boolean = value.strip().casefold()
                if normalized_boolean in {"1", "true", "yes"}:
                    value = True
                elif normalized_boolean in {"0", "false", "no"}:
                    value = False
                else:
                    continue
            else:
                continue
        if key in numeric_fact_keys and isinstance(value, bool):
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if isinstance(value, str):
            normalized_value = value.strip()
            if (
                not normalized_value
                or len(normalized_value) > 160
                or unsafe_serialized_geo.search(normalized_value)
                or unsafe_credential_text.search(normalized_value)
                or coordinate_pair_text.search(normalized_value)
                or exact_street_text.search(normalized_value)
            ):
                continue
            if normalized_value[:1] in {"{", "["}:
                try:
                    decoded = json.loads(normalized_value)
                except (TypeError, ValueError):
                    decoded = None
                if isinstance(decoded, (dict, list)):
                    continue
            if key in provider_identifier_keys and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}",
                normalized_value,
            ):
                continue
            if key == "postal_code" and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9 -]{0,11}",
                normalized_value,
            ):
                continue
            if key in bounded_coarse_place_keys and (
                len(normalized_value) > 80
                or not re.fullmatch(
                    r"[A-Za-zÀ-ž0-9 .()'/-]+",
                    normalized_value,
                )
                or re.search(
                    r"\b(?:avenue|boulevard|drive|gasse|house|lane|platz|road|"
                    r"strasse|straße|street|weg)\b",
                    normalized_value,
                    flags=re.IGNORECASE,
                )
            ):
                continue
            if key in numeric_fact_keys and not re.fullmatch(
                r"[-+]?\d+(?:[.,]\d+)?",
                normalized_value.replace(" ", ""),
            ):
                continue
            if key in numeric_fact_keys:
                try:
                    if not math.isfinite(
                        float(normalized_value.replace(" ", "").replace(",", "."))
                    ):
                        continue
                except ValueError:
                    continue
            value = normalized_value
        projected[key] = value
    return projected


def _property_public_preview_cache_payload(preview: dict[str, object] | None) -> dict[str, object]:
    payload = dict(preview or {})
    facts = _property_public_preview_safe_facts(
        dict(payload.get("property_facts_json") or {})
    )
    return {
        "property_url": urllib.parse.urldefrag(str(payload.get("property_url") or payload.get("listing_id") or "").strip())[0],
        "listing_id": str(payload.get("listing_id") or "").strip(),
        "title_full": compact_text(str(payload.get("title_full") or payload.get("title") or "").strip(), fallback="", limit=500),
        "title": compact_text(str(payload.get("title") or "").strip(), fallback="", limit=160),
        "summary": compact_text(str(payload.get("summary") or "").strip(), fallback="", limit=400),
        "property_facts_json": facts,
        "media_urls_json": [
            str(item or "").strip()
            for item in list(payload.get("media_urls_json") or [])
            if str(item or "").strip()
        ][:12],
        "floorplan_urls_json": [
            str(item or "").strip()
            for item in list(payload.get("floorplan_urls_json") or [])
            if str(item or "").strip()
        ][:6],
        "source_virtual_tour_url": str(payload.get("source_virtual_tour_url") or "").strip(),
        "panorama_source": str(payload.get("panorama_source") or "").strip(),
    }

def _property_scout_extract_gallery_floorplan_urls(
    *,
    source_url: str,
    html: str,
    media_urls: tuple[str, ...],
    resolve_images: bool = False,
) -> tuple[tuple[str, ...], dict[str, object]]:
    normalized_html = str(html or "").replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    context_by_url: dict[str, str] = {}
    attr_pattern = re.compile(
        r"""(src|href|data-src|data-original|data-lazy-src|data-background-image|data-flickity-lazyload|data-flickity-bg-lazyload)=["']([^"']+)["']""",
        re.IGNORECASE,
    )
    for match in attr_pattern.finditer(normalized_html):
        raw_url = str(match.group(2) or "").strip()
        try:
            normalized = urllib.parse.urldefrag(urllib.parse.urljoin(source_url, raw_url))[0]
        except ValueError:
            continue
        if not normalized:
            continue
        tag_start = normalized_html.rfind("<", 0, match.start())
        tag_end = normalized_html.find(">", match.end())
        if tag_start < 0:
            tag_start = max(0, match.start() - 160)
        if tag_end < 0:
            tag_end = min(len(normalized_html), match.end() + 160)
        context = normalized_html[tag_start : min(len(normalized_html), tag_end + 1)].lower()
        context_by_url[normalized] = f"{context_by_url.get(normalized, '')} {context}"[:2500]
    urls: list[str] = []
    seen: set[str] = set()
    visual_checks: list[dict[str, object]] = []
    normalized_media_urls = list(_property_scout_prioritized_media_urls(list(media_urls or ())))
    for url in normalized_media_urls:
        if url in seen:
            continue
        lowered_url = urllib.parse.unquote(url).lower()
        context = context_by_url.get(url, "")
        marker_hit = any(marker in lowered_url or marker in context for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS)
        if marker_hit:
            seen.add(url)
            urls.append(url)
    if resolve_images:
        visual_candidates = [
            url
            for url in normalized_media_urls
            if url not in seen and _property_scout_is_asset_url(url, extensions=_PROPERTY_SCOUT_IMAGE_EXTENSIONS)
        ]
        high_signal_candidates = [url for url in visual_candidates if _property_scout_media_url_priority(url)[0] >= 0]
        sample_pool = high_signal_candidates or visual_candidates
        if len(sample_pool) > 8:
            visual_candidates = [*sample_pool[:4], *sample_pool[-4:]]
        else:
            visual_candidates = sample_pool
        for url in visual_candidates:
            try:
                payload, content_type = _property_scout_download_bytes(url, timeout_seconds=5.0, max_bytes=3 * 1024 * 1024)
            except Exception as exc:
                visual_checks.append({"url": url, "status": "download_failed", "error": compact_text(str(exc), fallback="", limit=120)})
                continue
            if "image" not in str(content_type or "").lower() and not _property_scout_is_asset_url(url, extensions=_PROPERTY_SCOUT_IMAGE_EXTENSIONS):
                continue
            looks_like, diagnostics = _property_scout_image_looks_like_floorplan(payload)
            diagnostics["url"] = url
            visual_checks.append(diagnostics)
            if looks_like and url not in seen:
                seen.add(url)
                urls.append(url)
    return tuple(urls), {
        "status": "gallery_floorplan_scan_completed",
        "media_url_count": len(normalized_media_urls),
        "marker_detected_total": len(urls),
        "visual_check_total": len(visual_checks),
        "visual_checks": visual_checks[:8],
    }

def _property_scout_extract_context_links(
    *,
    source_url: str,
    html: str,
    markers: tuple[str, ...],
    limit: int = 4,
) -> tuple[str, ...]:
    normalized_html = str(html or "").replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    normalized_markers = tuple(str(marker or "").strip().lower() for marker in markers if str(marker or "").strip())
    if not normalized_markers:
        return ()
    urls: list[str] = []
    seen: set[str] = set()
    attr_pattern = re.compile(r"""href=["']([^"']+)["']""", re.IGNORECASE)
    for match in attr_pattern.finditer(normalized_html):
        raw_url = str(match.group(1) or "").strip()
        tag_start = normalized_html.rfind("<", 0, match.start())
        tag_end = normalized_html.find(">", match.end())
        if tag_start < 0:
            tag_start = max(0, match.start() - 120)
        if tag_end < 0:
            tag_end = min(len(normalized_html), match.end() + 120)
        context = normalized_html[tag_start : tag_end + 1].lower()
        close = normalized_html.find("</a>", tag_end)
        if close >= 0 and close - tag_end < 420:
            context += normalized_html[tag_end + 1 : close + 4].lower()
        if not any(marker in context for marker in normalized_markers):
            continue
        try:
            normalized = urllib.parse.urljoin(source_url, raw_url)
        except ValueError:
            continue
        normalized = urllib.parse.urldefrag(normalized)[0]
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
        if len(urls) >= max(1, int(limit)):
            break
    return tuple(urls)

def _property_scout_extract_siedlungsunion_attachment_links(*, source_url: str, html: str) -> tuple[tuple[str, str, str], ...]:
    if "siedlungsunion.at" not in urllib.parse.urlparse(str(source_url or "")).netloc.lower():
        return ()
    match = re.search(r"app\.attachments\s*=\s*(\[.*?\])\s*;", str(html or ""), re.IGNORECASE | re.DOTALL)
    if not match:
        return ()
    try:
        attachments = json.loads(match.group(1))
    except Exception:
        return ()
    if not isinstance(attachments, list):
        return ()
    links: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        attachment_type = dict(attachment.get("attachmentType") or {}) if isinstance(attachment.get("attachmentType"), dict) else {}
        if str(attachment_type.get("name") or "").strip().lower() != "file":
            continue
        file_key = str(attachment.get("file") or "").strip()
        name = str(attachment.get("name") or "").strip()
        if not file_key or not name:
            continue
        lowered_name = name.lower()
        if any(marker in lowered_name for marker in ("energieausweis", "energy", "ausweis")):
            continue
        if not any(marker in lowered_name for marker in _PROPERTY_SCOUT_FLOORPLAN_ARCHIVE_MEMBER_MARKERS):
            continue
        raw_url = f"/rest/file/{urllib.parse.quote(file_key, safe='')}/{urllib.parse.quote(name, safe='-._~')}"
        normalized = urllib.parse.urljoin(source_url, raw_url)
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(("siedlungsunion_attachment", normalized, name.lower()))
    return tuple(links)

def _property_scout_floorplan_recovery_diagnostics(*, source_url: str, html: str) -> dict[str, object]:
    normalized_html = str(html or "").replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    host = urllib.parse.urlparse(str(source_url or "")).netloc.lower()
    lowered = normalized_html.lower()
    marker_hits = [
        marker
        for marker in (*_PROPERTY_SCOUT_FLOORPLAN_MARKERS, "pdf", "download", "dokument", "unterlagen")
        if marker in lowered
    ][:20]
    attr_names = (
        "href",
        "src",
        "data-src",
        "data-original",
        "data-lazy-src",
        "data-background-image",
        "data-flickity-lazyload",
        "data-flickity-bg-lazyload",
    )
    candidate_urls: list[str] = []
    for attr_name in attr_names:
        for raw_url in _property_scout_extract_html_attr_urls(source_url=source_url, html=normalized_html, attr_name=attr_name):
            normalized = urllib.parse.urljoin(source_url, str(raw_url or "").strip())
            lowered_url = urllib.parse.unquote(normalized).lower()
            if any(marker in lowered_url for marker in ("pdf", "download", "file", "plan", "grundriss", "mmo/", "storage.justimmo.at")):
                candidate_urls.append(normalized)
            if len(candidate_urls) >= 12:
                break
        if len(candidate_urls) >= 12:
            break
    attachment_links = _property_scout_extract_siedlungsunion_attachment_links(source_url=source_url, html=normalized_html)
    return {
        "status": "floorplan_not_found_after_deep_scan",
        "provider_host": host,
        "html_size_bytes": len(normalized_html.encode("utf-8", errors="ignore")),
        "floorplan_marker_hits": marker_hits,
        "candidate_document_or_media_url_count": len(candidate_urls),
        "candidate_document_or_media_urls": candidate_urls[:8],
        "siedlungsunion_attachment_candidate_count": len(attachment_links),
        "siedlungsunion_attachment_candidate_urls": [item[1] for item in attachment_links[:4]],
        "recommended_ooda": [
            "Re-fetch the detail page.",
            "Inspect media/gallery/document/link attributes around marker hits.",
            "Patch provider extractor with a regression fixture.",
            "Rerun property_release_gates.sh before deploy.",
        ],
    }

def _property_scout_extract_floorplan_urls(*, source_url: str, html: str, resolve_archives: bool = False) -> tuple[str, ...]:
    normalized_html = str(html or "").replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    floorplan_urls: list[str] = []
    seen: set[str] = set()
    seen_archives: set[str] = set()
    candidate_links: list[tuple[str, str, str]] = []
    attr_pattern = re.compile(
        r"""(href|src|action|data-href|data-url|data-src|data-original|data-lazy-src|data-background-image|data-flickity-lazyload|data-flickity-bg-lazyload)=["']([^"']+)["']""",
        re.IGNORECASE,
    )
    for match in attr_pattern.finditer(normalized_html):
        attr_name = str(match.group(1) or "").strip().lower()
        raw_url = str(match.group(2) or "").strip()
        tag_start = normalized_html.rfind("<", 0, match.start())
        tag_end = normalized_html.find(">", match.end())
        if tag_start < 0:
            tag_start = max(0, match.start() - 80)
        if tag_end < 0:
            tag_end = min(len(normalized_html), match.end() + 80)
        context = normalized_html[tag_start : tag_end + 1].lower()
        if attr_name == "href":
            close = normalized_html.find("</a>", tag_end)
            if close >= 0 and close - tag_end < 320:
                context += normalized_html[tag_end + 1 : close + 4].lower()
        elif attr_name == "action":
            close = normalized_html.find("</form>", tag_end)
            if close >= 0 and close - tag_end < 520:
                context += normalized_html[tag_end + 1 : close + 7].lower()
        candidate_links.append((attr_name, raw_url, context))
    js_url_pattern = re.compile(
        r"""(?:window\.open|location(?:\.href)?|document\.location)\s*(?:=|\()\s*["']([^"']+)["']""",
        re.IGNORECASE,
    )
    for match in js_url_pattern.finditer(normalized_html):
        raw_url = str(match.group(1) or "").strip()
        context = normalized_html[max(0, match.start() - 180) : min(len(normalized_html), match.end() + 360)].lower()
        candidate_links.append(("script", raw_url, context))
    embedded_download_pattern = re.compile(
        r"""["']((?:https?://[^"']+|/(?:[^"'<>{}\s]+)|(?:alldoc/[^"']+|download[^"']*|downloads/[^"']+)))["']""",
        re.IGNORECASE,
    )
    for match in embedded_download_pattern.finditer(normalized_html):
        raw_url = str(match.group(1) or "").strip()
        if re.search(r"\s", raw_url):
            continue
        context = normalized_html[max(0, match.start() - 180) : min(len(normalized_html), match.end() + 360)].lower()
        combined = f"{raw_url} {context}".lower()
        if not any(marker in combined for marker in ("zip", "alldoc", "download", "dokument", "beilage", "anlage", "unterlag", "gutachten", "grundriss", "floorplan", "plan")):
            continue
        try:
            embedded_normalized = urllib.parse.urljoin(source_url, raw_url)
        except ValueError:
            continue
        lowered_raw_url = urllib.parse.unquote(raw_url).lower()
        if _property_scout_is_asset_url(embedded_normalized, extensions=_PROPERTY_SCOUT_IMAGE_EXTENSIONS) and not any(
            marker in lowered_raw_url for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS
        ):
            continue
        candidate_links.append(("embedded", raw_url, context))
    candidate_links.extend(_property_scout_extract_siedlungsunion_attachment_links(source_url=source_url, html=normalized_html))
    for attr_name, raw_url, context in candidate_links:
        try:
            normalized = urllib.parse.urljoin(source_url, raw_url)
        except ValueError:
            continue
        normalized = urllib.parse.urldefrag(normalized)[0]
        if not normalized or normalized in seen:
            continue
        lowered_url = urllib.parse.unquote(normalized).lower()
        is_direct_floorplan_asset = _property_scout_is_asset_url(normalized, extensions=_PROPERTY_SCOUT_FLOORPLAN_ASSET_EXTENSIONS)
        if (
            resolve_archives
            and lowered_url.endswith(".pdf")
            and _property_scout_is_floorplan_archive_candidate_url(url=normalized, context=context)
        ):
            if normalized not in seen_archives:
                seen_archives.add(normalized)
                archived_urls = _property_scout_extract_floorplan_urls_from_archive(
                    source_url=source_url,
                    archive_url=normalized,
                    context=context,
                )
                for archived_floorplan_url in archived_urls:
                    if archived_floorplan_url not in seen:
                        seen.add(archived_floorplan_url)
                        floorplan_urls.append(archived_floorplan_url)
                if archived_urls:
                    seen.add(normalized)
                    continue
        if resolve_archives and not is_direct_floorplan_asset and _property_scout_is_floorplan_archive_candidate_url(url=normalized, context=context):
            if normalized not in seen_archives:
                seen_archives.add(normalized)
                for archived_floorplan_url in _property_scout_extract_floorplan_urls_from_archive(
                    source_url=source_url,
                    archive_url=normalized,
                    context=context,
                ):
                    if archived_floorplan_url not in seen:
                        seen.add(archived_floorplan_url)
                        floorplan_urls.append(archived_floorplan_url)
                seen.add(normalized)
            continue
        if not is_direct_floorplan_asset:
            continue
        is_image_floorplan_asset = _property_scout_is_asset_url(normalized, extensions=_PROPERTY_SCOUT_IMAGE_EXTENSIONS)
        if is_image_floorplan_asset and not any((marker in lowered_url) or (marker in context) for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS):
            continue
        if attr_name in {"src", "data-src", "data-original", "data-lazy-src", "data-background-image"} and not any(
            (marker in lowered_url) or (marker in context) for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS
        ):
            continue
        is_edikte_valuation_pdf = (
            lowered_url.endswith(".pdf")
            and any(marker in urllib.parse.urlparse(normalized).netloc.lower() for marker in ("edikte.justiz.gv.at", "edikte2.justiz.gv.at"))
            and any(
                marker in context
                for marker in (
                    "langgutachten",
                    "kurzgutachten",
                    "schätzungsgutachten",
                    "schaetzungsgutachten",
                    "gutachten",
                )
            )
        )
        if not is_edikte_valuation_pdf and not any((marker in lowered_url) or (marker in context) for marker in _PROPERTY_SCOUT_FLOORPLAN_MARKERS):
            continue
        seen.add(normalized)
        floorplan_urls.append(normalized)
    return tuple(floorplan_urls)

def _property_scout_extract_source_virtual_tour_url(*, source_url: str, html: str) -> str:
    candidates: list[str] = []
    normalized_html = str(html or "").replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    candidates.extend(_extract_urls_from_text(normalized_html))
    candidates.extend(_property_scout_extract_html_attr_urls(source_url=source_url, html=normalized_html, attr_name="href"))
    candidates.extend(_property_scout_extract_html_attr_urls(source_url=source_url, html=normalized_html, attr_name="src"))
    for raw in candidates:
        normalized = urllib.parse.urldefrag(str(raw or "").strip())[0]
        if not normalized:
            continue
        parsed = urllib.parse.urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        host = parsed.netloc.lower()
        path = parsed.path.lower()
        if host == "api.willhaben.at" and ("/logevent/" in path or path.endswith("/virtual-tour-link-clicked")):
            continue
        combined = f"{host}{path}"
        if any(marker in combined for marker in _PROPERTY_SCOUT_360_HOST_MARKERS):
            return normalized
    return ""
