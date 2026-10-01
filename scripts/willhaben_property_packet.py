#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import html
import io
import json
import math
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
import time

import requests
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
EA_ROOT = ROOT / "ea"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(EA_ROOT) not in sys.path:
    sys.path.insert(0, str(EA_ROOT))


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0 Safari/537.36"
NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL)
NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_FALLBACK_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
GROCERY_SHOP_TAGS = frozenset({"supermarket", "convenience", "greengrocer"})
DEFAULT_LIVABILITY_CACHE_FILE = "/data/property_livability_cache.json"
LIVABILITY_CACHE_TTL_SECONDS = 7 * 24 * 3600
LIVABILITY_CACHE_MAX_ENTRIES = 512
FLOORPLAN_TOKENS = (
    "grundriss",
    "lageplan",
    "raumplan",
    "floorplan",
    "plan",
    "schnitt",
    "skizze",
)
PANORAMA_TOKENS = (
    "360",
    "panorama",
    "photosphere",
    "equirectangular",
    "theta",
    "insta360",
    "gopro max",
)
PANORAMA_TOKEN_PATTERNS = tuple(
    re.compile(
        r"(^|[^0-9a-z])" + re.escape(token) + r"([^0-9a-z]|$)",
        re.IGNORECASE,
    )
    if token == "360"
    else re.compile(re.escape(token), re.IGNORECASE)
    for token in PANORAMA_TOKENS
)
HTML_FLOORPLAN_IMAGE_RE = re.compile(
    r'https://cache\.willhaben\.at/mmo/[0-9/._-]+\.jpg(?=[^>]{0,240}(?:Grundriss|grundriss))',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Variant:
    variant_key: str
    scene_strategy: str
    theme_name: str
    tour_style: str
    audience: str
    creative_brief: str
    call_to_action: str
    scene_selection_json: dict[str, object]
    tour_settings_json: dict[str, object]


def fetch_html(url: str) -> str:
    from app.product.outbound_url_security import request_get_with_guarded_redirects

    response = request_get_with_guarded_redirects(
        requests.get,
        url,
        allowed_hosts=("willhaben.at",),
        headers={"User-Agent": USER_AGENT},
        timeout=60,
    )
    response.raise_for_status()
    return response.text


def extract_next_data(html: str) -> dict[str, object]:
    match = NEXT_DATA_RE.search(html)
    if not match:
        raise RuntimeError("willhaben_next_data_missing")
    loaded = json.loads(match.group(1))
    if not isinstance(loaded, dict):
        raise RuntimeError("willhaben_next_data_invalid")
    return loaded


def deep_get(mapping: object, *keys: str) -> object:
    current = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = re.sub(r"<[^>]+>", " ", value)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [as_text(entry) for entry in value]
        return " | ".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("value", "label", "text", "name"):
            text = as_text(value.get(key))
            if text:
                return text
        return ""
    return str(value).strip()


def normalize_attribute_value(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        result: list[str] = []
        for entry in raw:
            text = as_text(entry)
            if text:
                result.append(text)
        return result
    if isinstance(raw, dict):
        nested = raw.get("value")
        if nested is not None and nested is not raw:
            return normalize_attribute_value(nested)
        text = as_text(raw)
        return [text] if text else []
    text = as_text(raw)
    return [text] if text else []


def extract_advert_details(next_data: dict[str, object]) -> dict[str, object]:
    page_props = deep_get(next_data, "props", "pageProps")
    advert = page_props.get("advertDetails") if isinstance(page_props, dict) else None
    if not isinstance(advert, dict):
        expired_ad_id = page_props.get("fromExpiredAdId") if isinstance(page_props, dict) else None
        if as_text(expired_ad_id):
            raise RuntimeError("willhaben_listing_expired")
        raise RuntimeError("willhaben_advert_details_missing")
    return advert


def load_advert(url: str) -> dict[str, object]:
    return extract_advert_details(extract_next_data(fetch_html(url)))


def extract_attributes(advert: dict[str, object]) -> dict[str, list[str]]:
    raw = deep_get(advert, "attributes", "attribute")
    if not isinstance(raw, list):
        return {}
    attributes: dict[str, list[str]] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = as_text(entry.get("name"))
        if not name:
            continue
        values = normalize_attribute_value(entry.get("values"))
        if not values:
            values = normalize_attribute_value(entry.get("value"))
        if values:
            attributes[name] = values
    return attributes


def numeric_from_text(value: str) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"[^0-9,.\-]", "", text)
    if not text:
        return None
    if "." in text and "," in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    elif text.count(".") > 1:
        head, tail = text.rsplit(".", 1)
        text = head.replace(".", "") + "." + tail
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except Exception:
        return None


def pick_first_attribute(attributes: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = attributes.get(name) or []
        if values:
            return values[0]
    return ""


def parse_coordinate_pair(value: object) -> tuple[float, float] | None:
    text = as_text(value)
    if not text or "," not in text:
        return None
    left, right = text.split(",", 1)
    try:
        return float(left.strip()), float(right.strip())
    except Exception:
        return None


def infer_panorama_source(url: str) -> str:
    host = urllib.parse.urlparse(str(url or "").strip()).netloc.lower()
    if "kalandra" in host:
        return "feelestate_kalandra"
    return host or "external_virtual_tour"


def extract_virtual_tour(attributes: dict[str, list[str]]) -> tuple[str, str]:
    candidates = attributes.get("INFOLINK/URL") or []
    for value in candidates:
        url = as_text(value)
        if not url:
            continue
        lowered = url.lower()
        if "/view/portal/" in lowered or "360." in lowered or "virtual-tour" in lowered:
            return url, infer_panorama_source(url)
    return "", ""


def _request_json(
    url: str,
    *,
    params: dict[str, object] | None = None,
    method: str = "GET",
    data: bytes | None = None,
    timeout: float = 60.0,
) -> object:
    request_url = url
    payload = data
    headers = {"User-Agent": USER_AGENT}
    if method.upper() == "GET" and params:
        request_url = f"{url}?{urllib.parse.urlencode(params)}"
    elif method.upper() == "POST":
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"
        if payload is None and params is not None:
            payload = urllib.parse.urlencode(params).encode("utf-8")
    response = requests.request(method.upper(), request_url, headers=headers, data=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _request_json_with_headers(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, object] | None = None,
    data: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> object:
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    response = requests.request(method.upper(), url, params=params, data=data, headers=merged_headers, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _livability_cache_file() -> Path:
    configured = str(os.getenv("EA_PROPERTY_LIVABILITY_CACHE_FILE") or "").strip()
    return Path(configured or DEFAULT_LIVABILITY_CACHE_FILE)


def _livability_cache_lock_file() -> Path:
    return _livability_cache_file().with_suffix(_livability_cache_file().suffix + ".lock")


def _livability_cache_key(lat: float, lon: float) -> str:
    return f"v2:{lat:.5f},{lon:.5f}"


def _livability_cache_ttl_seconds() -> float:
    try:
        configured = float(str(os.getenv("EA_PROPERTY_LIVABILITY_CACHE_TTL_SECONDS") or "").strip() or LIVABILITY_CACHE_TTL_SECONDS)
    except Exception:
        configured = float(LIVABILITY_CACHE_TTL_SECONDS)
    return max(60.0, configured)


def _livability_cache_max_entries() -> int:
    try:
        configured = int(str(os.getenv("EA_PROPERTY_LIVABILITY_CACHE_MAX_ENTRIES") or "").strip() or LIVABILITY_CACHE_MAX_ENTRIES)
    except Exception:
        configured = LIVABILITY_CACHE_MAX_ENTRIES
    return max(1, configured)


def _prune_livability_cache(payload: dict[str, object]) -> dict[str, object]:
    now = time.time()
    ttl_seconds = _livability_cache_ttl_seconds()
    rows: list[tuple[str, float, dict[str, object]]] = []
    for key, raw in payload.items():
        if not isinstance(raw, dict):
            continue
        snapshot = raw.get("snapshot")
        if not isinstance(snapshot, dict):
            continue
        try:
            cached_at = float(raw.get("cached_at"))
        except Exception:
            continue
        if now - cached_at > ttl_seconds:
            continue
        rows.append((str(key), cached_at, dict(snapshot)))
    rows.sort(key=lambda item: item[1], reverse=True)
    limited = rows[: _livability_cache_max_entries()]
    return {
        key: {
            "cached_at": cached_at,
            "snapshot": snapshot,
        }
        for key, cached_at, snapshot in limited
    }


@contextlib.contextmanager
def _livability_cache_lock() -> object:
    lock_path = _livability_cache_lock_file()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield handle
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        yield None


def _read_livability_cache_unlocked() -> dict[str, object]:
    path = _livability_cache_file()
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except Exception:
        return {}
    return _prune_livability_cache(loaded) if isinstance(loaded, dict) else {}


def _load_livability_cache() -> dict[str, object]:
    with _livability_cache_lock():
        return _read_livability_cache_unlocked()


def _save_livability_cache(payload: dict[str, object]) -> None:
    path = _livability_cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(_prune_livability_cache(payload), ensure_ascii=True, indent=2))
    temporary.replace(path)


def _cached_livability_snapshot(lat: float, lon: float) -> dict[str, object] | None:
    payload = _load_livability_cache()
    row = payload.get(_livability_cache_key(lat, lon))
    if not isinstance(row, dict):
        return None
    cached_at = row.get("cached_at")
    try:
        cached_at_ts = float(cached_at)
    except Exception:
        return None
    if time.time() - cached_at_ts > _livability_cache_ttl_seconds():
        return None
    snapshot = row.get("snapshot")
    return dict(snapshot) if isinstance(snapshot, dict) else None


def _store_livability_snapshot(lat: float, lon: float, snapshot: dict[str, object]) -> None:
    try:
        with _livability_cache_lock():
            payload = _read_livability_cache_unlocked()
            payload[_livability_cache_key(lat, lon)] = {
                "cached_at": time.time(),
                "snapshot": dict(snapshot),
            }
            _save_livability_cache(payload)
    except Exception:
        return None


def _overpass_json(query: str) -> dict[str, object]:
    errors: list[str] = []
    for url in OVERPASS_FALLBACK_URLS:
        for method, kwargs in (
            ("POST", {"data": {"data": query}}),
            ("GET", {"params": {"data": query}}),
        ):
            try:
                payload = _request_json_with_headers(url, method=method, timeout=6.0, **kwargs)
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:
                errors.append(f"{method}:{url}:{type(exc).__name__}")
                continue
    raise RuntimeError("overpass_unavailable:" + ",".join(errors))


def _nominatim_nearest_distance(lat: float, lon: float, *, query: str) -> int | None:
    try:
        payload = _request_json(
            NOMINATIM_SEARCH_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 3,
                "viewbox": f"{lon - 0.03},{lat + 0.03},{lon + 0.03},{lat - 0.03}",
                "bounded": 1,
            },
            timeout=4.0,
        )
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    distances: list[int] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        try:
            target_lat = float(raw.get("lat"))
            target_lon = float(raw.get("lon"))
        except Exception:
            continue
        distances.append(int(round(_haversine_distance_meters(lat, lon, target_lat, target_lon))))
    return min(distances) if distances else None


def _nominatim_nearest_distance_any(lat: float, lon: float, *, queries: tuple[str, ...]) -> int | None:
    distances = [
        _nominatim_nearest_distance(lat, lon, query=query)
        for query in queries
    ]
    numeric = [int(value) for value in distances if isinstance(value, int)]
    return min(numeric) if numeric else None


def geocode_listing_location(*, address_lines: list[str], postal_code: str, postal_name: str, country: str) -> dict[str, object]:
    query = ", ".join(part for part in [*(address_lines or []), postal_code, postal_name, country] if str(part or "").strip())
    if not query:
        return {}
    try:
        payload = _request_json(
            NOMINATIM_SEARCH_URL,
            params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            timeout=6.0,
        )
    except Exception:
        return {}
    if not isinstance(payload, list) or not payload:
        return {}
    entry = payload[0] if isinstance(payload[0], dict) else {}
    try:
        lat = float(entry.get("lat"))
        lon = float(entry.get("lon"))
    except Exception:
        return {}
    return {
        "query": query,
        "lat": lat,
        "lon": lon,
        "display_name": as_text(entry.get("display_name")),
    }


def _haversine_distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    return 2.0 * earth_radius_m * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _nearest_distance_from_elements(lat: float, lon: float, elements: list[object]) -> int | None:
    distances: list[int] = []
    for raw in elements:
        if not isinstance(raw, dict):
            continue
        center = raw.get("center") if isinstance(raw.get("center"), dict) else raw
        try:
            target_lat = float(center.get("lat"))  # type: ignore[arg-type]
            target_lon = float(center.get("lon"))  # type: ignore[arg-type]
        except Exception:
            continue
        distances.append(int(round(_haversine_distance_meters(lat, lon, target_lat, target_lon))))
    return min(distances) if distances else None


def nearby_livability_snapshot(lat: float, lon: float) -> dict[str, object]:
    cached = _cached_livability_snapshot(lat, lon)
    if isinstance(cached, dict):
        return cached
    query = """
    [out:json][timeout:25];
    (
      nwr(around:1800,%(lat).6f,%(lon).6f)[amenity=pharmacy];
      nwr(around:1800,%(lat).6f,%(lon).6f)[shop=supermarket];
      nwr(around:1800,%(lat).6f,%(lon).6f)[shop=convenience];
      nwr(around:1800,%(lat).6f,%(lon).6f)[shop=greengrocer];
      nwr(around:1800,%(lat).6f,%(lon).6f)[amenity=bicycle_parking];
      nwr(around:1800,%(lat).6f,%(lon).6f)[leisure=playground];
      way(around:1800,%(lat).6f,%(lon).6f)[highway=cycleway];
      nwr(around:1800,%(lat).6f,%(lon).6f)[railway=tram_stop];
      nwr(around:1800,%(lat).6f,%(lon).6f)[railway=station];
      nwr(around:1800,%(lat).6f,%(lon).6f)[station=subway];
      nwr(around:1800,%(lat).6f,%(lon).6f)[public_transport=station];
      nwr(around:1800,%(lat).6f,%(lon).6f)[highway=bus_stop];
      nwr(around:1800,%(lat).6f,%(lon).6f)[amenity=school];
      nwr(around:1800,%(lat).6f,%(lon).6f)[shop=bakery];
      nwr(around:1800,%(lat).6f,%(lon).6f)[leisure=park];
      nwr(around:1800,%(lat).6f,%(lon).6f)[leisure=track];
      nwr(around:1800,%(lat).6f,%(lon).6f)[natural=wood];
    );
    out center;
    """ % {"lat": lat, "lon": lon}
    try:
        payload = _overpass_json(query)
    except Exception:
        payload = {}
    elements = list(payload.get("elements") or []) if isinstance(payload, dict) else []
    grouped: dict[str, list[object]] = {
        "pharmacy": [],
        "supermarket": [],
        "bakery": [],
        "bicycle_parking": [],
        "cycleway": [],
        "playground": [],
        "school": [],
        "transit": [],
        "tram_bus": [],
        "subway": [],
        "running": [],
    }
    for raw in elements:
        if not isinstance(raw, dict):
            continue
        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        amenity = str(tags.get("amenity") or "").strip().lower()
        shop = str(tags.get("shop") or "").strip().lower()
        leisure = str(tags.get("leisure") or "").strip().lower()
        railway = str(tags.get("railway") or "").strip().lower()
        station = str(tags.get("station") or "").strip().lower()
        public_transport = str(tags.get("public_transport") or "").strip().lower()
        highway = str(tags.get("highway") or "").strip().lower()
        natural = str(tags.get("natural") or "").strip().lower()
        if amenity == "pharmacy":
            grouped["pharmacy"].append(raw)
        if shop in GROCERY_SHOP_TAGS:
            grouped["supermarket"].append(raw)
        if amenity == "bicycle_parking":
            grouped["bicycle_parking"].append(raw)
        if shop == "bakery":
            grouped["bakery"].append(raw)
        if leisure == "playground":
            grouped["playground"].append(raw)
        if highway == "cycleway":
            grouped["cycleway"].append(raw)
        if amenity == "school":
            grouped["school"].append(raw)
        if railway == "tram_stop" or highway == "bus_stop":
            grouped["tram_bus"].append(raw)
            grouped["transit"].append(raw)
        if railway == "station" or station == "subway" or public_transport == "station":
            grouped["subway"].append(raw)
            grouped["transit"].append(raw)
        if leisure in {"park", "track"} or natural == "wood":
            grouped["running"].append(raw)
    result = {
        "nearest_pharmacy_m": _nearest_distance_from_elements(lat, lon, grouped["pharmacy"]),
        "nearest_supermarket_m": _nearest_distance_from_elements(lat, lon, grouped["supermarket"]),
        "nearest_bakery_m": _nearest_distance_from_elements(lat, lon, grouped["bakery"]),
        "nearest_bicycle_parking_m": _nearest_distance_from_elements(lat, lon, grouped["bicycle_parking"]),
        "nearest_cycleway_m": _nearest_distance_from_elements(lat, lon, grouped["cycleway"]),
        "nearest_playground_m": _nearest_distance_from_elements(lat, lon, grouped["playground"]),
        "nearest_school_m": _nearest_distance_from_elements(lat, lon, grouped["school"]),
        "nearest_transit_m": _nearest_distance_from_elements(lat, lon, grouped["transit"]),
        "nearest_tram_bus_m": _nearest_distance_from_elements(lat, lon, grouped["tram_bus"]),
        "nearest_subway_m": _nearest_distance_from_elements(lat, lon, grouped["subway"]),
        "nearest_running_m": _nearest_distance_from_elements(lat, lon, grouped["running"]),
    }
    fallback_queries = (
        ("nearest_pharmacy_m", "pharmacy"),
        ("nearest_supermarket_m", "supermarket"),
        ("nearest_bicycle_parking_m", "bicycle parking"),
        ("nearest_running_m", "park"),
        ("nearest_playground_m", "playground"),
    )
    for key, query_text in fallback_queries:
        if result.get(key) is None:
            result[key] = _nominatim_nearest_distance(lat, lon, query=query_text)
    if result.get("nearest_transit_m") is None:
        result["nearest_transit_m"] = _nominatim_nearest_distance_any(
            lat,
            lon,
            queries=("subway station", "tram stop", "bus stop"),
        )
    if result.get("nearest_tram_bus_m") is None:
        result["nearest_tram_bus_m"] = _nominatim_nearest_distance_any(
            lat,
            lon,
            queries=("tram stop", "bus stop"),
        )
    if result.get("nearest_subway_m") is None:
        result["nearest_subway_m"] = _nominatim_nearest_distance_any(
            lat,
            lon,
            queries=("subway station", "u-bahn"),
        )
    _store_livability_snapshot(lat, lon, result)
    return result


def looks_like_floorplan(*values: object) -> bool:
    haystack = " ".join(as_text(value).lower() for value in values if as_text(value))
    return any(token in haystack for token in FLOORPLAN_TOKENS)


def best_image_url(image: dict[str, object]) -> str:
    # Willhaben currently exposes ``mainImageUrl`` as the 400 px ``_hoved``
    # derivative while ``referenceImageUrl`` points at the 1024 px listing
    # image.  Tour/reconstruction inputs need the source-sized reference; the
    # small derivative is only suitable as a card thumbnail.
    for key in ("referenceImageUrl", "largeImageUrl", "mainImageUrl", "middleImageUrl", "smallImageUrl"):
        text = as_text(image.get(key))
        if text:
            return text
    return ""


def inspect_panorama_signal(url: str, description: str) -> dict[str, object]:
    lowered_markers = " ".join(part for part in (str(url or ""), str(description or ""))).lower()
    marker_match = any(pattern.search(lowered_markers) for pattern in PANORAMA_TOKEN_PATTERNS)
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=45)
        response.raise_for_status()
        data = response.content
    except Exception as exc:
        return {
            "panorama_candidate": marker_match,
            "panorama_reason": "marker_only" if marker_match else f"inspect_failed:{type(exc).__name__}",
            "width": None,
            "height": None,
            "aspect_ratio": None,
        }
    width = None
    height = None
    aspect_ratio = None
    floorplan_candidate = False
    floorplan_reason = ""
    try:
        with Image.open(io.BytesIO(data)) as image:
            rgb_image = image.convert("RGB")
            width, height = rgb_image.size
            if width and height:
                aspect_ratio = round(width / height, 4)
                # Many Willhaben listings upload floorplans as plain gallery images rather
                # than structured floorPlan attachments. Accept smaller document images too,
                # but keep the heuristic narrow enough to avoid swallowing energy-certificate
                # sheets or other scanned paperwork.
                if min(width, height) >= 260 and 0.65 <= (width / height) <= 1.45:
                    probe = rgb_image.resize((220, 220))
                    pixel_count = 0
                    white_pixels = 0
                    low_saturation_pixels = 0
                    dark_pixels = 0
                    colorful_pixels = 0
                    pixels = (
                        probe.get_flattened_data()
                        if hasattr(probe, "get_flattened_data")
                        else probe.getdata()
                    )
                    for r, g, b in pixels:
                        pixel_count += 1
                        if r >= 232 and g >= 232 and b >= 232:
                            white_pixels += 1
                        if max(r, g, b) > 0 and (max(r, g, b) - min(r, g, b)) / max(r, g, b) <= 0.10:
                            low_saturation_pixels += 1
                        if max(r, g, b) <= 90:
                            dark_pixels += 1
                        if max(abs(r - g), abs(r - b), abs(g - b)) > 40:
                            colorful_pixels += 1
                    white_ratio = white_pixels / float(pixel_count or 1)
                    low_saturation_ratio = low_saturation_pixels / float(pixel_count or 1)
                    dark_ratio = dark_pixels / float(pixel_count or 1)
                    colorful_ratio = colorful_pixels / float(pixel_count or 1)
                    if (
                        white_ratio >= 0.70
                        and low_saturation_ratio >= 0.90
                        and dark_ratio <= 0.04
                        and colorful_ratio <= 0.05
                    ):
                        floorplan_candidate = True
                        floorplan_reason = "plan_like_document_image"
    except Exception:
        width = None
        height = None
    if width and height:
        aspect_ratio = round(width / height, 4)
    text_probe = data[:262144].decode("latin-1", errors="ignore").lower()
    xmp_equirectangular = "projectiontype" in text_probe and "equirectangular" in text_probe
    wide_equirectangular = bool(width and height and width >= 2000 and 1.9 <= (width / height) <= 2.1)
    panorama_candidate = bool(xmp_equirectangular or wide_equirectangular or marker_match)
    if xmp_equirectangular:
        panorama_reason = "xmp_equirectangular"
    elif wide_equirectangular:
        panorama_reason = "wide_2_to_1"
    elif marker_match:
        panorama_reason = "marker_only"
    else:
        panorama_reason = ""
    return {
        "panorama_candidate": panorama_candidate,
        "panorama_reason": panorama_reason,
        "width": width,
        "height": height,
        "aspect_ratio": aspect_ratio,
        "floorplan_candidate": floorplan_candidate,
        "floorplan_reason": floorplan_reason,
    }


def extract_media(
    advert: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    images = deep_get(advert, "advertImageList", "advertImage")
    photos: list[dict[str, object]] = []
    floorplans: list[dict[str, object]] = []
    all_assets: list[dict[str, object]] = []
    panoramas: list[dict[str, object]] = []
    if isinstance(images, list):
        for index, entry in enumerate(images):
            if not isinstance(entry, dict):
                continue
            url = best_image_url(entry)
            if not url:
                continue
            description = as_text(entry.get("description"))
            panorama_signal = inspect_panorama_signal(url, description)
            role = "floorplan" if (looks_like_floorplan(description, url) or bool(panorama_signal.get("floorplan_candidate"))) else "photo"
            asset = {
                "index": index,
                "url": url,
                "description": description,
                "role": role,
                **panorama_signal,
            }
            all_assets.append(asset)
            if asset["role"] == "floorplan":
                floorplans.append(asset)
            else:
                photos.append(asset)
                if bool(asset.get("panorama_candidate")):
                    panoramas.append(asset)
    attachments = deep_get(advert, "advertAttachmentList", "advertAttachment")
    if isinstance(attachments, list):
        for entry in attachments:
            if not isinstance(entry, dict):
                continue
            url = as_text(entry.get("url") or entry.get("attachmentUrl") or entry.get("downloadUrl"))
            if not url:
                continue
            description = as_text(entry.get("description") or entry.get("name") or entry.get("title"))
            if looks_like_floorplan(description, url):
                asset = {"index": len(all_assets), "url": url, "description": description or "Attachment floorplan", "role": "floorplan"}
                floorplans.append(asset)
                all_assets.append(asset)
    return photos, floorplans, all_assets, panoramas


def extract_html_floorplan_refs(listing_html: str) -> list[dict[str, object]]:
    refs: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, match in enumerate(HTML_FLOORPLAN_IMAGE_RE.finditer(str(listing_html or ""))):
        url = html.unescape(match.group(0).strip())
        if not url or url in seen:
            continue
        seen.add(url)
        refs.append(
            {
                "index": index,
                "url": url,
                "description": "HTML floorplan",
                "role": "floorplan",
            }
        )
    return refs


def teaser_values(advert: dict[str, object]) -> tuple[float | None, float | None, list[str]]:
    teaser = advert.get("teaserAttributes")
    rooms = None
    area = None
    labels: list[str] = []
    if isinstance(teaser, list):
        for entry in teaser:
            if not isinstance(entry, dict):
                continue
            value = as_text(entry.get("value"))
            postfix = as_text(entry.get("postfix"))
            joined = " ".join(part for part in (value, postfix) if part).strip()
            if joined:
                labels.append(joined)
            lowered = postfix.lower()
            if "m²" in postfix or "m2" in lowered:
                area = numeric_from_text(value)
            if "zimmer" in lowered:
                rooms = numeric_from_text(value)
    return rooms, area, labels


def decision_context_summary(facts: dict[str, object]) -> dict[str, str]:
    attributes = facts.get("attribute_map")
    attribute_map = attributes if isinstance(attributes, dict) else {}
    rooms_label = str(facts.get("rooms_label") or "").strip() or "the room count"
    area_label = str(facts.get("area_label") or "").strip() or "the overall size"
    total_rent = facts.get("total_rent_eur")
    total_rent_text = ""
    if isinstance(total_rent, (int, float)) and total_rent > 0:
        total_rent_text = f"EUR {int(total_rent):,}".replace(",", ".")
    availability = str(facts.get("availability") or "").strip() or "not stated"
    postal_name = str(facts.get("postal_name") or "").strip()
    address_lines = facts.get("address_lines")
    address_line = ""
    if isinstance(address_lines, list) and address_lines:
        address_line = str(address_lines[0] or "").strip()
    location = ", ".join(part for part in (address_line, postal_name) if part) or "the micro-location"
    equipment = (
        pick_first_attribute(
            attribute_map,
            "GENERAL_TEXT_ADVERT/Ausstattung",
            "GENERAL_TEXT_ADVERT/Zusatzinformationen",
        )
        or str(facts.get("headline_hook") or "").strip()
        or "the listed equipment"
    )
    heating = pick_first_attribute(
        attribute_map,
        "HEATING",
        "HEATING_TYPE",
        "HEIZUNG",
        "HEIZUNGSART",
        "GENERAL_TEXT_ADVERT/Heizung",
        "GENERAL_TEXT_ADVERT/Heizungsart",
        "GENERAL_TEXT_ADVERT/Heating",
    ) or "the heating setup and running costs"
    return {
        "rooms_label": rooms_label,
        "area_label": area_label,
        "rent_text": total_rent_text or "the total monthly burden",
        "availability": availability,
        "location": location,
        "equipment": equipment,
        "heating": heating,
    }


def decision_signals(facts: dict[str, object]) -> dict[str, object]:
    context = decision_context_summary(facts)
    pros: list[str] = []
    cons: list[str] = []
    unknowns: list[str] = []
    score = 0
    rooms = facts.get("rooms")
    area_sqm = facts.get("area_sqm")
    floorplan_count = facts.get("floorplan_count")
    panorama_mode = str(facts.get("tour_media_mode") or "").strip()
    heating = context["heating"].lower()
    total_rent = facts.get("total_rent_eur")
    availability = context["availability"].lower()
    livability = facts.get("livability_snapshot")
    livability_snapshot = livability if isinstance(livability, dict) else {}
    attributes = facts.get("attribute_map")
    attribute_map = attributes if isinstance(attributes, dict) else {}
    preferences = [value.lower() for value in list(attribute_map.get("ESTATE_PREFERENCE") or []) if isinstance(value, str)]

    if isinstance(floorplan_count, int) and floorplan_count > 0:
        pros.append("A floor plan is available, so the layout can be judged before committing to a viewing.")
        score += 1
    else:
        cons.append("No floor plan is available, so circulation, storage, and furniture fit are harder to verify remotely.")
        score -= 1

    if panorama_mode == "panorama_360":
        pros.append("A live 360 source is available, which makes it easier to verify light, room connections, and spatial feel.")
        score += 2
    else:
        cons.append("The listing only exposes flat images, so light, room flow, and real proportions are harder to trust.")
        score -= 2

    if isinstance(rooms, (int, float)) and isinstance(area_sqm, (int, float)) and rooms > 0 and area_sqm > 0:
        sqm_per_room = area_sqm / rooms
        if sqm_per_room >= 22:
            pros.append(f"{context['rooms_label']} across {context['area_label']} suggests generous room sizing for daily living.")
            score += 1
        elif sqm_per_room <= 16:
            cons.append(f"{context['rooms_label']} within {context['area_label']} may mean tighter room sizing than the headline suggests.")
            score -= 1

    if "gas" in heating:
        cons.append(f"{context['heating']} can mean higher running-cost and future-upgrade risk than newer heating systems.")
        score -= 1
    elif "fern" in heating or "district" in heating:
        pros.append(f"{context['heating']} may be operationally simpler than an older in-unit heating setup.")
        score += 1
    else:
        unknowns.append(f"Check the real running costs and efficiency of {context['heating']} before signing.")

    if isinstance(total_rent, (int, float)) and total_rent > 0:
        pros.append(f"The listing states a total monthly burden of {context['rent_text']}, which makes shortlist comparison easier.")
    else:
        cons.append("The total monthly burden is not stated clearly, so side costs and affordability need manual verification.")
        score -= 1

    if availability and availability != "not stated":
        pros.append(f"Availability is listed as {context['availability']}, which helps with timing decisions.")
    else:
        unknowns.append("Confirm move-in timing, notice period, and handover date.")

    nearest_pharmacy_m = livability_snapshot.get("nearest_pharmacy_m")
    nearest_supermarket_m = livability_snapshot.get("nearest_supermarket_m")
    nearest_bakery_m = livability_snapshot.get("nearest_bakery_m")
    nearest_bicycle_parking_m = livability_snapshot.get("nearest_bicycle_parking_m")
    nearest_cycleway_m = livability_snapshot.get("nearest_cycleway_m")
    nearest_transit_m = livability_snapshot.get("nearest_transit_m")
    nearest_tram_bus_m = livability_snapshot.get("nearest_tram_bus_m")
    nearest_subway_m = livability_snapshot.get("nearest_subway_m")
    nearest_running_m = livability_snapshot.get("nearest_running_m")
    nearest_playground_m = livability_snapshot.get("nearest_playground_m")
    nearest_school_m = livability_snapshot.get("nearest_school_m")

    if isinstance(nearest_supermarket_m, int):
        if nearest_supermarket_m <= 600:
            pros.append(f"Daily errands look practical, with a supermarket roughly {nearest_supermarket_m} m away.")
            score += 1
        elif nearest_supermarket_m > 1200:
            cons.append(f"The nearest supermarket is about {nearest_supermarket_m} m away, which may make everyday errands less convenient.")
            score -= 1
    else:
        unknowns.append("Verify supermarket access and basic errand convenience around the address.")

    if isinstance(nearest_pharmacy_m, int):
        if nearest_pharmacy_m <= 700:
            pros.append(f"Pharmacy access looks practical at roughly {nearest_pharmacy_m} m.")
        elif nearest_pharmacy_m > 1500:
            unknowns.append(f"The nearest mapped pharmacy is about {nearest_pharmacy_m} m away; verify whether that feels acceptable.")
    else:
        unknowns.append("Verify pharmacy and basic service access in the immediate area.")

    if isinstance(nearest_tram_bus_m, int):
        if nearest_tram_bus_m <= 500:
            pros.append(f"Tram or bus access appears close at roughly {nearest_tram_bus_m} m, which supports everyday mobility.")
            score += 1
        elif nearest_tram_bus_m > 1000:
            cons.append(f"Tram or bus access looks farther away at about {nearest_tram_bus_m} m.")
            score -= 1
    if isinstance(nearest_subway_m, int):
        if nearest_subway_m <= 650:
            pros.append(f"Underground access appears practical at roughly {nearest_subway_m} m.")
            score += 1
        elif nearest_subway_m > 1200:
            cons.append(f"The nearest underground access looks farther away at about {nearest_subway_m} m.")
            score -= 1
    elif isinstance(nearest_transit_m, int):
        if nearest_transit_m <= 500:
            pros.append(f"Public transit appears close at roughly {nearest_transit_m} m, which supports commute flexibility.")
            score += 1
        elif nearest_transit_m > 1000:
            cons.append(f"Public transit looks farther away at about {nearest_transit_m} m.")
            score -= 1
    else:
        unknowns.append("Verify tram, bus, and U-Bahn access separately instead of trusting the district headline alone.")

    if isinstance(nearest_running_m, int):
        if nearest_running_m <= 900:
            pros.append(f"There appears to be nearby green or run-friendly space within about {nearest_running_m} m.")
            score += 1
        elif nearest_running_m > 1600:
            cons.append(f"Run-friendly green space looks farther away at about {nearest_running_m} m.")
            score -= 1
    else:
        unknowns.append("Check whether there is genuinely pleasant green space nearby for walks or runs.")

    if isinstance(nearest_bakery_m, int) and nearest_bakery_m <= 500:
        pros.append(f"A bakery is roughly {nearest_bakery_m} m away, which helps with day-to-day neighborhood convenience.")

    if isinstance(nearest_bicycle_parking_m, int):
        if nearest_bicycle_parking_m <= 250:
            pros.append(f"Bicycle parking appears very close at roughly {nearest_bicycle_parking_m} m.")
            score += 1
        elif nearest_bicycle_parking_m > 1200:
            unknowns.append(f"The nearest mapped bicycle parking is about {nearest_bicycle_parking_m} m away; verify whether bike storage is practical.")
    else:
        unknowns.append("Check whether bike parking or secure bike storage is realistically available.")

    if isinstance(nearest_cycleway_m, int):
        if nearest_cycleway_m <= 400:
            pros.append(f"Cycleway access looks close at roughly {nearest_cycleway_m} m, which strengthens bike commuting or errands.")
            score += 1
        elif nearest_cycleway_m > 1200:
            cons.append(f"Dedicated cycleway access looks farther away at about {nearest_cycleway_m} m.")
            score -= 1
    else:
        unknowns.append("Verify whether the surrounding streets actually feel safe and practical for cycling.")

    if isinstance(nearest_playground_m, int):
        if nearest_playground_m <= 900:
            pros.append(f"A playground is mapped within about {nearest_playground_m} m, which can matter for family fit.")
        else:
            unknowns.append(f"The nearest mapped playground is about {nearest_playground_m} m away; verify whether family infrastructure is good enough.")

    if isinstance(nearest_school_m, int) and nearest_school_m <= 1200:
        pros.append(f"A school is mapped within about {nearest_school_m} m, which strengthens family practicality.")

    if "fahrstuhl" in preferences or "lift" in preferences:
        pros.append("Lift access is listed, which helps with groceries, strollers, guests, and long-term usability.")
        score += 1
    if "garage" in preferences:
        pros.append("A garage option is listed, which helps if parking friction matters.")
    if "keller" in preferences:
        pros.append("Cellar storage is listed, which improves everyday practicality.")
    if "einbauküche" in preferences or "einbaukuche" in preferences:
        pros.append("An installed kitchen is listed, which reduces move-in friction and upfront setup cost.")

    unknowns.extend(
        [
            "Confirm noise, privacy, and natural light in person because listing media rarely shows those tradeoffs honestly.",
            "Check storage, stroller or bike practicality, and cellar or lift details if those matter to the household.",
        ]
    )
    if context["location"] != "the micro-location":
        unknowns.append(f"Walk {context['location']} in person to verify street feel, transit, and day-to-day errands.")

    if score >= 2:
        recommendation = "shortlist"
    elif score <= -2:
        recommendation = "reject"
    else:
        recommendation = "view_if_compelling"

    return {
        "good_fit_reasons": pros,
        "bad_fit_reasons": cons,
        "unknowns": unknowns,
        "recommendation": recommendation,
        "location_fit_score": score,
        "livability_snapshot": livability_snapshot,
    }


def build_variants(*, title: str, floorplan_count: int, photo_count: int, facts: dict[str, object]) -> list[dict[str, object]]:
    headline = str(facts.get("headline_hook") or title).strip()
    context = decision_context_summary(facts)
    availability = context["availability"]
    room_text = context["rooms_label"]
    area_text = context["area_label"]
    layout_first = Variant(
        variant_key="layout_first",
        scene_strategy="layout_first",
        theme_name="clean_light",
        tour_style="guided_layout_walkthrough",
        audience="tenant_screening",
        creative_brief=(
            f"Reason like a household decision memo, not a brochure. Open on the floor plan, then walk through the spaces that matter most for daily life in {headline}. "
            f"Explain why this could be a good fit, why it could be a bad fit, what is still unknown, and whether it deserves a shortlist recommendation. "
            f"Take into account {room_text}, {area_text}, {context['heating']}, {context['rent_text']}, how {context['location']} supports or weakens the case, "
            f"and practical neighborhood questions like transit, errands, pharmacy access, playgrounds, and whether there is good space nearby for walks or runs."
        ),
        call_to_action="Decide whether to shortlist, book a viewing, or reject this listing.",
        scene_selection_json={"include_floorplans": floorplan_count > 0, "floorplan_position": "start", "max_photos": min(max(photo_count, 1), 10)},
        tour_settings_json={"showSceneNumbers": True, "defaultPanel": "share", "tone": "practical"},
    )
    lifestyle = Variant(
        variant_key="light_and_view",
        scene_strategy="story_first",
        theme_name="warm_editorial",
        tour_style="atmospheric_highlights",
        audience="urban_renter",
        creative_brief=(
            f"Translate the home into day-to-day use instead of generic lifestyle copy. Highlight light, privacy, noise exposure, outdoor value, and the rooms a renter would actually use every day. "
            f"Use {room_text}, {area_text}, and {context['equipment']} to explain whether the atmosphere supports real living rather than just pretty photos. "
            f"Include neighborhood practicality: groceries, pharmacy, transit, playgrounds, and nearby running or green-space options."
        ),
        call_to_action="Use this version to judge whether the home feels worth an in-person visit.",
        scene_selection_json={"include_floorplans": floorplan_count > 0, "floorplan_position": "end", "max_photos": min(max(photo_count, 1), 8)},
        tour_settings_json={"showSceneNumbers": False, "defaultPanel": "theme", "tone": "editorial"},
    )
    shortlist = Variant(
        variant_key="shortlist_comparison",
        scene_strategy="compact",
        theme_name="minimal_analyst",
        tour_style="comparison_ready",
        audience="shortlist_reviewer",
        creative_brief=(
            f"Create a compare-ready decision artifact with compact scenes and direct reasoning. State the strongest reasons to rent or buy, the main concerns, and the unknowns that still need checking before a viewing or offer. "
            f"Include total cost cues around {context['rent_text']}, availability {availability}, and whether the layout, heating, location, transit, errands, and outdoor/family practicality materially beat comparable options."
        ),
        call_to_action="Compare the tradeoffs, then shortlist, view, or reject.",
        scene_selection_json={"include_floorplans": floorplan_count > 0, "floorplan_position": "alternate" if floorplan_count > 0 else "omit", "max_photos": min(max(photo_count, 1), 6)},
        tour_settings_json={"showSceneNumbers": True, "defaultPanel": "ctas", "tone": "analyst"},
    )
    return [variant.__dict__ for variant in (layout_first, lifestyle, shortlist)]


def summarize_listing(url: str) -> dict[str, object]:
    listing_html = fetch_html(url)
    next_data = extract_next_data(listing_html)
    advert = extract_advert_details(next_data)
    attributes = extract_attributes(advert)
    photos, floorplans, assets, panoramas = extract_media(advert)
    html_floorplans = extract_html_floorplan_refs(listing_html)
    if html_floorplans:
        existing_floorplans = {str(item.get("url") or "").strip() for item in floorplans}
        for asset in html_floorplans:
            url = str(asset.get("url") or "").strip()
            if not url or url in existing_floorplans:
                continue
            floorplans.append(asset)
            assets.append(asset)
            existing_floorplans.add(url)
    source_virtual_tour_url, panorama_source = extract_virtual_tour(attributes)
    rooms, area, teaser_labels = teaser_values(advert)
    seo = deep_get(advert, "seoMetaData") or {}
    address = deep_get(advert, "advertAddressDetails") or {}
    organisation = deep_get(advert, "organisationDetails") or {}
    canonical_url = as_text((seo or {}).get("canonicalUrl")) or url
    description = as_text(advert.get("description"))
    title = description or as_text((seo or {}).get("title")) or canonical_url
    description = as_text(advert.get("description"))
    total_rent = numeric_from_text(pick_first_attribute(attributes, "RENTAL_PRICE/TOTAL_ENCUMBRANCE", "PRICE", "EUROPRICE"))
    area_label = pick_first_attribute(attributes, "ESTATE_SIZE/LIVING_AREA", "ESTATE_SIZE/USEABLE_AREA")
    if area is None:
        area = numeric_from_text(area_label)
    rooms_label = pick_first_attribute(attributes, "NUMBER_OF_ROOMS", "ESTATE_SIZE/NUMBER_OF_ROOMS")
    if rooms is None:
        rooms = numeric_from_text(rooms_label)
    availability = pick_first_attribute(
        attributes,
        "AVAILABLE_NOW",
        "AVAILABLE_DATE",
        "GENERAL_TEXT_ADVERT/Available from",
        "GENERAL_TEXT_ADVERT/verfuegbar ab",
        "DURATION/TERMLIMITTEXT",
    )
    headline_hook = (
        pick_first_attribute(attributes, "GENERAL_TEXT_ADVERT/Ausstattung")
        or pick_first_attribute(attributes, "GENERAL_TEXT_ADVERT/Zusatzinformationen")
        or description
        or title
    )
    address_lines = normalize_attribute_value((address or {}).get("addressLines"))
    if not address_lines and isinstance((address or {}).get("addressLine"), list):
        address_lines = normalize_attribute_value((address or {}).get("addressLine"))
    direct_coordinates = parse_coordinate_pair(pick_first_attribute(attributes, "COORDINATES"))
    geocoded_location = (
        {
            "query": "listing_coordinates",
            "lat": direct_coordinates[0],
            "lon": direct_coordinates[1],
            "display_name": ", ".join(address_lines) if address_lines else "",
        }
        if direct_coordinates is not None
        else geocode_listing_location(
            address_lines=address_lines,
            postal_code=as_text((address or {}).get("postCode")),
            postal_name=as_text((address or {}).get("postalName")),
            country=as_text((address or {}).get("country")),
        )
    )
    livability_snapshot = {}
    if geocoded_location:
        try:
            livability_snapshot = nearby_livability_snapshot(
                float(geocoded_location["lat"]),
                float(geocoded_location["lon"]),
            )
        except Exception:
            livability_snapshot = {}
    panorama_ready = bool(panoramas or source_virtual_tour_url)
    facts = {
        "title": title,
        "canonical_url": canonical_url,
        "headline_hook": headline_hook,
        "description": description,
        "rooms": rooms,
        "rooms_label": rooms_label or (f"{rooms:g} Zimmer" if rooms is not None else ""),
        "area_sqm": area,
        "area_label": area_label or (f"{area:g} m²" if area is not None else ""),
        "total_rent_eur": total_rent,
        "availability": availability,
        "teaser_attributes": teaser_labels,
        "address_lines": address_lines,
        "postal_code": as_text((address or {}).get("postCode")),
        "postal_name": as_text((address or {}).get("postalName")),
        "country": as_text((address or {}).get("country")),
        "organisation_name": as_text((organisation or {}).get("orgName")),
        "organisation_phone": as_text((organisation or {}).get("orgPhone")),
        "organisation_email": as_text((organisation or {}).get("orgEmail")),
        "attribute_map": attributes,
        "photo_count": len(photos),
        "floorplan_count": len(floorplans),
        "panorama_candidate_count": len(panoramas),
        "tour_media_mode": "panorama_360" if panorama_ready else "flat_images",
        "source_virtual_tour_url": source_virtual_tour_url,
        "panorama_source": panorama_source,
        "geocoded_location": geocoded_location,
        "livability_snapshot": livability_snapshot,
    }
    facts["decision_summary"] = decision_signals(facts)
    return {
        "source": "willhaben",
        "property_url": canonical_url,
        "listing_id": as_text(advert.get("id")),
        "listing_uuid": as_text(advert.get("uuid")),
        "title": title,
        "description": description,
        "address_lines": address_lines,
        "property_facts_json": facts,
        "media_urls_json": [entry["url"] for entry in photos],
        "panorama_media_urls_json": [entry["url"] for entry in panoramas],
        "floorplan_urls_json": [entry["url"] for entry in floorplans],
        "media_assets_json": assets,
        "source_virtual_tour_url": source_virtual_tour_url,
        "panorama_source": panorama_source,
        "tour_variants_json": build_variants(title=title, floorplan_count=len(floorplans), photo_count=len(photos), facts=facts),
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch Willhaben property packets for the Crezlo tour workflow.")
    parser.add_argument("urls", nargs="*", help="Willhaben property URLs.")
    parser.add_argument("--url-file", help="Optional newline-delimited URL file.")
    parser.add_argument("--output", help="Optional path for JSON output.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON.")
    return parser.parse_args(argv)


def load_urls(args: argparse.Namespace) -> list[str]:
    urls = [str(url or "").strip() for url in args.urls if str(url or "").strip()]
    if args.url_file:
        with open(args.url_file, "r", encoding="utf-8") as handle:
            for raw in handle:
                url = raw.strip()
                if url:
                    urls.append(url)
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = urllib.parse.urldefrag(url)[0]
        if normalized not in seen:
            deduped.append(normalized)
            seen.add(normalized)
    if not deduped:
        raise SystemExit("willhaben_urls_required")
    return deduped


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        payload = [summarize_listing(url) for url in load_urls(args)]
    except RuntimeError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    text = json.dumps(payload, ensure_ascii=True, indent=2 if args.pretty else None)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
    else:
        sys.stdout.write(text)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
