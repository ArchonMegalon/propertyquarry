from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import os
import threading
import uuid

import pytest

from app.product import property_research_packet_links as packet_links_module
from app.product import property_search_storage as storage
from app.product import property_search_schema as search_schema
from app.product.property_research_packet_links import (
    PROPERTY_RESEARCH_PACKET_MAX_BYTES,
    PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
    PropertyResearchPacketConflictError,
    PropertyResearchPacketOversizeError,
    PropertyResearchPacketProjectionError,
    PropertyResearchPacketVersionError,
    _PROPERTY_RESEARCH_PACKET_LINK_LOOKUP_SQL,
    load_property_research_packet_link,
    project_property_research_packet_links,
    refresh_property_research_packet_links_for_refs,
    sync_property_research_packet_run_memberships,
    upsert_property_research_packet_links,
)


def _record(*candidates: dict[str, object]) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "principal_id": "tenant-a",
        "created_at": "2026-07-17T08:00:00+00:00",
        "updated_at": "2026-07-17T09:00:00+00:00",
        "summary": {"ranked_candidates": list(candidates)},
    }


def _content_sha(packet: dict[str, object]) -> str:
    content = dict(packet)
    content.pop("packet_sha256", None)
    serialized = json.dumps(
        content,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def test_projection_is_deterministic_versioned_and_sha_bound() -> None:
    record = _record(
        {
            "candidate_ref": "explicit-ref/kept",
            "title": "Apartment",
            "property_url": "https://example.test/listing/1",
            "property_facts": {"rooms": 3, "has_floorplan": True},
        }
    )

    first = project_property_research_packet_links(record)
    second = project_property_research_packet_links(record)

    assert first == second
    assert len(first) == 1
    link = first[0]
    packet = dict(link["packet_json"])
    assert link["candidate_ref"] == "explicit-ref/kept"
    assert link["candidate_ref_algorithm"] == "explicit"
    assert packet["candidate_ref"] == "explicit-ref/kept"
    assert packet["packet_schema_version"] == PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION
    assert packet["packet_sha256"] == _content_sha(packet) == link["packet_sha256"]
    assert link["packet_canonical_json"] == json.dumps(
        packet,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert link["packet_size_bytes"] == len(
        str(link["packet_canonical_json"]).encode("utf-8")
    )


def test_rank_beyond_compact_ui_limit_survives_packet_projection() -> None:
    candidates = [
        {
            "candidate_ref": f"candidate-{index}",
            "property_url": f"https://example.test/{index}",
        }
        for index in range(1, 46)
    ]

    links = project_property_research_packet_links(_record(*candidates))

    assert len(links) == 45
    target = next(link for link in links if link["candidate_ref"] == "candidate-45")
    assert dict(target["packet_json"])["packet_source_rank"] == 45


def test_missing_ref_uses_existing_stable_derived_v1_contract() -> None:
    candidate = {
        "title": "Apartment",
        "property_url": "https://example.test/derived",
        "review_url": "",
        "source_ref": "listing-7",
        "source_label": "Example",
    }
    expected = hashlib.sha1(
        "Apartment|https://example.test/derived||listing-7|Example".encode("utf-8")
    ).hexdigest()[:16]

    link = project_property_research_packet_links(_record(candidate))[0]

    assert link["candidate_ref"] == expected
    assert link["candidate_ref_algorithm"] == "derived_v1"


def test_same_url_with_multiple_explicit_refs_is_preserved() -> None:
    shared_url = "https://example.test/shared"

    links = project_property_research_packet_links(
        _record(
            {"candidate_ref": "ref-a", "property_url": shared_url},
            {"candidate_ref": "ref-b", "property_url": shared_url},
        )
    )

    assert {link["candidate_ref"] for link in links} == {"ref-a", "ref-b"}
    assert len({link["property_url_sha256"] for link in links}) == 1


def test_same_ref_with_conflicting_urls_fails_closed() -> None:
    with pytest.raises(PropertyResearchPacketConflictError, match="candidate_ref_url_conflict:ref-a"):
        project_property_research_packet_links(
            _record(
                {"candidate_ref": "ref-a", "property_url": "https://example.test/one"},
                {"candidate_ref": "ref-a", "property_url": "https://example.test/two"},
            )
        )


def test_oversize_packet_fails_explicitly_without_truncation() -> None:
    with pytest.raises(PropertyResearchPacketOversizeError, match="packet_max_bytes_exceeded"):
        project_property_research_packet_links(
            _record(
                {
                    "candidate_ref": "oversize",
                    "property_url": "https://example.test/oversize",
                    "description": "x" * PROPERTY_RESEARCH_PACKET_MAX_BYTES,
                }
            )
        )


def test_aggregate_packet_bytes_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(packet_links_module, "PROPERTY_RESEARCH_PACKET_MAX_AGGREGATE_BYTES", 500)

    with pytest.raises(PropertyResearchPacketOversizeError, match="packet_max_aggregate_bytes_exceeded"):
        project_property_research_packet_links(
            _record(
                {"candidate_ref": "ref-a", "description": "a" * 200},
                {"candidate_ref": "ref-b", "description": "b" * 200},
            )
        )


def test_projection_rejects_missing_observation_time_instead_of_using_wall_clock() -> None:
    record = _record({"candidate_ref": "ref-a", "property_url": "https://example.test/one"})
    record.pop("created_at")
    record.pop("updated_at")

    with pytest.raises(PropertyResearchPacketProjectionError, match="packet_observed_at_missing"):
        project_property_research_packet_links(record)


class _LookupCursor:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


def _lookup_row(link: dict[str, object]) -> tuple[object, ...]:
    return (
        link["candidate_ref"],
        dict(link["packet_json"]),
        link["packet_canonical_json"],
        link["packet_size_bytes"],
        link["packet_schema_version"],
        link["packet_sha256"],
        link["candidate_ref_algorithm"],
        link["property_url_sha256"],
        link["first_run_id"],
        link["last_run_id"],
        link["first_seen_at"],
        link["last_seen_at"],
        "active",
    )


def test_lookup_is_tenant_pk_scoped_and_contains_no_run_json_scan() -> None:
    link = project_property_research_packet_links(
        _record({"candidate_ref": "ref-a", "property_url": "https://example.test/one"})
    )[0]
    packet = dict(link["packet_json"])
    cursor = _LookupCursor(_lookup_row(link))

    loaded = load_property_research_packet_link(
        cursor,
        principal_id="tenant-a",
        candidate_ref="ref-a",
    )

    assert loaded and loaded["candidate"] == packet
    assert cursor.executed[0][1] == ("tenant-a", "ref-a")
    normalized_sql = " ".join(_PROPERTY_RESEARCH_PACKET_LINK_LOOKUP_SQL.lower().split())
    assert "where principal_id = %s and candidate_ref = %s" in normalized_sql
    assert "property_search_runs" not in normalized_sql
    assert "jsonb_array_elements" not in normalized_sql
    assert load_property_research_packet_link(
        _LookupCursor(None), principal_id="tenant-b", candidate_ref="ref-a"
    ) is None


def test_lookup_rejects_mixed_writer_packet_version_and_sha() -> None:
    link = project_property_research_packet_links(
        _record({"candidate_ref": "ref-a", "property_url": "https://example.test/one"})
    )[0]
    packet = dict(link["packet_json"])
    bad_version_row = list(_lookup_row(link))
    bad_version_row[4] = PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION + 1
    with pytest.raises(PropertyResearchPacketVersionError, match="packet_schema_version_mismatch"):
        load_property_research_packet_link(
            _LookupCursor(tuple(bad_version_row)), principal_id="tenant-a", candidate_ref="ref-a"
        )

    bad_sha_row = list(bad_version_row)
    bad_sha_row[4] = PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION
    bad_sha_row[5] = "0" * 64
    with pytest.raises(PropertyResearchPacketVersionError, match="packet_sha256_mismatch"):
        load_property_research_packet_link(
            _LookupCursor(tuple(bad_sha_row)), principal_id="tenant-a", candidate_ref="ref-a"
        )


@pytest.mark.parametrize(
    ("row_index", "replacement", "error"),
    (
        (0, "wrong-ref", "packet_candidate_ref_mismatch"),
        (3, 1, "packet_size_or_canonical_json_mismatch"),
        (6, "derived_v1", "packet_candidate_ref_algorithm_mismatch"),
        (7, "0" * 64, "packet_property_url_sha256_mismatch"),
    ),
)
def test_lookup_rejects_row_and_embedded_metadata_drift(
    row_index: int,
    replacement: object,
    error: str,
) -> None:
    link = project_property_research_packet_links(
        _record({"candidate_ref": "ref-a", "property_url": "https://example.test/one"})
    )[0]
    row = list(_lookup_row(link))
    row[row_index] = replacement

    with pytest.raises(PropertyResearchPacketVersionError, match=error):
        load_property_research_packet_link(
            _LookupCursor(tuple(row)), principal_id="tenant-a", candidate_ref="ref-a"
        )


class _ConflictCursor:
    def execute(self, _sql: str, _params: tuple[object, ...]) -> None:
        pass

    def fetchone(self) -> None:
        return None


@pytest.mark.parametrize(
    ("writer", "ordered_tokens"),
    (
        (
            upsert_property_research_packet_links,
            ("FOR UPDATE", "_PROPERTY_RESEARCH_PACKET_LINK_UPSERT_SQL"),
        ),
        (
            refresh_property_research_packet_links_for_refs,
            ("_refresh_property_research_packet_links_for_refs_unchecked(",),
        ),
        (
            sync_property_research_packet_run_memberships,
            (
                "FOR UPDATE",
                "_PROPERTY_RESEARCH_PACKET_MEMBERSHIP_UPSERT_SQL",
                "DELETE FROM property_research_packet_run_memberships",
                "_refresh_property_research_packet_links_for_refs_unchecked(",
            ),
        ),
    ),
)
def test_packet_writer_source_asserts_authority_before_row_lock_or_dml(
    writer: object,
    ordered_tokens: tuple[str, ...],
) -> None:
    source = inspect.getsource(writer)
    authority_index = source.index(
        "_assert_property_research_packet_write_authorities("
    )

    for token in ordered_tokens:
        assert authority_index < source.index(token)


def test_unchecked_refresh_contains_only_the_row_lock_and_materialization_dml() -> None:
    source = inspect.getsource(
        packet_links_module._refresh_property_research_packet_links_for_refs_unchecked
    )

    assert "_assert_property_research_packet_write_authorities(" not in source
    for token in (
        "FOR UPDATE",
        "DELETE FROM property_research_packet_links",
        "UPDATE property_research_packet_links",
    ):
        assert token in source


def test_upsert_asserts_each_exact_run_once_before_packet_row_locks() -> None:
    links = project_property_research_packet_links(
        _record(
            {"candidate_ref": "ref-a", "property_url": "https://example.test/one"},
            {"candidate_ref": "ref-b", "property_url": "https://example.test/two"},
        )
    )

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[tuple[str, object]] = []

        def execute(self, sql: str, params: object) -> None:
            self.executed.append((" ".join(sql.split()), params))

        def fetchone(self) -> tuple[str] | None:
            if "FOR UPDATE" in self.executed[-1][0]:
                return None
            return ("written",)

    cursor = _Cursor()
    assert upsert_property_research_packet_links(
        cursor,
        (link for link in links),
    ) == 2

    authority_sql = packet_links_module._PROPERTY_SEARCH_ASSERT_PRINCIPAL_WRITE_ALLOWED_SQL
    authority_calls = [entry for entry in cursor.executed if entry[0] == authority_sql]
    assert authority_calls == [(authority_sql, ("tenant-a", "run-1"))]
    assert cursor.executed[0] == authority_calls[0]
    assert all(
        cursor.executed.index(authority_calls[0]) < index
        for index, (sql, _params) in enumerate(cursor.executed)
        if "FOR UPDATE" in sql or sql.startswith("INSERT INTO")
    )


def test_refresh_asserts_account_authority_once_before_deduplicated_row_locks() -> None:
    class _Cursor:
        rowcount = 1

        def __init__(self) -> None:
            self.executed: list[tuple[str, object]] = []

        def execute(self, sql: str, params: object) -> None:
            self.executed.append((" ".join(sql.split()), params))

        def fetchone(self) -> tuple[str]:
            return ("legal_hold",)

    cursor = _Cursor()
    assert refresh_property_research_packet_links_for_refs(
        cursor,
        principal_id="tenant-a",
        candidate_refs=(value for value in ("ref-a", " ref-a ", "")),
    ) == 1

    authority_sql = packet_links_module._PROPERTY_SEARCH_ASSERT_PRINCIPAL_WRITE_ALLOWED_SQL
    assert cursor.executed[0] == (authority_sql, ("tenant-a", ""))
    assert [entry for entry in cursor.executed if entry[0] == authority_sql] == [
        (authority_sql, ("tenant-a", ""))
    ]
    assert sum("FOR UPDATE" in sql for sql, _params in cursor.executed) == 1


def test_membership_sync_reuses_exact_run_authority_during_internal_refresh() -> None:
    link = project_property_research_packet_links(
        _record({"candidate_ref": "ref-a", "property_url": "https://example.test/one"})
    )[0]

    class _Cursor:
        rowcount = 1

        def __init__(self) -> None:
            self.executed: list[tuple[str, object]] = []

        def execute(self, sql: str, params: object) -> None:
            self.executed.append((" ".join(sql.split()), params))

        def fetchone(self) -> tuple[str]:
            if "INSERT INTO property_research_packet_run_memberships" in self.executed[-1][0]:
                return ("ref-a",)
            if "SELECT retention_state FROM property_research_packet_links" in self.executed[-1][0]:
                return ("legal_hold",)
            raise AssertionError(f"unexpected fetchone after {self.executed[-1][0]}")

        def fetchall(self) -> list[tuple[str]]:
            if "FOR UPDATE" in self.executed[-1][0]:
                return [("removed-ref",)]
            if self.executed[-1][0].startswith("DELETE FROM"):
                return [("removed-ref",)]
            raise AssertionError(f"unexpected fetchall after {self.executed[-1][0]}")

    cursor = _Cursor()
    assert sync_property_research_packet_run_memberships(
        cursor,
        principal_id="tenant-a",
        run_id="run-1",
        links=(link for link in (link,)),
    ) == 1

    authority_sql = packet_links_module._PROPERTY_SEARCH_ASSERT_PRINCIPAL_WRITE_ALLOWED_SQL
    assert [entry for entry in cursor.executed if entry[0] == authority_sql] == [
        (authority_sql, ("tenant-a", "run-1"))
    ]
    assert cursor.executed[0] == (authority_sql, ("tenant-a", "run-1"))


@pytest.mark.parametrize(
    ("writer_kind", "error_code"),
    (
        ("upsert", "packet_max_links_per_write_exceeded"),
        ("refresh", "packet_max_refresh_refs_exceeded"),
        ("sync", "packet_max_membership_links_exceeded"),
    ),
)
def test_packet_writer_iterables_are_materialized_with_a_hard_bound(
    monkeypatch: pytest.MonkeyPatch,
    writer_kind: str,
    error_code: str,
) -> None:
    monkeypatch.setattr(
        packet_links_module,
        "PROPERTY_RESEARCH_PACKET_MAX_CANDIDATES_PER_RUN",
        2,
    )
    link = project_property_research_packet_links(
        _record({"candidate_ref": "ref-a", "property_url": "https://example.test/one"})
    )[0]

    class _NoSqlCursor:
        @staticmethod
        def execute(_sql: str, _params: object) -> None:
            pytest.fail("oversize iterable reached SQL")

    def _unbounded(value: object):  # type: ignore[no-untyped-def]
        while True:
            yield value

    with pytest.raises(PropertyResearchPacketOversizeError, match=error_code):
        if writer_kind == "upsert":
            upsert_property_research_packet_links(_NoSqlCursor(), _unbounded(link))
        elif writer_kind == "refresh":
            refresh_property_research_packet_links_for_refs(
                _NoSqlCursor(),
                principal_id="tenant-a",
                candidate_refs=_unbounded("ref-a"),
            )
        else:
            sync_property_research_packet_run_memberships(
                _NoSqlCursor(),
                principal_id="tenant-a",
                run_id="run-1",
                links=_unbounded(link),
            )


def test_upsert_reports_cross_run_url_identity_conflict() -> None:
    link = project_property_research_packet_links(
        _record({"candidate_ref": "ref-a", "property_url": "https://example.test/one"})
    )[0]
    with pytest.raises(PropertyResearchPacketConflictError, match="candidate_ref_packet_conflict:ref-a"):
        upsert_property_research_packet_links(_ConflictCursor(), (link,))


def test_membership_reselection_promotes_newest_surviving_run() -> None:
    link = project_property_research_packet_links(
        _record({"candidate_ref": "ref-a", "property_url": "https://example.test/one"})
    )[0]
    executed: list[tuple[str, object]] = []

    class _Cursor:
        rowcount = 1

        def __init__(self) -> None:
            self.fetchone_calls = 0

        def execute(self, sql: str, params: object) -> None:
            executed.append((" ".join(sql.split()), params))

        def fetchone(self) -> tuple[object, ...]:
            self.fetchone_calls += 1
            if self.fetchone_calls == 1:
                return ("active",)
            if self.fetchone_calls == 2:
                return (
                    link["candidate_ref_algorithm"],
                    link["packet_json"],
                    link["packet_canonical_json"],
                    link["packet_size_bytes"],
                    link["packet_schema_version"],
                    link["packet_sha256"],
                    link["property_url_sha256"],
                    "run-surviving-latest",
                    "2026-07-16T09:00:00+00:00",
                )
            return ("run-surviving-first", "2026-07-15T09:00:00+00:00")

    assert refresh_property_research_packet_links_for_refs(
        _Cursor(),
        principal_id="tenant-a",
        candidate_refs=("ref-a",),
    ) == 1
    update_sql, update_params = executed[-1]
    assert "UPDATE property_research_packet_links" in update_sql
    assert update_params[7:11] == (
        "run-surviving-first",
        "run-surviving-latest",
        "2026-07-15T09:00:00+00:00",
        "2026-07-16T09:00:00+00:00",
    )


def test_membership_reselection_retains_evidence_only_legal_hold_without_a_winner() -> None:
    executed: list[str] = []

    class _Cursor:
        rowcount = 1

        def execute(self, sql: str, _params: object = None) -> None:
            executed.append(" ".join(sql.split()))

        def fetchone(self) -> tuple[str]:
            return ("legal_hold",)

    assert refresh_property_research_packet_links_for_refs(
        _Cursor(),
        principal_id="tenant-a",
        candidate_refs=("held-ref",),
    ) == 1
    assert len(executed) == 2
    assert "property_search_assert_principal_write_allowed" in executed[0]
    assert "SELECT retention_state" in executed[1]


def test_legal_hold_packet_is_immutable_under_later_upsert_and_reselection() -> None:
    held = project_property_research_packet_links(
        _record(
            {
                "candidate_ref": "held-ref",
                "property_url": "https://example.test/held",
                "title": "Held bytes",
            }
        )
    )[0]
    later_record = _record(
        {
            "candidate_ref": "held-ref",
            "property_url": "https://example.test/held",
            "title": "Later bytes must not replace hold",
        }
    )
    later_record["run_id"] = "run-later"
    later_record["updated_at"] = "2026-07-18T10:00:00+00:00"
    later = project_property_research_packet_links(later_record)[0]
    executed: list[str] = []

    class _HeldCursor:
        rowcount = 1

        def execute(self, sql: str, _params: object = None) -> None:
            executed.append(" ".join(sql.split()))

        def fetchone(self) -> tuple[object, ...]:
            return ("legal_hold", held["property_url_sha256"])

    assert upsert_property_research_packet_links(_HeldCursor(), (later,)) == 1
    assert len(executed) == 2
    assert "property_search_assert_principal_write_allowed" in executed[0]
    assert "FOR UPDATE" in executed[1]
    assert all("INSERT INTO" not in sql and "UPDATE " not in sql for sql in executed)

    executed.clear()

    class _HeldRefreshCursor:
        rowcount = 1

        def execute(self, sql: str, _params: object) -> None:
            executed.append(" ".join(sql.split()))

        def fetchone(self) -> tuple[str]:
            return ("legal_hold",)

    assert refresh_property_research_packet_links_for_refs(
        _HeldRefreshCursor(),
        principal_id="tenant-a",
        candidate_refs=("held-ref",),
    ) == 1
    assert len(executed) == 2
    assert "property_search_assert_principal_write_allowed" in executed[0]
    assert "FOR UPDATE" in executed[1]
    assert all("UPDATE " not in sql and "DELETE FROM" not in sql for sql in executed)
    assert held["packet_sha256"] != later["packet_sha256"]


def test_non_null_property_url_identity_anchor_cannot_be_cleared_or_changed() -> None:
    anchored = project_property_research_packet_links(
        _record(
            {
                "candidate_ref": "anchored-ref",
                "property_url": "https://example.test/original",
            }
        )
    )[0]

    class _AnchoredCursor:
        def execute(self, _sql: str, _params: object) -> None:
            return None

        def fetchone(self) -> tuple[object, ...]:
            return ("active", anchored["property_url_sha256"])

    for incoming_url in ("", "https://example.test/different"):
        incoming_record = _record(
            {"candidate_ref": "anchored-ref", "property_url": incoming_url}
        )
        incoming_record["run_id"] = f"run-{incoming_url or 'null'}"
        incoming_record["updated_at"] = "2026-07-18T11:00:00+00:00"
        incoming = project_property_research_packet_links(incoming_record)[0]
        with pytest.raises(
            PropertyResearchPacketConflictError,
            match="candidate_ref_packet_conflict:anchored-ref",
        ):
            upsert_property_research_packet_links(_AnchoredCursor(), (incoming,))

    normalized_sql = " ".join(
        packet_links_module._PROPERTY_RESEARCH_PACKET_LINK_UPSERT_SQL.split()
    )
    normalized_membership_sql = " ".join(
        packet_links_module._PROPERTY_RESEARCH_PACKET_MEMBERSHIP_UPSERT_SQL.split()
    )
    assert "OR EXCLUDED.property_url_sha256 IS NULL" not in normalized_sql
    assert "OR EXCLUDED.property_url_sha256 IS NULL" not in normalized_membership_sql


def test_run_delete_and_account_erasure_set_writer_contract_and_preserve_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executions: list[str] = []
    execution_params: list[object] = []

    class _Cursor:
        rowcount = 0

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object = None) -> None:
            normalized = " ".join(sql.split())
            executions.append(normalized)
            execution_params.append(_params)
            if normalized.startswith("DELETE FROM property_search_runs"):
                self.rowcount = 2
            elif normalized.startswith("DELETE FROM property_search_work_jobs"):
                self.rowcount = 1
            elif normalized.startswith("DELETE FROM property_research_packet_links"):
                self.rowcount = 1
            else:
                self.rowcount = 0

        def fetchall(self) -> list[tuple[str]]:
            return [("ref-a",)] if "SELECT candidate_ref" in executions[-1] else []

        def fetchone(self) -> tuple[int]:
            return (1,)

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

        def transaction(self):
            return contextlib.nullcontext()

    @contextlib.contextmanager
    def _connect() -> object:
        yield _Connection()

    refreshed: list[tuple[str, ...]] = []
    monkeypatch.setattr(storage, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setenv("PROPERTYQUARRY_PRIVACY_LOOKUP_SECRET", "erasure-fence-test-secret")
    monkeypatch.setattr(storage, "_require_property_search_run_schema", lambda: None)
    monkeypatch.setattr(storage, "_property_search_run_connect", _connect)
    monkeypatch.setattr(
        storage,
        "refresh_property_research_packet_links_for_refs",
        lambda _cursor, *, principal_id, candidate_refs: refreshed.append(
            tuple(candidate_refs)
        )
        or 1,
    )

    assert storage._delete_property_search_run_record(
        run_id="run-a", principal_id="tenant-a"
    ) is True
    delete_executions = list(executions)
    assert "'propertyquarry.property_search_writer_contract'" in delete_executions[0]
    assert "'propertyquarry.property_search_erasure_key_id'" in delete_executions[0]
    assert refreshed == [("ref-a",)]

    executions.clear()
    execution_params.clear()
    assert storage._erase_property_search_account_data(
        principal_ids=("tenant-a", "cf-email:owner@example.test"),
    ) == {
        "runs_deleted": 2,
        "work_jobs_deleted": 1,
        "packet_links_deleted": 1,
        "packet_links_legal_hold_retained": 1,
    }
    assert "'propertyquarry.property_search_writer_contract'" in executions[0]
    assert "'propertyquarry.property_search_erasure_key_id'" in executions[0]
    fence_writes = [
        params
        for sql, params in zip(executions, execution_params, strict=True)
        if sql.startswith("INSERT INTO property_search_erasure_fences")
    ]
    assert len(fence_writes) == 2
    assert all(str(tuple(params)[0]).startswith("hmac-sha256:") for params in fence_writes)
    assert all("owner@example.test" not in str(params) for params in fence_writes)
    assert any(sql.startswith("DELETE FROM property_search_work_jobs") for sql in executions)
    held_delete = next(
        sql
        for sql in executions
        if sql.startswith("DELETE FROM property_research_packet_links")
    )
    assert "retention_state <> 'legal_hold'" in held_delete


def test_erasure_fence_identity_is_keyed_and_rotation_changes_key_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = "cf-email:owner@example.test"
    monkeypatch.setenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET", "old-process-secret"
    )
    before = storage._property_search_principal_key(principal_id)
    before_key_id = storage._property_search_erasure_key_id()
    monkeypatch.setenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET", "new-process-secret"
    )
    monkeypatch.setenv("EA_SIGNING_SECRET", "different-fleet-secret")
    after = storage._property_search_principal_key(principal_id)
    after_key_id = storage._property_search_erasure_key_id()

    assert before != after
    assert before_key_id != after_key_id
    assert before.startswith("hmac-sha256:")
    assert after.startswith("hmac-sha256:")
    assert principal_id not in before
    assert principal_id not in after


