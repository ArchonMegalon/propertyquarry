from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.product.property_search_work_queue import (
    PostgresPropertySearchWorkQueue,
    property_search_work_idempotency_key,
)


@pytest.fixture(scope="module", autouse=True)
def _migrated_property_search_schema() -> None:
    database_url = _db_url()
    from app.product.property_search_schema import migrate_property_search_schema

    migrate_property_search_schema(
        database_url,
        applied_by="property-search-work-queue-contract",
    )


def _db_url() -> str:
    value = str(
        os.environ.get("EA_TEST_PROPERTY_DATABASE_URL")
        or os.environ.get("EA_TEST_DATABASE_URL")
        or ""
    ).strip()
    if not value:
        pytest.skip("EA_TEST_PROPERTY_DATABASE_URL or EA_TEST_DATABASE_URL is not set")
    return value


def _record(*, principal_id: str, run_id: str) -> dict[str, object]:
    return {
        "principal_id": principal_id,
        "run_id": run_id,
        "status": "queued",
        "selected_platforms": ["willhaben"],
        "property_search_preferences": {"country_code": "AT"},
        "summary": {},
        "events": [],
        "created_at": "2026-07-13T08:00:00+00:00",
        "updated_at": "2026-07-13T08:00:00+00:00",
    }


def _cleanup(database_url: str, *, principal_ids: tuple[str, ...]) -> None:
    import psycopg

    from app.product.property_search_storage import (
        _set_property_search_writer_contract,
    )

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            _set_property_search_writer_contract(cur)
            cur.execute(
                "DELETE FROM property_search_work_jobs WHERE principal_id = ANY(%s)",
                (list(principal_ids),),
            )
            cur.execute(
                "DELETE FROM property_search_runs WHERE principal_id = ANY(%s)",
                (list(principal_ids),),
            )


def test_postgres_enqueue_atomically_persists_run_and_unique_job() -> None:
    database_url = _db_url()
    repository = PostgresPropertySearchWorkQueue(database_url)
    suffix = uuid4().hex
    principal_id = f"queue-contract-{suffix}"
    first_run_id = f"run-{suffix}-a"
    duplicate_run_id = f"run-{suffix}-b"
    key = property_search_work_idempotency_key(
        principal_id=principal_id,
        run_id=first_run_id,
        requested_key=f"request-{suffix}",
    )
    try:
        first = repository.enqueue_run(
            run_record=_record(principal_id=principal_id, run_id=first_run_id),
            payload_json={"actor": "contract"},
            idempotency_key=key,
            max_attempts=3,
        )
        duplicate = repository.enqueue_run(
            run_record=_record(principal_id=principal_id, run_id=duplicate_run_id),
            payload_json={"actor": "duplicate"},
            idempotency_key=key,
            max_attempts=3,
        )

        assert first.created is True
        assert duplicate.created is False
        assert duplicate.job.job_id == first.job.job_id
        assert duplicate.job.run_id == first_run_id

        import psycopg

        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM property_search_runs WHERE principal_id = %s",
                    (principal_id,),
                )
                assert cur.fetchone()[0] == 1
                cur.execute(
                    "SELECT COUNT(*) FROM property_search_work_jobs WHERE principal_id = %s",
                    (principal_id,),
                )
                assert cur.fetchone()[0] == 1
    finally:
        _cleanup(database_url, principal_ids=(principal_id,))


def test_postgres_enqueue_rolls_back_run_when_job_payload_cannot_be_persisted() -> None:
    database_url = _db_url()
    repository = PostgresPropertySearchWorkQueue(database_url)
    suffix = uuid4().hex
    principal_id = f"queue-rollback-{suffix}"
    run_id = f"run-{suffix}"
    try:
        with pytest.raises(Exception):
            repository.enqueue_run(
                run_record=_record(principal_id=principal_id, run_id=run_id),
                payload_json={"not_json": object()},
                idempotency_key=f"key-{suffix}",
                max_attempts=3,
            )

        import psycopg

        with psycopg.connect(database_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM property_search_runs WHERE principal_id = %s AND run_id = %s",
                    (principal_id, run_id),
                )
                assert cur.fetchone()[0] == 0
                cur.execute(
                    "SELECT COUNT(*) FROM property_search_work_jobs WHERE principal_id = %s AND run_id = %s",
                    (principal_id, run_id),
                )
                assert cur.fetchone()[0] == 0
    finally:
        _cleanup(database_url, principal_ids=(principal_id,))


