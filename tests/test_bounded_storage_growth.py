from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.api.routes import landing_view_models
from app.product import property_search_schema, property_search_storage
from app.repositories import artifacts_postgres, observation_postgres


ROOT = Path(__file__).resolve().parents[1]


def _large_run(*, status: str) -> dict[str, object]:
    return {
        "run_id": "bounded-run",
        "principal_id": "bounded-principal",
        "status": status,
        "created_at": "2026-08-02T00:00:00+00:00",
        "updated_at": "2026-08-02T00:01:00+00:00",
        "summary": {
            "status": status,
            "ranked_candidates": [
                {
                    "candidate_ref": f"candidate-{index}",
                    "title": "Home " + ("x" * 2000),
                    "property_url": f"https://example.test/{index}",
                }
                for index in range(100)
            ],
            "sources": [
                {
                    "source_label": f"source-{index}",
                    "source_html": "<html>" + ("y" * 100_000) + "</html>",
                }
                for index in range(10)
            ],
        },
    }


def test_search_run_payload_is_byte_bounded_and_terminal_rows_compact_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_MAX_PAYLOAD_BYTES", "65536")

    active = property_search_storage._bounded_property_search_run_payload(  # type: ignore[attr-defined]
        _large_run(status="in_progress")
    )
    terminal = property_search_storage._bounded_property_search_run_payload(  # type: ignore[attr-defined]
        _large_run(status="completed")
    )

    assert len(property_search_storage._property_search_json_bytes(active)) <= 65536  # type: ignore[attr-defined]
    assert len(property_search_storage._property_search_json_bytes(terminal)) <= 65536  # type: ignore[attr-defined]
    assert active["payload_retention_status"] == "bounded_projection"
    assert terminal["payload_retention_status"] == "compact_only"
    assert "sources" not in dict(terminal.get("summary") or {})
    assert int(active["payload_original_bytes"]) > 65536
    assert len(str(active["payload_original_sha256"])) == 64

    pathological = _large_run(status="in_progress")
    pathological["summary"] = {"operator_note": "z" * 1_000_000}
    pathological_bounded = property_search_storage._bounded_property_search_run_payload(  # type: ignore[attr-defined]
        pathological
    )
    assert len(property_search_storage._property_search_json_bytes(pathological_bounded)) <= 65536  # type: ignore[attr-defined]


def test_observation_payload_requires_external_pointer_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_OBSERVATION_MAX_PAYLOAD_BYTES", "16384")
    payload = {"body": "x" * 20_000}

    with pytest.raises(
        ValueError,
        match="observation_payload_too_large_raw_payload_uri_required",
    ):
        observation_postgres._bounded_observation_payload(  # type: ignore[attr-defined]
            payload,
            raw_payload_uri="",
        )

    bounded = observation_postgres._bounded_observation_payload(  # type: ignore[attr-defined]
        payload,
        raw_payload_uri="s3://propertyquarry-observations/raw-1.json",
    )
    assert bounded["payload_retention_status"] == "external_pointer_only"
    assert bounded["payload_original_bytes"] > 16384
    assert bounded["raw_payload_uri"] == "s3://propertyquarry-observations/raw-1.json"
    assert len(str(bounded["payload_sha256"])) == 64


def test_principal_quota_deletes_only_terminal_compact_nonheld_rows_in_a_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_MAX_ROWS_PER_PRINCIPAL", "500")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RETENTION_BATCH_SIZE", "10")
    refreshed: dict[str, object] = {}

    class Cursor:
        rowcount = 0

        def __init__(self) -> None:
            self.queries: list[str] = []
            self.fetchone_rows = [(501,), (1,)]
            self.fetchall_rows = [[("old-run",)], [("candidate-ref",)]]

        def execute(self, query: str, _params: object = ()) -> None:
            self.queries.append(str(query))
            if str(query).strip().startswith("DELETE FROM property_search_runs"):
                self.rowcount = 1

        def fetchone(self):  # type: ignore[no-untyped-def]
            return self.fetchone_rows.pop(0)

        def fetchall(self):  # type: ignore[no-untyped-def]
            return self.fetchall_rows.pop(0)

    cursor = Cursor()
    monkeypatch.setattr(
        property_search_storage,
        "refresh_property_research_packet_links_for_refs",
        lambda _cursor, **kwargs: refreshed.update(kwargs),
    )

    result = property_search_storage._enforce_property_search_principal_run_quota(  # type: ignore[attr-defined]
        cursor,
        principal_id="bounded-principal",
    )

    sql = "\n".join(cursor.queries)
    assert result == {"compacted": 1, "deleted": 1}
    assert "links.retention_state = 'legal_hold'" in sql
    assert "payload_retention_status', '') = 'compact_only'" in sql
    assert "FOR UPDATE OF runs SKIP LOCKED" in sql
    assert refreshed == {
        "principal_id": "bounded-principal",
        "candidate_refs": ("candidate-ref",),
    }


