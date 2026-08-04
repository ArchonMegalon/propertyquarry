from __future__ import annotations

from app.product import service as property_service
from app.product.service import ProductService


def test_exact_scope_preview_pool_is_not_serialized_by_provider_plan_cap(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PROPERTYQUARRY_SEARCH_PROVIDER_WORKER_CONCURRENCY", "8")
    monkeypatch.setattr(property_service, "property_worker_cap", lambda plan_key: 1)

    assert property_service._property_search_preview_worker_concurrency_for_plan(
        "free",
        exact_scope=False,
    ) == 1
    assert property_service._property_search_preview_worker_concurrency_for_plan(
        "free",
        exact_scope=True,
    ) == 3


def test_exact_scope_preview_prefetch_refreshes_full_details_in_parallel_lane(
    monkeypatch,
) -> None:
    service = object.__new__(ProductService)
    preview_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        ProductService,
        "_property_public_preview_cache_lookup",
        lambda self, *, cache_index, property_url: {
            "title": "Fast cached title",
        },
    )
    monkeypatch.setattr(
        ProductService,
        "_property_public_preview_cache_store",
        lambda self, *, cache_index, property_url, preview: dict(preview or {}),
    )

    def _preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        preview_calls.append((property_url, prefer_fast))
        return {
            "title": "Full detail title",
            "title_full": "Full detail title · exact location",
        }

    monkeypatch.setattr(
        property_service,
        "_property_scout_page_preview_with_timeout",
        _preview,
    )

    result = service._prefetch_property_public_previews_for_sources(
        source_jobs=[
            {
                "platform": "willhaben",
                "url": "https://example.test/search",
                "__source_url__": "https://example.test/search",
                "__listing_urls__": ["https://example.test/listing/1"],
                "__prefer_fast__": False,
            }
        ],
        cache_index={},
        worker_cap=4,
    )

    assert preview_calls == [("https://example.test/listing/1", False)]
    source_result = result["source_results"][(
        "willhaben",
        "https://example.test/search",
    )]
    assert source_result["previews"]["https://example.test/listing/1"][
        "title_full"
    ] == "Full detail title · exact location"


def test_source_research_snapshot_uses_bounded_preview_lane(monkeypatch) -> None:
    preview_calls: list[tuple[str, bool]] = []

    def _preview(property_url: str, *, prefer_fast: bool = False) -> dict[str, object]:
        preview_calls.append((property_url, prefer_fast))
        return {"title": "Bounded preview", "property_facts_json": {}}

    monkeypatch.setattr(
        property_service,
        "_property_scout_page_preview_with_timeout",
        _preview,
    )
    monkeypatch.setattr(
        property_service,
        "_property_scout_fetch_html_compat",
        lambda *args, **kwargs: "",
    )
    monkeypatch.setattr(
        property_service,
        "_property_apply_location_hint_research",
        lambda *, facts, title="", summary="": dict(facts),
    )
    monkeypatch.setattr(
        property_service,
        "_property_enrich_official_risk_evidence",
        lambda facts: dict(facts or {}),
    )
    property_service._property_source_research_snapshot.cache_clear()

    property_service._property_source_research_snapshot(
        "https://example.test/listing/2"
    )

    assert preview_calls == [("https://example.test/listing/2", True)]
    property_service._property_source_research_snapshot.cache_clear()
