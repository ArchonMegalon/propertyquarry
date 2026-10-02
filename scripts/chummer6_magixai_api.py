from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


MAGIXAI_OFFICIAL_API_BASE = "https://www.aimagicx.com/api/v1"
MAGIXAI_BETA_API_BASE = "https://beta.aimagicx.com/api/v1"
MAGIXAI_CHAT_ENDPOINTS = ("/chat/completions", "/chat")
MAGIXAI_IMAGE_ENDPOINT = "/images/generations"
MAGIXAI_QUOTA_ENDPOINT = "/quota"
MAGIXAI_MODELS_ENDPOINT = "/models"

MAGIXAI_DEFAULT_IMAGE_MODELS = (
    "fal-ai/flux-2-pro",
    "fal-ai/flux-pro/v1.1-ultra",
    "fal-ai/hidream-i1-dev",
    "fal-ai/ideogram/v2",
    "fal-ai/gpt-image-1.5",
    "fal-ai/flux-2",
    "fal-ai/flux/dev",
    "fal-ai/hidream-i1-fast",
)

MAGIXAI_IMAGE_MODEL_ALIASES = {
    "flux": "fal-ai/flux/dev",
    "flux-dev": "fal-ai/flux/dev",
    "flux-2": "fal-ai/flux-2",
    "ideogram": "fal-ai/ideogram/v2",
}
MAGIXAI_IMAGE_MODELS_WITHOUT_QUALITY = frozenset(
    {
        "fal-ai/flux-2-pro",
    }
)

_KNOWN_ENDPOINT_SUFFIXES = (
    "/api/v1/chat/completions",
    "/api/v1/chat",
    "/api/v1/images/generations",
    "/api/v1/models",
    "/api/v1/quota",
    "/chat/completions",
    "/chat",
    "/images/generations",
    "/models",
    "/quota",
)

_HTML_MARKERS = ("<!doctype html", "<html", "__next", "/_next/static/")


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def normalize_magixai_image_model(value: str | None) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return MAGIXAI_IMAGE_MODEL_ALIASES.get(normalized.lower(), normalized)


def normalize_magixai_base_url(value: str | None) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    parts = urlsplit(raw)
    path = parts.path.rstrip("/")
    lowered_path = path.lower()
    for suffix in _KNOWN_ENDPOINT_SUFFIXES:
        if lowered_path.endswith(suffix):
            path = path[: -len(suffix)]
            lowered_path = path.lower()
            break
    if lowered_path in {"", "/"}:
        path = "/api/v1"
    elif lowered_path.endswith("/api"):
        path = path + "/v1"
    normalized = urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))
    return normalized.rstrip("/")


def magixai_api_base_urls(configured_base: str | None = None) -> list[str]:
    configured = normalize_magixai_base_url(configured_base)
    return _unique([MAGIXAI_OFFICIAL_API_BASE, configured, MAGIXAI_BETA_API_BASE])


def magixai_build_url(base_url: str, endpoint: str) -> str:
    clean_base = normalize_magixai_base_url(base_url) or MAGIXAI_OFFICIAL_API_BASE
    clean_endpoint = str(endpoint or "").strip()
    if not clean_endpoint:
        return clean_base
    return clean_base.rstrip("/") + "/" + clean_endpoint.lstrip("/")


def magixai_image_model_candidates(configured_model: str | None = None) -> list[str]:
    candidates: list[str] = []
    for candidate in (configured_model, *MAGIXAI_DEFAULT_IMAGE_MODELS):
        normalized = normalize_magixai_image_model(candidate)
        if not normalized:
            continue
        if normalized not in candidates:
            candidates.append(normalized)
    return candidates


def magixai_model_supports_quality(model: str | None) -> bool:
    normalized = normalize_magixai_image_model(model)
    if not normalized:
        return True
    return normalized not in MAGIXAI_IMAGE_MODELS_WITHOUT_QUALITY


def magixai_size_variants(width: int, height: int) -> list[str]:
    safe_width = max(1, int(width or 0))
    safe_height = max(1, int(height or 0))
    raw = f"{safe_width}x{safe_height}"
    ratio = safe_width / safe_height
    if 0.92 <= ratio <= 1.08:
        if max(safe_width, safe_height) >= 1024:
            return _unique(["square_hd", raw, "1024x1024", "square"])
        return _unique(["square", raw, "1024x1024", "square_hd"])
    if ratio > 1.0:
        primary = "landscape_4_3" if abs(ratio - (4.0 / 3.0)) <= abs(ratio - (16.0 / 9.0)) else "landscape_16_9"
        secondary = "landscape_16_9" if primary == "landscape_4_3" else "landscape_4_3"
        official = "1792x1024"
    else:
        portrait_ratio = safe_height / safe_width
        primary = "portrait_4_3" if abs(portrait_ratio - (4.0 / 3.0)) <= abs(portrait_ratio - (16.0 / 9.0)) else "portrait_16_9"
        secondary = "portrait_16_9" if primary == "portrait_4_3" else "portrait_4_3"
        official = "1024x1792"
    return _unique([primary, raw, official, secondary])


def magixai_looks_like_html(*, content_type: str | None, body: str | None) -> bool:
    normalized_content_type = str(content_type or "").lower()
    if "text/html" in normalized_content_type:
        return True
    lowered_body = str(body or "").strip().lower()
    return any(marker in lowered_body for marker in _HTML_MARKERS)