def test_principal_quota_backpressures_when_every_candidate_is_held_or_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_MAX_ROWS_PER_PRINCIPAL", "500")

    class Cursor:
        rowcount = 0

        def __init__(self) -> None:
            self.fetchone_rows = [(501,), (0,)]

        def execute(self, _query: str, _params: object = ()) -> None:
            return None

        def fetchone(self):  # type: ignore[no-untyped-def]
            return self.fetchone_rows.pop(0)

        def fetchall(self):  # type: ignore[no-untyped-def]
            return []

    with pytest.raises(
        RuntimeError,
        match="property_search_run_quota_exceeded_legal_hold_or_active",
    ):
        property_search_storage._enforce_property_search_principal_run_quota(  # type: ignore[attr-defined]
            Cursor(),
            principal_id="held-principal",
        )


def test_schema_v19_and_operator_lane_are_journaled_batched_and_scheduled() -> None:
    migration = property_search_schema.PROPERTY_SEARCH_MIGRATIONS[18]
    migration_sql = " ".join(migration.sql.split())
    retention_script = (ROOT / "scripts/db_retention.sh").read_text(encoding="utf-8")
    size_script = (ROOT / "scripts/db_size.sh").read_text(encoding="utf-8")
    storage_script = (ROOT / "scripts/storage_size.sh").read_text(encoding="utf-8")
    units = ROOT / "packaging/propertyquarry-storage-maintenance/systemd"

    assert migration.version == 19
    assert migration.name == "bounded_storage_retention_control"
    assert "CREATE TABLE IF NOT EXISTS propertyquarry_retention_runs" in migration_sql
    assert "idx_propertyquarry_retention_single_running" in migration_sql
    assert "WHERE status = 'running'" in migration_sql
    assert "CHECK (pg_column_size(policy_json) <= 65536)" in migration_sql
    assert "FOR UPDATE SKIP LOCKED" in retention_script
    assert "EA_RETENTION_MAX_ROWS_PER_TABLE" in retention_script
    assert "propertyquarry_retention_runs" in retention_script
    assert "retention table is not allowlisted" in retention_script
    assert "EA_RETENTION_WORKSPACE_ACCESS_SESSIONS_DAYS" in retention_script
    assert "workspace_access_sessions" in retention_script
    assert "status IN ('revoked','expired')" in retention_script
    assert "CASE WHEN expires_at ~" in retention_script
    assert 'if [[ "${running}" != "true" ]]' in retention_script
    assert "pg_ls_waldir" in size_script
    assert "toast_bytes" in size_script
    assert "high_water_filesystem" in size_script
    assert "Docker host summary (shared host; diagnostic only)" in storage_script
    assert (units / "propertyquarry-db-retention.timer").is_file()
    assert (units / "propertyquarry-storage-high-water.timer").is_file()


def test_storage_policy_health_projection_exposes_all_enforced_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_MAX_PAYLOAD_BYTES", "131072")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_MAX_ROWS_PER_PRINCIPAL", "321")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RETENTION_BATCH_SIZE", "17")

    policy = property_search_storage.property_search_run_retention_policy()

    assert policy["property_search_run_max_payload_bytes"] == "131072"
    assert policy["property_search_run_max_rows_per_principal"] == "321"
    assert policy["property_search_retention_batch_size"] == "17"
    assert policy["property_search_legal_hold_policy"] == "preserve_and_backpressure"


