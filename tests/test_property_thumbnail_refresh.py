from __future__ import annotations

from app.api.routes import landing


class _PreviewCacheProduct:
    @staticmethod
    def _property_public_preview_cache_lookup(
        *,
        cache_index: dict[str, dict[str, object]],
        property_url: str,
    ) -> dict[str, object]:
        return dict(cache_index.get(property_url) or {})

    @staticmethod
    def _property_public_preview_cache_store(
        *,
        cache_index: dict[str, dict[str, object]],
        property_url: str,
        preview: dict[str, object],
    ) -> None:
        cache_index[property_url] = dict(preview)


def test_failed_thumbnail_forces_one_detailed_preview_refresh(monkeypatch) -> None:
    preview_calls: list[tuple[str, bool]] = []

    def _detailed_preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        preview_calls.append((property_url, prefer_fast))
        return {
            "title": "Recovered listing",
            "media_urls_json": ["https://cache.example.test/recovered.jpg"],
        }

    monkeypatch.setattr(
        landing,
        "_property_scout_page_preview_with_timeout",
        _detailed_preview,
    )
    candidate = {
        "candidate_ref": "candidate-refresh",
        "property_url": "https://example.test/listing/1",
        "preview_image_url": "https://cache.example.test/stale.jpg",
    }

    unchanged = landing._propertyquarry_refresh_candidate_preview_if_needed(
        product=_PreviewCacheProduct(),
        candidate=candidate,
        allow_network=True,
    )

    assert unchanged == candidate
    assert preview_calls == []

    refreshed = landing._propertyquarry_refresh_candidate_preview_if_needed(
        product=_PreviewCacheProduct(),
        candidate=candidate,
        allow_network=True,
        force_network=True,
    )

    assert preview_calls == [("https://example.test/listing/1", False)]
    assert refreshed["media_urls_json"] == [
        "https://cache.example.test/recovered.jpg"
    ]


def test_failed_thumbnail_never_forces_network_when_network_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        landing,
        "_property_scout_page_preview_with_timeout",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("network preview must remain disabled")
        ),
    )

    result = landing._propertyquarry_refresh_candidate_preview_if_needed(
        product=_PreviewCacheProduct(),
        candidate={
            "property_url": "https://example.test/listing/2",
            "preview_image_url": "https://cache.example.test/stale.jpg",
        },
        allow_network=False,
        force_network=True,
    )

    assert result["preview_image_url"] == "https://cache.example.test/stale.jpg"