def test_postgres_claim_skips_locked_row_and_preserves_unique_lease() -> None:
    database_url = _db_url()
    repository = PostgresPropertySearchWorkQueue(database_url)
    suffix = uuid4().hex
    principals = (f"queue-lock-a-{suffix}", f"queue-lock-b-{suffix}")
    jobs = []
    try:
        for index, principal_id in enumerate(principals):
            run_id = f"run-{suffix}-{index}"
            jobs.append(
                repository.enqueue_run(
                    run_record=_record(principal_id=principal_id, run_id=run_id),
                    payload_json={},
                    idempotency_key=f"key-{suffix}-{index}",
                    max_attempts=3,
                ).job
            )

        import psycopg

        first_job = sorted(jobs, key=lambda item: (item.available_at, item.created_at, item.job_id))[0]
        with psycopg.connect(database_url, autocommit=False) as locked_conn:
            with locked_conn.cursor() as locked_cur:
                locked_cur.execute(
                    "SELECT job_id FROM property_search_work_jobs WHERE job_id = %s FOR UPDATE",
                    (first_job.job_id,),
                )
                claimed = repository.claim(lease_owner="worker-b", lease_seconds=60)
                assert claimed is not None
                assert claimed.job_id != first_job.job_id
                assert repository.claim(lease_owner="worker-c", lease_seconds=60) is None
                assert repository.complete(job_id=claimed.job_id, lease_owner="worker-c") is None
                completed = repository.complete(job_id=claimed.job_id, lease_owner="worker-b")
                assert completed is not None
                assert completed.status == "completed"
    finally:
        _cleanup(database_url, principal_ids=principals)


def test_postgres_retry_budget_requeues_then_fails_terminally() -> None:
    database_url = _db_url()
    repository = PostgresPropertySearchWorkQueue(database_url, backoff_seconds=lambda _attempt: 0)
    suffix = uuid4().hex
    principal_id = f"queue-retry-{suffix}"
    run_id = f"run-{suffix}"
    try:
        repository.enqueue_run(
            run_record=_record(principal_id=principal_id, run_id=run_id),
            payload_json={},
            idempotency_key=f"key-{suffix}",
            max_attempts=2,
        )
        first = repository.claim(lease_owner="worker-a", lease_seconds=60)
        assert first is not None
        retry = repository.fail(job_id=first.job_id, lease_owner="worker-a", error="transient")
        assert retry is not None
        assert retry.status == "queued"

        second = repository.claim(lease_owner="worker-b", lease_seconds=60)
        assert second is not None
        assert second.job_id == first.job_id
        assert second.attempt_count == 2
        terminal = repository.fail(job_id=second.job_id, lease_owner="worker-b", error="permanent")
        assert terminal is not None
        assert terminal.status == "failed"
        assert terminal.completed_at is not None
        assert repository.claim(lease_owner="worker-c", lease_seconds=60) is None
    finally:
        _cleanup(database_url, principal_ids=(principal_id,))


def test_postgres_observability_snapshot_tracks_active_jobs_without_identity() -> None:
    database_url = _db_url()
    repository = PostgresPropertySearchWorkQueue(database_url)
    suffix = uuid4().hex
    principals = (
        f"queue-observability-a-{suffix}",
        f"queue-observability-b-{suffix}",
    )
    baseline = repository.observability_snapshot()
    try:
        for index, principal_id in enumerate(principals):
            repository.enqueue_run(
                run_record=_record(
                    principal_id=principal_id,
                    run_id=f"run-observability-{suffix}-{index}",
                ),
                payload_json={"private_marker": f"must-not-escape-{index}"},
                idempotency_key=f"queue-observability-{suffix}-{index}",
                max_attempts=1,
            )

        queued = repository.observability_snapshot()

        assert queued.depth == baseline.depth + 2
        assert queued.oldest_item_age_seconds >= 0.0
        assert set(vars(queued)) == {
            "depth",
            "oldest_item_age_seconds",
        }
    finally:
        _cleanup(database_url, principal_ids=principals)
