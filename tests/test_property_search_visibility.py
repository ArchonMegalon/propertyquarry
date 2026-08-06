from __future__ import annotations

from app.product.property_surface_state import normalize_property_search_run_snapshot
from app.services.property_search_visibility import (
    filter_property_search_run_visibility,
    property_search_candidate_is_suppressed,
    property_search_suppression_index,
)


def _karl_candidate(*, candidate_ref: str = "ad48357be22535c1") -> dict[str, object]:
    return {
        "candidate_ref": candidate_ref,
        "source_ref": candidate_ref,
        "listing_id": "1536069684",
        "property_url": "https://www.willhaben.at/example-1536069684/",
        "title": "Karl-Czerny-Gasse 2",
        "rank": 1,
    }


def _visible_candidate() -> dict[str, object]:
    return {
        "candidate_ref": "visible-home",
        "listing_id": "9999999999",
        "property_url": "https://listing.example/visible-9999999999/",
        "title": "Visible home",
        "rank": 2,
    }


def test_karl_candidate_and_all_curated_aliases_are_suppressed() -> None:
    suppression_index = property_search_suppression_index()

    assert suppression_index["listing_ids"] == frozenset({"1536069684"})
    for candidate_ref in (
        "16290a106d16a31c",
        "ad48357be22535c1",
        "cece2dad814fdf68",
        "karl-czerny-gasse-2-private-showcase",
    ):
        assert property_search_candidate_is_suppressed(
            _karl_candidate(candidate_ref=candidate_ref)
        )


def test_visibility_filter_removes_karl_from_every_result_projection() -> None:
    karl = _karl_candidate()
    visible = _visible_candidate()
    raw_run = {
        "run_id": "shared-tibor-elisabeth-run",
        "status": "completed",
        "summary": {
            "ranked_candidates": [karl, visible],
            "_delivery_candidates": [karl, visible],
            "sources": [
                {
                    "source_label": "Provider",
                    "top_candidates": [karl, visible],
                    "research_candidates": [karl, visible],
                    "listing_total": 2,
                    "ranked_total": 2,
                }
            ],
            "listing_total": 2,
            "ranked_total": 2,
            "ranked_candidate_total": 2,
            "results_total": 2,
            "survivor_total": 2,
            "raw_listing_total": 2,
        },
    }

    filtered = filter_property_search_run_visibility(raw_run)
    summary = filtered["summary"]

    assert [row["candidate_ref"] for row in summary["ranked_candidates"]] == [
        "visible-home"
    ]
    assert [row["candidate_ref"] for row in summary["_delivery_candidates"]] == [
        "visible-home"
    ]
    assert [
        row["candidate_ref"] for row in summary["sources"][0]["top_candidates"]
    ] == ["visible-home"]
    assert summary["listing_total"] == 1
    assert summary["ranked_total"] == 1
    assert summary["raw_listing_total"] == 2
    assert summary["owner_suppressed_total"] == 1


def test_normalized_search_snapshot_cannot_restore_suppressed_candidate() -> None:
    normalized = normalize_property_search_run_snapshot(
        {
            "run_id": "shared-run",
            "status": "completed",
            "summary": {
                "ranked_candidates": [
                    _karl_candidate(
                        candidate_ref="karl-czerny-gasse-2-private-showcase"
                    ),
                    _visible_candidate(),
                ],
                "_delivery_candidates": [
                    _karl_candidate(candidate_ref="16290a106d16a31c"),
                    _visible_candidate(),
                ],
                "ranked_total": 2,
                "listing_total": 2,
            },
        }
    )

    assert [
        row["candidate_ref"]
        for row in normalized["summary"]["ranked_candidates"]
    ] == ["visible-home"]
    assert [
        row["candidate_ref"]
        for row in normalized["summary"]["_delivery_candidates"]
    ] == ["visible-home"]
    assert normalized["summary"]["ranked_total"] == 1