def test_map_preview_cache_prunes_oldest_files_by_entry_and_byte_limits(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_PROPERTY_MAP_PREVIEW_CACHE_MAX_ENTRIES", "2")
    monkeypatch.setenv("EA_PROPERTY_MAP_PREVIEW_CACHE_MAX_BYTES", "9")
    paths = [tmp_path / f"preview-{index}.png" for index in range(3)]
    for index, path in enumerate(paths):
        path.write_bytes(b"x" * 5)
        os.utime(path, ns=(index + 1, index + 1))
    ignored = tmp_path / "not-a-preview.tmp"
    ignored.write_bytes(b"keep")

    result = landing_view_models._prune_map_preview_cache(  # type: ignore[attr-defined]
        tmp_path,
        force=True,
        now_monotonic=100.0,
    )

    assert result == {"entries": 1, "bytes": 5, "deleted": 2}
    assert not paths[0].exists()
    assert not paths[1].exists()
    assert paths[2].exists()
    assert ignored.exists()


def test_artifact_admission_backpressures_oversized_and_low_water_growth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_ARTIFACT_MAX_BYTES", "8")
    monkeypatch.setenv("EA_ARTIFACTS_MIN_FREE_BYTES", "100")

    with pytest.raises(ValueError, match="artifact_payload_too_large:9>8"):
        artifacts_postgres._validate_artifact_storage_admission(  # type: ignore[attr-defined]
            tmp_path,
            b"123456789",
        )

    class Filesystem:
        f_bavail = 10
        f_frsize = 10

    monkeypatch.setattr(artifacts_postgres.os, "statvfs", lambda _path: Filesystem())
    with pytest.raises(RuntimeError, match="artifact_storage_low_water"):
        artifacts_postgres._validate_artifact_storage_admission(  # type: ignore[attr-defined]
            tmp_path,
            b"12345678",
        )

    # Shrinking an existing durable artifact remains possible below low water.
    artifacts_postgres._validate_artifact_storage_admission(  # type: ignore[attr-defined]
        tmp_path,
        b"1234",
        existing_size_bytes=8,
    )


def test_postgres_principal_quota_preserves_legal_hold_and_evicts_next_oldest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = str(os.environ.get("EA_TEST_PROPERTY_DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("EA_TEST_PROPERTY_DATABASE_URL is not set")
    import psycopg

    property_search_schema.migrate_property_search_schema(
        database_url,
        applied_by="bounded-storage-quota-test",
    )
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RUN_MAX_ROWS_PER_PRINCIPAL", "10")
    monkeypatch.setenv("EA_PROPERTY_SEARCH_RETENTION_BATCH_SIZE", "3")
    principal_id = f"bounded-quota-{uuid4().hex}"

    def store(index: int) -> None:
        assert property_search_storage._store_property_search_run_record(  # type: ignore[attr-defined]
            {
                "run_id": f"run-{index:02d}",
                "principal_id": principal_id,
                "status": "completed",
                "created_at": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                "updated_at": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                "summary": {
                    "status": "completed",
                    "ranked_candidates": [
                        {
                            "candidate_ref": f"candidate-{index:02d}",
                            "property_url": f"https://example.test/{index}",
                        }
                    ],
                },
            }
        )

    try:
        for index in range(10):
            store(index)
        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    property_search_storage._set_property_search_writer_contract(cursor)  # type: ignore[attr-defined]
                    cursor.execute(
                        """
                        UPDATE property_research_packet_links
                        SET retention_state = 'legal_hold'
                        WHERE principal_id = %s AND candidate_ref = 'candidate-00'
                        """,
                        (principal_id,),
                    )

        store(10)

        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT run_id FROM property_search_runs WHERE principal_id = %s ORDER BY run_id",
                    (principal_id,),
                )
                run_ids = [str(row[0]) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT retention_state
                    FROM property_research_packet_links
                    WHERE principal_id = %s AND candidate_ref = 'candidate-00'
                    """,
                    (principal_id,),
                )
                hold_row = cursor.fetchone()

        assert len(run_ids) == 10
        assert "run-00" in run_ids
        assert "run-01" not in run_ids
        assert "run-10" in run_ids
        assert hold_row == ("legal_hold",)
    finally:
        with psycopg.connect(database_url) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    property_search_storage._set_property_search_writer_contract(cursor)  # type: ignore[attr-defined]
                    cursor.execute(
                        """
                        UPDATE property_research_packet_links
                        SET retention_state = 'active'
                        WHERE principal_id = %s
                        """,
                        (principal_id,),
                    )
        property_search_storage._erase_property_search_account_data(  # type: ignore[attr-defined]
            principal_id=principal_id,
        )
