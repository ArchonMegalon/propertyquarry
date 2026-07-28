from __future__ import annotations

import contextlib

import pytest

from app.product import property_search_storage


def _run_record(*, compact_only: bool = False) -> dict[str, object]:
    record: dict[str, object] = {
        "run_id": "retention-run",
        "principal_id": "retention-principal",
        "status": "completed",
        "summary": {
            "status": "completed",
            "ranked_total": 1,
            "ranked_candidates": [
                {"candidate_ref": "saved-result", "title": "Saved result"}
            ],
            "sources": [
                {
                    "source_label": "Willhaben",
                    "source_html": "<html>discard me</html>",
                }
            ],
        },
    }
    if compact_only:
        record["payload_retention_status"] = "compact_only"
    return record


def _legacy_compact_row(*, compact_only: bool) -> tuple[object, ...]:
    compact = property_search_storage._compact_property_search_run_record(  # type: ignore[attr-defined]
        _run_record()
    )
    return (
        compact,
        "2026-07-18T10:00:00+00:00",
        "2026-07-18T11:00:00+00:00",
        None,
        property_search_storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,  # type: ignore[attr-defined]
        False,
        "compact_only" if compact_only else None,
    )


def _install_fake_rows(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[tuple[object, ...]],
) -> list[str]:
    queries: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=()):
            queries.append(str(query))

        def fetchone(self):
            return rows[0] if rows else None

        def fetchall(self):
            return list(rows)

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(
        property_search_storage,
        "_property_search_run_database_url",
        lambda: "postgresql://test",
    )
    monkeypatch.setattr(
        property_search_storage,
        "_require_property_search_run_schema",
        lambda: None,
    )
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_run_connect",
        lambda: _Connection(),
    )
    return queries


def test_compact_only_canonicalization_keeps_saved_results_without_sources() -> None:
    loaded = property_search_storage._property_search_run_canonicalize_record(  # type: ignore[attr-defined]
        _run_record(compact_only=True)
    )

    summary = dict(loaded["summary"])
    assert summary["ranked_candidates"] == [
        {"candidate_ref": "saved-result", "title": "Saved result"}
    ]
    assert "sources" not in summary


def test_non_compacted_canonicalization_preserves_sources() -> None:
    loaded = property_search_storage._property_search_run_canonicalize_record(  # type: ignore[attr-defined]
        _run_record()
    )

    summary = dict(loaded["summary"])
    assert summary["sources"] == [
        {
            "source_label": "Willhaben",
            "source_html": "<html>discard me</html>",
        }
    ]


def test_compact_pruned_record_removes_sources_from_retained_payload() -> None:
    compact = property_search_storage._compact_pruned_property_search_run_record(  # type: ignore[attr-defined]
        _run_record(),
        pruned_at="2026-07-18T12:00:00+00:00",
    )

    summary = dict(compact["summary"])
    assert compact["payload_retention_status"] == "compact_only"
    assert compact["payload_pruned_at"] == "2026-07-18T12:00:00+00:00"
    assert summary["ranked_candidates"] == [
        {"candidate_ref": "saved-result", "title": "Saved result"}
    ]
    assert "sources" not in summary


def test_legacy_compact_load_uses_payload_retention_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _install_fake_rows(monkeypatch, [_legacy_compact_row(compact_only=True)])

    loaded = property_search_storage._load_property_search_run_compact_record(  # type: ignore[attr-defined]
        run_id="retention-run",
        principal_id="retention-principal",
    )

    assert loaded is not None
    assert loaded["payload_retention_status"] == "compact_only"
    assert "sources" not in dict(loaded["summary"])
    assert "payload_json->>'payload_retention_status'" in queries[-1]


def test_legacy_lightweight_rows_strip_only_retained_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = _install_fake_rows(
        monkeypatch,
        [
            _legacy_compact_row(compact_only=True),
            _legacy_compact_row(compact_only=False),
        ],
    )

    loaded = property_search_storage._list_property_search_run_records(  # type: ignore[attr-defined]
        limit=2,
        principal_id="retention-principal",
        lightweight=True,
    )

    assert len(loaded) == 2
    assert "sources" not in dict(loaded[0]["summary"])
    assert "sources" in dict(loaded[1]["summary"])
    assert "payload_json->>'payload_retention_status'" in queries[-1]


def test_postgres_prune_strips_sources_from_retained_compaction_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries: list[str] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, _params=()):
            queries.append(str(query))

        def fetchall(self):
            return []

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return _Cursor()

        def transaction(self):
            return contextlib.nullcontext()

    monkeypatch.setattr(
        property_search_storage,
        "_property_search_run_database_url",
        lambda: "postgresql://test",
    )
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_run_retention_seconds",
        lambda: 60,
    )
    monkeypatch.setattr(
        property_search_storage,
        "_require_property_search_run_schema",
        lambda: None,
    )
    monkeypatch.setattr(
        property_search_storage,
        "_set_property_search_writer_contract",
        lambda _cursor: None,
    )
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_run_connect",
        lambda: _Connection(),
    )

    property_search_storage._prune_property_search_run_records()  # type: ignore[attr-defined]

    update_query = queries[-1]
    assert "SET compact_json = stale_runs.compacted" in update_query
    assert "#- '{summary,sources}') AS compacted" in update_query
    assert "payload_json = stale_runs.compacted ||" in update_query