def test_production_requires_dedicated_erasure_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    monkeypatch.delenv("PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET", raising=False)
    monkeypatch.setenv("PROPERTYQUARRY_PRIVACY_LOOKUP_SECRET", "unrelated-privacy-secret")
    monkeypatch.setenv("EA_SIGNING_SECRET", "unrelated-signing-secret")

    with pytest.raises(RuntimeError, match="property_search_erasure_secret_required"):
        storage._property_search_principal_key("tenant-prod")

    monkeypatch.setenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET", "predictable-short-value"
    )
    with pytest.raises(RuntimeError, match="property_search_erasure_secret_too_short"):
        storage._property_search_principal_key("tenant-prod")


def test_exact_run_delete_does_not_reselect_immutable_held_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held_snapshot = {
        "packet_json": {"held": True},
        "packet_sha256": "a" * 64,
        "packet_canonical_json": '{"held":true}',
        "packet_size_bytes": 13,
        "first_run_id": "run-held",
        "last_run_id": "run-held",
        "first_seen_at": "2026-07-17T08:00:00+00:00",
        "last_seen_at": "2026-07-17T08:00:00+00:00",
    }
    memberships = {
        "run-held": {"candidate_ref": "held-ref", "packet_sha256": "a" * 64},
        "run-alternate": {
            "candidate_ref": "held-ref",
            "packet_sha256": "b" * 64,
            "packet_canonical_json": '{"alternate":true}',
            "last_seen_at": "2026-07-18T08:00:00+00:00",
        },
    }
    executed: list[str] = []

    class _Cursor:
        rowcount = 0

        def __init__(self) -> None:
            self._rows: list[tuple[str]] = []
            self._row: tuple[str] | None = None

        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object = None) -> None:
            normalized = " ".join(sql.split())
            executed.append(normalized)
            self._rows = []
            self._row = None
            self.rowcount = 0
            if normalized.startswith(
                "SELECT candidate_ref FROM property_research_packet_run_memberships"
            ):
                self._rows = (
                    [("held-ref",)] if "run-held" in memberships else []
                )
            elif normalized.startswith("DELETE FROM property_search_runs"):
                memberships.pop("run-held", None)
                self.rowcount = 1
            elif normalized.startswith("SELECT retention_state"):
                self._row = ("legal_hold",)
            elif "FROM property_research_packet_run_memberships" in normalized:
                raise AssertionError(
                    "legal hold must not reselect the surviving alternate membership"
                )

        def fetchall(self) -> list[tuple[str]]:
            return list(self._rows)

        def fetchone(self) -> tuple[str]:
            assert self._row is not None
            return self._row

    class _Connection:
        def cursor(self) -> _Cursor:
            return _Cursor()

        def transaction(self):
            return contextlib.nullcontext()

    @contextlib.contextmanager
    def _connect() -> object:
        yield _Connection()

    monkeypatch.setattr(storage, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(storage, "_require_property_search_run_schema", lambda: None)
    monkeypatch.setattr(storage, "_property_search_run_connect", _connect)

    before = dict(held_snapshot)
    assert storage._delete_property_search_run_record(
        principal_id="tenant-a", run_id="run-held"
    ) is True
    assert held_snapshot == before
    assert "run-held" not in memberships
    assert memberships["run-alternate"]["packet_sha256"] == "b" * 64
    assert any("SELECT retention_state" in sql for sql in executed)
    assert not any(
        sql.startswith("UPDATE property_research_packet_links")
        or sql.startswith("DELETE FROM property_research_packet_links")
        for sql in executed
    )


def test_compact_mixed_writer_payload_is_rejected_into_repair_envelope() -> None:
    corrupt = storage._compact_property_search_run_record_with_row_timestamps(
        {
            "run_id": "run-old-writer",
            "principal_id": "tenant-a",
            "status": "completed",
            "summary": {"ranked_candidates": [{"candidate_ref": "lost-if-trusted"}]},
        },
        compact_schema_version=storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        delivery_pending=False,
    )

    assert corrupt == {
        "run_id": "run-old-writer",
        "principal_id": "tenant-a",
        "status": "completed",
        "compact_schema_version": 0,
        "compact_contract_status": "repair_required",
        "compact_contract_expected_version": storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        "compact_contract_row_version": storage._PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        "compact_contract_embedded_version": 0,
        "delivery_pending": True,
    }


def test_dual_write_failure_rolls_back_run_update(monkeypatch: pytest.MonkeyPatch) -> None:
    database_state = {"run_written": False}
    transaction_entries: list[str] = []

    class _Cursor:
        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object) -> None:
            if "INSERT INTO property_search_runs" in sql:
                database_state["run_written"] = True

    class _Transaction:
        def __enter__(self) -> None:
            transaction_entries.append("entered")
            self.snapshot = dict(database_state)

        def __exit__(self, exc_type: object, *_args: object) -> bool:
            if exc_type is not None:
                database_state.clear()
                database_state.update(self.snapshot)
            return False

    class _Connection:
        def transaction(self) -> _Transaction:
            return _Transaction()

        def cursor(self) -> _Cursor:
            return _Cursor()

    @contextlib.contextmanager
    def _connect() -> object:
        yield _Connection()

    monkeypatch.setattr(storage, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(storage, "_require_property_search_run_schema", lambda: None)
    monkeypatch.setattr(storage, "_property_search_run_connect", _connect)
    monkeypatch.setattr(
        storage,
        "upsert_property_research_packet_links",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index_write_failed")),
    )

    with pytest.raises(RuntimeError, match="index_write_failed"):
        storage._store_property_search_run_record(
            _record({"candidate_ref": "ref-a", "property_url": "https://example.test/one"})
        )

    assert database_state["run_written"] is False
    assert transaction_entries == ["entered"]


def test_compact_refresh_never_projects_or_writes_packet_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[str] = []

    class _Cursor:
        def __enter__(self) -> "_Cursor":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _params: object) -> None:
            executed.append(" ".join(sql.split()))

        def fetchone(self) -> tuple[int]:
            return (1,)

    class _Transaction:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_args: object) -> bool:
            return False

    class _Connection:
        def transaction(self) -> _Transaction:
            return _Transaction()

        def cursor(self) -> _Cursor:
            return _Cursor()

    @contextlib.contextmanager
    def _connect() -> object:
        yield _Connection()

    monkeypatch.setattr(storage, "_property_search_run_database_url", lambda: "postgresql://test")
    monkeypatch.setattr(storage, "_require_property_search_run_schema", lambda: None)
    monkeypatch.setattr(storage, "_property_search_run_connect", _connect)
    monkeypatch.setattr(
        storage,
        "project_property_research_packet_links",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("compact writer projected packets")),
    )
    monkeypatch.setattr(
        storage,
        "upsert_property_research_packet_links",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("compact writer wrote packets")),
    )
    record = _record({"candidate_ref": "ref-a", "description": "x" * 5000})

    assert storage._store_property_search_run_compact_record(record) is True
    assert len(executed) == 2
    assert "'propertyquarry.property_search_writer_contract'" in executed[0]
    assert "'propertyquarry.property_search_erasure_key_id'" in executed[0]
    assert "UPDATE property_search_runs" in executed[1]
    assert all("property_research_packet" not in sql for sql in executed)


