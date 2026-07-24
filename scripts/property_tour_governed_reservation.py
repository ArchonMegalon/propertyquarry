from __future__ import annotations


GOVERNED_PRATER_SLUG = (
    "prater-messe-maisonette-ai-360-053ad185e1c44b2e"
)


def require_dynamic_tour_slug(slug: object) -> str:
    normalized = str(slug or "").strip()
    if normalized == GOVERNED_PRATER_SLUG:
        raise RuntimeError("governed_tour_slug_reserved")
    return normalized