@pytest.mark.skipif(
    os.environ.get("EA_RUN_PROPERTY_SEARCH_POSTGRES_INTEGRATION") != "1",
    reason="explicit isolated PostgreSQL integration lane only",
)
def test_postgres_canonical_packet_size_boundary_round_trips_in_rollback() -> None:
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("DATABASE_URL is required for the explicit integration lane")
    link = project_property_research_packet_links(
        _record(
            {
                "candidate_ref": "near-boundary",
                "property_url": "https://example.test/near-boundary",
                "description": "ü" * 120_000,
            }
        )
    )[0]
    assert 200_000 < int(link["packet_size_bytes"]) <= PROPERTY_RESEARCH_PACKET_MAX_BYTES

    import psycopg
    from psycopg.types.json import Json

    connection = psycopg.connect(database_url, autocommit=False, connect_timeout=5)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE packet_size_contract_test (
                    packet_json JSONB NOT NULL,
                    packet_canonical_json TEXT NOT NULL,
                    packet_size_bytes INTEGER NOT NULL,
                    CHECK (packet_json = packet_canonical_json::jsonb),
                    CHECK (
                        packet_size_bytes = octet_length(
                            convert_to(packet_canonical_json, 'UTF8')
                        )
                    ),
                    CHECK (packet_size_bytes BETWEEN 2 AND 262144)
                ) ON COMMIT DROP
                """
            )
            cursor.execute(
                """
                INSERT INTO packet_size_contract_test
                    (packet_json, packet_canonical_json, packet_size_bytes)
                VALUES (%s, %s, %s)
                RETURNING packet_size_bytes
                """,
                (
                    Json(link["packet_json"]),
                    link["packet_canonical_json"],
                    link["packet_size_bytes"],
                ),
            )
            assert int(cursor.fetchone()[0]) == int(link["packet_size_bytes"])
    finally:
        connection.rollback()
        connection.close()


@pytest.mark.skipif(
    os.environ.get("EA_RUN_PROPERTY_SEARCH_POSTGRES_INTEGRATION") != "1",
    reason="explicit isolated PostgreSQL integration lane only",
)
def test_postgres_delete_writer_guard_and_current_reselection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("DATABASE_URL is required for the explicit integration lane")

    import psycopg
    from psycopg import sql

    schema_name = f"packet_delete_contract_{uuid.uuid4().hex}"
    erasure_secret = "postgres-run-delete-test-secret"
    monkeypatch.setenv(
        "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET", erasure_secret
    )
    principal_key = storage._property_search_principal_key("tenant-a")
    admin = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        setup = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
        try:
            with setup.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
                )
                for migration in search_schema.PROPERTY_SEARCH_MIGRATIONS:
                    cursor.execute(migration.sql)

            with pytest.raises(psycopg.Error) as unmarked_insert:
                with setup.transaction():
                    with setup.cursor() as cursor:
                        cursor.execute(
                            """
                            INSERT INTO property_search_runs
                                (principal_id, run_id, payload_json)
                            VALUES ('tenant-a', 'run-unmarked', '{}'::jsonb)
                            """
                        )
            assert unmarked_insert.value.sqlstate == "23514"

            with pytest.raises(psycopg.Error) as missing_principal_key:
                with setup.transaction():
                    with setup.cursor() as cursor:
                        storage._set_property_search_writer_contract(cursor)
                        cursor.execute(
                            """
                            INSERT INTO property_search_runs
                                (principal_id, run_id, payload_json)
                            VALUES ('tenant-a', 'run-missing-key', '{}'::jsonb)
                            """
                        )
            assert missing_principal_key.value.sqlstate == "23514"
            assert "property_search_principal_key_required" in str(
                missing_principal_key.value
            )

            with setup.transaction():
                with setup.cursor() as cursor:
                    storage._set_property_search_writer_contract(cursor)
                    for run_id, observed_at in (
                        ("run-old", "2026-07-17T08:00:00+00:00"),
                        ("run-new", "2026-07-17T09:00:00+00:00"),
                    ):
                        record = _record(
                            {
                                "candidate_ref": "shared-ref",
                                "property_url": "https://example.test/shared-ref",
                                "title": run_id,
                            }
                        )
                        record["run_id"] = run_id
                        record["created_at"] = observed_at
                        record["updated_at"] = observed_at
                        cursor.execute(
                            """
                            INSERT INTO property_search_runs
                                (principal_id, run_id, principal_key, payload_json,
                                 created_at, updated_at)
                            VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                            """,
                            (
                                "tenant-a",
                                run_id,
                                principal_key,
                                json.dumps(record),
                                observed_at,
                                observed_at,
                            ),
                        )
                        links = project_property_research_packet_links(record)
                        upsert_property_research_packet_links(cursor, links)
                        packet_links_module.sync_property_research_packet_run_memberships(
                            cursor,
                            principal_id="tenant-a",
                            run_id=run_id,
                            links=links,
                        )

            with setup.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM property_research_packet_links),
                        (
                            SELECT COUNT(*)
                            FROM property_research_packet_run_memberships
                        )
                    """
                )
                assert cursor.fetchone() == (1, 2)

            for statement in (
                """
                UPDATE property_search_runs
                SET status = 'unmarked'
                WHERE principal_id = 'tenant-a' AND run_id = 'run-new'
                """,
                """
                DELETE FROM property_search_runs
                WHERE principal_id = 'tenant-a' AND run_id = 'run-new'
                """,
            ):
                with pytest.raises(psycopg.Error) as rejected:
                    with setup.transaction():
                        with setup.cursor() as cursor:
                            cursor.execute(statement)
                assert rejected.value.sqlstate == "23514"
        finally:
            setup.close()

        @contextlib.contextmanager
        def _isolated_connect() -> object:
            connection = psycopg.connect(
                database_url, autocommit=True, connect_timeout=5
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("SET search_path TO {}").format(
                            sql.Identifier(schema_name)
                        )
                    )
                yield connection
            finally:
                connection.close()

        monkeypatch.setattr(storage, "_property_search_run_database_url", lambda: database_url)
        monkeypatch.setattr(storage, "_require_property_search_run_schema", lambda: None)
        monkeypatch.setattr(storage, "_property_search_run_connect", _isolated_connect)

        monkeypatch.setenv(
            "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET",
            "rotated-without-database-migration",
        )
        with pytest.raises(psycopg.Error) as key_mismatch:
            storage._delete_property_search_run_record(
                principal_id="tenant-a", run_id="run-new"
            )
        assert key_mismatch.value.sqlstate == "23514"
        assert "property_search_erasure_key_mismatch" in str(key_mismatch.value)

        retained = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
        try:
            with retained.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
                )
                cursor.execute(
                    "SELECT COUNT(*) FROM property_search_runs WHERE run_id = 'run-new'"
                )
                assert cursor.fetchone() == (1,)
        finally:
            retained.close()

        monkeypatch.setenv(
            "PROPERTYQUARRY_PROPERTY_SEARCH_ERASURE_SECRET", erasure_secret
        )
        assert storage._delete_property_search_run_record(
            principal_id="tenant-a", run_id="run-new"
        ) is True

        verify = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
        try:
            with verify.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
                )
                cursor.execute(
                    """
                    SELECT last_run_id
                    FROM property_research_packet_links
                    WHERE principal_id = 'tenant-a' AND candidate_ref = 'shared-ref'
                    """
                )
                assert cursor.fetchone() == ("run-old",)
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM property_research_packet_run_memberships
                    WHERE principal_id = 'tenant-a' AND run_id = 'run-new'
                    """
                )
                assert cursor.fetchone() == (0,)
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM property_search_erasure_fences
                    WHERE principal_key = %s AND run_id = 'run-new'
                    """,
                    (principal_key,),
                )
                assert cursor.fetchone() == (1,)

            with pytest.raises(psycopg.Error) as stale_run_write:
                with verify.transaction():
                    with verify.cursor() as cursor:
                        storage._set_property_search_writer_contract(cursor)
                        cursor.execute(
                            """
                            INSERT INTO property_search_runs (
                                principal_id, run_id, principal_key, payload_json,
                                created_at, updated_at
                            )
                            VALUES (%s, 'run-new', %s, '{}'::jsonb,
                                    '2099-01-01T00:00:00+00:00',
                                    '2099-01-01T00:00:00+00:00')
                            """,
                            ("tenant-a", principal_key),
                        )
            assert stale_run_write.value.sqlstate == "23514"
            assert "property_search_account_erased" in str(stale_run_write.value)

            with pytest.raises(psycopg.Error) as stale_job_write:
                with verify.transaction():
                    with verify.cursor() as cursor:
                        storage._set_property_search_writer_contract(cursor)
                        cursor.execute(
                            """
                            INSERT INTO property_search_work_jobs (
                                job_id, principal_id, run_id, principal_key,
                                idempotency_key
                            )
                            VALUES ('job-stale', 'tenant-a', 'run-new', %s, 'key-stale')
                            """,
                            (principal_key,),
                        )
            assert stale_job_write.value.sqlstate == "23514"
            assert "property_search_account_erased" in str(stale_job_write.value)

            from app.product.property_search_work_queue import (
                PostgresPropertySearchWorkQueue,
            )

            repository = object.__new__(PostgresPropertySearchWorkQueue)
            repository._backoff_seconds = lambda _attempt: 0

            def _queue_connect():  # type: ignore[no-untyped-def]
                return psycopg.connect(
                    database_url,
                    autocommit=False,
                    connect_timeout=5,
                    options=f"-csearch_path={schema_name}",
                )

            repository._connect = _queue_connect  # type: ignore[method-assign]
            late_record = _record(
                {
                    "candidate_ref": "late-run-fence-ref",
                    "property_url": "https://example.test/late-run-fence",
                }
            )
            late_record.update(
                {"principal_id": "tenant-a", "run_id": "run-new"}
            )
            with pytest.raises(psycopg.Error) as late_queue_write:
                repository.enqueue_run(
                    run_record=late_record,
                    payload_json={"run_id": "run-new"},
                    idempotency_key="late-run-fence-key",
                )
            assert late_queue_write.value.sqlstate == "23514"
            assert "property_search_account_erased" in str(late_queue_write.value)
            with verify.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM property_search_runs WHERE run_id = 'run-new'"
                )
                assert cursor.fetchone() == (0,)
                cursor.execute(
                    "SELECT COUNT(*) FROM property_search_work_jobs WHERE run_id = 'run-new'"
                )
                assert cursor.fetchone() == (0,)
        finally:
            verify.close()
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )
        finally:
            admin.close()


@pytest.mark.skipif(
    os.environ.get("EA_RUN_PROPERTY_SEARCH_POSTGRES_INTEGRATION") != "1",
    reason="explicit isolated PostgreSQL integration lane only",
)
def test_postgres_account_erasure_is_atomic_across_aliases_and_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("DATABASE_URL is required for the explicit integration lane")

    import psycopg
    from psycopg import sql

    from app.product.property_search_work_queue import PostgresPropertySearchWorkQueue

    schema_name = f"account_erasure_contract_{uuid.uuid4().hex}"
    aliases = ("tenant-erasure", "cf-email:owner@example.test")
    monkeypatch.setenv(
        "PROPERTYQUARRY_PRIVACY_LOOKUP_SECRET",
        "postgres-account-erasure-test-secret",
    )
    admin = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        setup = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
        try:
            with setup.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
                )
                for migration in search_schema.PROPERTY_SEARCH_MIGRATIONS:
                    cursor.execute(migration.sql)
        finally:
            setup.close()

        @contextlib.contextmanager
        def _isolated_connect() -> object:
            connection = psycopg.connect(
                database_url, autocommit=True, connect_timeout=5
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("SET search_path TO {}").format(
                            sql.Identifier(schema_name)
                        )
                    )
                yield connection
            finally:
                connection.close()

        monkeypatch.setattr(storage, "_property_search_run_database_url", lambda: database_url)
        monkeypatch.setattr(storage, "_require_property_search_run_schema", lambda: None)
        monkeypatch.setattr(storage, "_property_search_run_connect", _isolated_connect)

        for index, principal_id in enumerate(aliases, start=1):
            record = _record(
                {
                    "candidate_ref": f"erasure-ref-{index}",
                    "property_url": f"https://example.test/erasure-{index}",
                }
            )
            record.update(
                {
                    "principal_id": principal_id,
                    "run_id": f"erasure-run-{index}",
                }
            )
            assert storage._store_property_search_run_record(record) is True

        seed = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
        try:
            with seed.transaction():
                with seed.cursor() as cursor:
                    cursor.execute(
                        sql.SQL("SET LOCAL search_path TO {}").format(
                            sql.Identifier(schema_name)
                        )
                    )
                    storage._set_property_search_writer_contract(cursor)
                    for index, principal_id in enumerate(aliases, start=1):
                        cursor.execute(
                            """
                            INSERT INTO property_search_work_jobs (
                                job_id, principal_id, run_id, principal_key,
                                idempotency_key
                            )
                            VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                f"erasure-job-{index}",
                                principal_id,
                                f"erasure-run-{index}",
                                storage._property_search_principal_key(principal_id),
                                f"erasure-key-{index}",
                            ),
                        )
                    cursor.execute(
                        """
                        UPDATE property_research_packet_links
                        SET retention_state = 'legal_hold'
                        WHERE principal_id = %s AND candidate_ref = 'erasure-ref-1'
                        """,
                        (aliases[0],),
                    )
                    cursor.execute(
                        """
                        CREATE FUNCTION reject_erasure_packet_delete()
                        RETURNS TRIGGER LANGUAGE plpgsql AS $$
                        BEGIN
                            RAISE EXCEPTION 'synthetic_packet_delete_failure';
                        END
                        $$
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TRIGGER reject_erasure_packet_delete_guard
                        BEFORE DELETE ON property_research_packet_links
                        FOR EACH ROW
                        WHEN (OLD.retention_state <> 'legal_hold')
                        EXECUTE FUNCTION reject_erasure_packet_delete()
                        """
                    )
        finally:
            seed.close()

        with pytest.raises(psycopg.Error, match="synthetic_packet_delete_failure"):
            storage._erase_property_search_account_data(principal_ids=aliases)

        verify = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
        try:
            with verify.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
                )
                for table_name, expected in (
                    ("property_search_runs", 2),
                    ("property_search_work_jobs", 2),
                    ("property_research_packet_run_memberships", 2),
                    ("property_research_packet_links", 2),
                    ("property_search_erasure_fences", 0),
                ):
                    cursor.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}").format(
                            sql.Identifier(table_name)
                        )
                    )
                    assert cursor.fetchone() == (expected,)
                cursor.execute("DROP TRIGGER reject_erasure_packet_delete_guard ON property_research_packet_links")

            assert storage._erase_property_search_account_data(
                principal_ids=aliases,
            ) == {
                "runs_deleted": 2,
                "work_jobs_deleted": 2,
                "packet_links_deleted": 1,
                "packet_links_legal_hold_retained": 1,
            }

            with verify.cursor() as cursor:
                for table_name, expected in (
                    ("property_search_runs", 0),
                    ("property_search_work_jobs", 0),
                    ("property_research_packet_run_memberships", 0),
                    ("property_research_packet_links", 1),
                    ("property_search_erasure_fences", 2),
                ):
                    cursor.execute(
                        sql.SQL("SELECT COUNT(*) FROM {}").format(
                            sql.Identifier(table_name)
                        )
                    )
                    assert cursor.fetchone() == (expected,)
                cursor.execute(
                    "SELECT principal_key, run_id FROM property_search_erasure_fences ORDER BY principal_key"
                )
                fences = tuple(cursor.fetchall())
                assert all(str(row[0]).startswith("hmac-sha256:") for row in fences)
                assert all(row[1] == "" for row in fences)
                assert all("owner@example.test" not in str(row) for row in fences)
                cursor.execute(
                    """
                    SELECT retention_state
                    FROM property_research_packet_links
                    WHERE principal_id = %s AND candidate_ref = 'erasure-ref-1'
                    """,
                    (aliases[0],),
                )
                assert cursor.fetchone() == ("legal_hold",)

            repository = object.__new__(PostgresPropertySearchWorkQueue)
            repository._backoff_seconds = lambda _attempt: 0

            def _queue_connect():  # type: ignore[no-untyped-def]
                return psycopg.connect(
                    database_url,
                    autocommit=False,
                    connect_timeout=5,
                    options=f"-csearch_path={schema_name}",
                )

            repository._connect = _queue_connect  # type: ignore[method-assign]
            late_account_record = _record(
                {
                    "candidate_ref": "late-account-fence-ref",
                    "property_url": "https://example.test/late-account-fence",
                }
            )
            late_account_record.update(
                {
                    "principal_id": aliases[0],
                    "run_id": "late-account-erasure-run",
                }
            )
            with pytest.raises(psycopg.Error) as late_account_queue_write:
                repository.enqueue_run(
                    run_record=late_account_record,
                    payload_json={"run_id": "late-account-erasure-run"},
                    idempotency_key="late-account-erasure-key",
                )
            assert late_account_queue_write.value.sqlstate == "23514"
            assert "property_search_account_erased" in str(
                late_account_queue_write.value
            )
            assert repository.heartbeat(
                job_id="erasure-job-1",
                lease_owner="late-worker",
                lease_seconds=30,
            ) is False
            assert repository.complete(
                job_id="erasure-job-1",
                lease_owner="late-worker",
            ) is None
            assert repository.fail(
                job_id="erasure-job-1",
                lease_owner="late-worker",
                error="late failure",
            ) is None
            assert repository.claim(lease_owner="late-worker", lease_seconds=30) is None
        finally:
            verify.close()
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )
        finally:
            admin.close()


@pytest.mark.skipif(
    os.environ.get("EA_RUN_PROPERTY_SEARCH_POSTGRES_INTEGRATION") != "1",
    reason="explicit isolated PostgreSQL integration lane only",
)
def test_postgres_erasure_fence_serializes_both_writer_orderings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("DATABASE_URL is required for the explicit integration lane")

    import psycopg
    from psycopg import sql

    schema_name = f"erasure_race_contract_{uuid.uuid4().hex}"
    monkeypatch.setenv(
        "PROPERTYQUARRY_PRIVACY_LOOKUP_SECRET",
        "postgres-erasure-race-test-secret",
    )
    admin = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        setup = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
        try:
            with setup.cursor() as cursor:
                cursor.execute(
                    sql.SQL("SET search_path TO {}").format(sql.Identifier(schema_name))
                )
                for migration in search_schema.PROPERTY_SEARCH_MIGRATIONS:
                    cursor.execute(migration.sql)
        finally:
            setup.close()

        def _connection(*, autocommit: bool = True):  # type: ignore[no-untyped-def]
            return psycopg.connect(
                database_url,
                autocommit=autocommit,
                connect_timeout=5,
                options=f"-csearch_path={schema_name}",
            )

        @contextlib.contextmanager
        def _isolated_connect() -> object:
            connection = _connection()
            try:
                yield connection
            finally:
                connection.close()

        monkeypatch.setattr(storage, "_property_search_run_database_url", lambda: database_url)
        monkeypatch.setattr(storage, "_require_property_search_run_schema", lambda: None)
        monkeypatch.setattr(storage, "_property_search_run_connect", _isolated_connect)

        writer_first_principal = "tenant-writer-first"
        writer_first_key = storage._property_search_principal_key(
            writer_first_principal
        )
        writer_connection = _connection(autocommit=False)
        with writer_connection.cursor() as cursor:
            storage._set_property_search_writer_contract(cursor)
            cursor.execute(
                """
                INSERT INTO property_search_runs (
                    principal_id, run_id, principal_key, payload_json,
                    created_at, updated_at
                )
                VALUES (%s, 'writer-first-run', %s, '{}'::jsonb, NOW(), NOW())
                """,
                (writer_first_principal, writer_first_key),
            )

        eraser_entered = threading.Event()
        original_record_fence = storage._record_property_search_erasure_fence

        def _observed_record_fence(*args: object, **kwargs: object) -> None:
            eraser_entered.set()
            original_record_fence(*args, **kwargs)

        monkeypatch.setattr(
            storage,
            "_record_property_search_erasure_fence",
            _observed_record_fence,
        )
        writer_first_results: list[dict[str, int]] = []
        writer_first_errors: list[BaseException] = []

        def _erase_after_writer() -> None:
            try:
                writer_first_results.append(
                    storage._erase_property_search_account_data(
                        principal_ids=(writer_first_principal,)
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                writer_first_errors.append(exc)

        erase_thread = threading.Thread(target=_erase_after_writer, daemon=True)
        erase_thread.start()
        assert eraser_entered.wait(timeout=5)
        writer_connection.commit()
        writer_connection.close()
        erase_thread.join(timeout=5)
        assert not erase_thread.is_alive()
        assert writer_first_errors == []
        assert writer_first_results == [
            {
                "runs_deleted": 1,
                "work_jobs_deleted": 0,
                "packet_links_deleted": 0,
                "packet_links_legal_hold_retained": 0,
            }
        ]

        eraser_first_principal = "tenant-eraser-first"
        eraser_first_key = storage._property_search_principal_key(
            eraser_first_principal
        )
        eraser_connection = _connection(autocommit=False)
        with eraser_connection.cursor() as cursor:
            storage._set_property_search_writer_contract(cursor)
            original_record_fence(
                cursor,
                principal_key=eraser_first_key,
            )

        writer_reached_contract = threading.Event()
        release_writer = threading.Event()
        original_set_contract = storage._set_property_search_writer_contract

        def _held_writer_contract(cursor: object) -> None:
            original_set_contract(cursor)
            writer_reached_contract.set()
            assert release_writer.wait(timeout=5)

        monkeypatch.setattr(storage, "_set_property_search_writer_contract", _held_writer_contract)
        eraser_first_results: list[bool] = []
        eraser_first_errors: list[BaseException] = []
        delayed_record = _record()
        delayed_record.update(
            {
                "principal_id": eraser_first_principal,
                "run_id": "eraser-first-run",
                "created_at": "2099-01-01T00:00:00+00:00",
                "updated_at": "2099-01-01T00:00:00+00:00",
            }
        )

        def _write_after_eraser() -> None:
            try:
                eraser_first_results.append(
                    storage._store_property_search_run_record(delayed_record)
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                eraser_first_errors.append(exc)

        writer_thread = threading.Thread(target=_write_after_eraser, daemon=True)
        writer_thread.start()
        assert writer_reached_contract.wait(timeout=5)
        eraser_connection.commit()
        eraser_connection.close()
        release_writer.set()
        writer_thread.join(timeout=5)
        assert not writer_thread.is_alive()
        assert eraser_first_errors == []
        assert eraser_first_results == [False]

        verify = _connection()
        try:
            with verify.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) FROM property_search_runs")
                assert cursor.fetchone() == (0,)
                cursor.execute("SELECT COUNT(*) FROM property_search_work_jobs")
                assert cursor.fetchone() == (0,)
                cursor.execute("SELECT COUNT(*) FROM property_search_erasure_fences")
                assert cursor.fetchone() == (2,)
        finally:
            verify.close()
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        sql.Identifier(schema_name)
                    )
                )
        finally:
            admin.close()
