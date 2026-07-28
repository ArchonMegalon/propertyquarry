from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

import pytest

from app.product.property_search_work_queue import (
    PostgresPropertySearchWorkQueue,
    PropertySearchWorkJob,
)
from app.product.property_search_storage import _property_search_principal_key


def _job_row(
    *,
    job_id: str,
    principal_id: str,
    run_id: str,
    status: str,
    lease_owner: str = "worker-1",
) -> tuple[object, ...]:
    now = datetime.now(timezone.utc)
    return (
        job_id,
        principal_id,
        run_id,
        f"key-{job_id}",
        {"job_id": job_id},
        status,
        1,
        3,
        now,
        lease_owner,
        now + timedelta(minutes=5) if status == "leased" else None,
        now,
        "",
        now,
        now,
        now if status in {"completed", "failed"} else None,
    )


class _SqlWorld:
    def __init__(self) -> None:
        self.events: list[tuple[str, tuple[object, ...]]] = []
        self.identities: dict[str, tuple[object, ...]] = {}
        self.candidate_batches: list[tuple[str, ...]] = []
        self.returning_rows: list[tuple[object, ...] | None] = []
        self.enqueue_job_row: tuple[object, ...] | None = None
        self.reject_authority = False


class _FakeCursor:
    def __init__(self, world: _SqlWorld) -> None:
        self.world = world
        self.rowcount = 0
        self._row: tuple[object, ...] | None = None
        self._rows: tuple[tuple[object, ...], ...] = ()

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(
        self,
        sql: str,
        params: tuple[object, ...] | None = None,
    ) -> None:
        normalized = " ".join(str(sql or "").split()).lower()
        normalized_params = tuple(params or ())
        self.world.events.append((normalized, normalized_params))
        self.rowcount = 0
        self._row = None
        self._rows = ()

        if (
            "property_search_assert_principal_write_allowed" in normalized
            or "property_search_assert_write_allowed" in normalized
        ):
            if self.world.reject_authority:
                raise RuntimeError("property_search_account_erased")
            self._row = (None,)
            return
        if normalized.startswith("select job_id from property_search_work_jobs"):
            batch = self.world.candidate_batches.pop(0) if self.world.candidate_batches else ()
            self._rows = tuple((job_id,) for job_id in batch)
            return
        if normalized.startswith("select principal_id, run_id, attempt_count, max_attempts"):
            self._row = self.world.identities.get(str(normalized_params[0] or ""))
            return
        if normalized.startswith("select principal_id, run_id from property_search_work_jobs"):
            identity = self.world.identities.get(str(normalized_params[0] or ""))
            self._row = tuple(identity[:2]) if identity is not None else None
            return
        if normalized.startswith("insert into property_search_runs"):
            self._row = None
            return
        if normalized.startswith("insert into property_search_work_jobs"):
            self._row = self.world.enqueue_job_row
            self.rowcount = 1 if self._row is not None else 0
            return
        if normalized.startswith("update property_search_work_jobs"):
            if "returning" in normalized:
                self._row = self.world.returning_rows.pop(0)
                self.rowcount = 1 if self._row is not None else 0
            else:
                self.rowcount = 1

    def fetchone(self) -> tuple[object, ...] | None:
        row = self._row
        self._row = None
        return row

    def fetchall(self) -> tuple[tuple[object, ...], ...]:
        rows = self._rows
        self._rows = ()
        return rows


class _FakeConnection:
    def __init__(self, world: _SqlWorld) -> None:
        self.world = world

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, exc_type: object, *_args: object) -> None:
        if exc_type is not None:
            self.world.events.append(("<rollback>", ()))
        return None

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.world)

    def commit(self) -> None:
        self.world.events.append(("<commit>", ()))


def _queue(world: _SqlWorld) -> PostgresPropertySearchWorkQueue:
    queue = object.__new__(PostgresPropertySearchWorkQueue)
    queue._database_url = "postgresql://fake"  # type: ignore[attr-defined]
    queue._backoff_seconds = lambda _attempt: 17  # type: ignore[attr-defined]
    queue._connect = lambda: _FakeConnection(world)  # type: ignore[method-assign]
    return queue


def _assert_update_transactions_are_authorized(
    events: list[tuple[str, tuple[object, ...]]],
) -> None:
    previous_boundary = -1
    for update_index, (sql, _params) in enumerate(events):
        if not sql.startswith("update property_search_work_jobs"):
            continue
        boundary = max(
            (index for index in range(update_index) if events[index][0] == "<commit>"),
            default=previous_boundary,
        )
        transaction_sql = [item[0] for item in events[boundary + 1 : update_index]]
        writer_index = next(
            index for index, statement in enumerate(transaction_sql) if "set_config" in statement
        )
        identity_index = next(
            index
            for index, statement in enumerate(transaction_sql)
            if statement.startswith("select principal_id, run_id")
        )
        authority_index = next(
            index
            for index, statement in enumerate(transaction_sql)
            if "property_search_assert_principal_write_allowed" in statement
        )
        assert writer_index < identity_index < authority_index
        assert all(not statement.startswith("update ") for statement in transaction_sql)
        previous_boundary = boundary


@pytest.mark.parametrize("mutation", ("heartbeat", "complete", "fail"))
def test_direct_job_mutations_lookup_identity_then_authorize_before_update(
    mutation: str,
) -> None:
    world = _SqlWorld()
    world.identities["job-1"] = ("principal-1", "run-1", 1, 3)
    returned_status = "completed" if mutation == "complete" else "queued"
    if mutation != "heartbeat":
        world.returning_rows.append(
            _job_row(
                job_id="job-1",
                principal_id="principal-1",
                run_id="run-1",
                status=returned_status,
                lease_owner="" if mutation == "complete" else "worker-1",
            )
        )
    queue = _queue(world)

    if mutation == "heartbeat":
        result = queue.heartbeat(job_id="job-1", lease_owner="worker-1", lease_seconds=60)
        assert result is True
    elif mutation == "complete":
        result = queue.complete(job_id="job-1", lease_owner="worker-1")
        assert isinstance(result, PropertySearchWorkJob)
        assert result.status == "completed"
    else:
        result = queue.fail(job_id="job-1", lease_owner="worker-1", error="retry")
        assert isinstance(result, PropertySearchWorkJob)
        assert result.status == "queued"

    _assert_update_transactions_are_authorized(world.events)
    statements = [event[0] for event in world.events]
    identity_statement = next(
        statement for statement in statements if statement.startswith("select principal_id, run_id")
    )
    assert "for update" not in identity_statement
    authority_event = next(
        event for event in world.events if "property_search_assert_principal_write_allowed" in event[0]
    )
    assert authority_event[1] == ("principal-1", "run-1")


def test_authority_rejection_prevents_direct_job_update() -> None:
    world = _SqlWorld()
    world.identities["job-erased"] = ("principal-erased", "run-erased")
    world.reject_authority = True
    queue = _queue(world)

    with pytest.raises(RuntimeError, match="property_search_account_erased"):
        queue.heartbeat(
            job_id="job-erased",
            lease_owner="worker-1",
            lease_seconds=60,
        )

    assert not any(
        statement.startswith("update property_search_work_jobs")
        for statement, _params in world.events
    )


def test_claim_authority_rejection_prevents_candidate_row_lock_or_update() -> None:
    world = _SqlWorld()
    world.candidate_batches = [(), ("job-erased",)]
    world.identities["job-erased"] = ("principal-erased", "run-erased")
    world.reject_authority = True
    queue = _queue(world)

    with pytest.raises(RuntimeError, match="property_search_account_erased"):
        queue.claim(lease_owner="worker-1", lease_seconds=60)

    statements = [event[0] for event in world.events]
    assert all("for update" not in statement for statement in statements)
    assert not any(
        statement.startswith("update property_search_work_jobs")
        for statement in statements
    )


def test_claim_cleanup_and_racing_candidates_authorize_before_conditional_updates() -> None:
    world = _SqlWorld()
    world.candidate_batches = [
        ("job-exhausted",),
        ("job-raced", "job-claimed"),
    ]
    world.identities.update(
        {
            "job-exhausted": ("principal-exhausted", "run-exhausted"),
            "job-raced": ("principal-raced", "run-raced"),
            "job-claimed": ("principal-claimed", "run-claimed"),
        }
    )
    # Another worker wins job-raced after our nonlocking scan; the fully
    # conditional update returns no row, so this worker tries the next bounded
    # candidate without duplicating the lease.
    world.returning_rows = [
        None,
        _job_row(
            job_id="job-claimed",
            principal_id="principal-claimed",
            run_id="run-claimed",
            status="leased",
        ),
    ]
    queue = _queue(world)

    claimed = queue.claim(lease_owner="worker-1", lease_seconds=90)

    assert isinstance(claimed, PropertySearchWorkJob)
    assert claimed.job_id == "job-claimed"
    statements = [event[0] for event in world.events]
    assert all("for update" not in statement for statement in statements)
    candidate_scans = [
        event
        for event in world.events
        if event[0].startswith("select job_id from property_search_work_jobs")
    ]
    assert len(candidate_scans) == 2
    assert all(event[1] == (queue._CLAIM_CANDIDATE_SCAN_LIMIT,) for event in candidate_scans)
    updates = [
        statement
        for statement in statements
        if statement.startswith("update property_search_work_jobs")
    ]
    assert len(updates) == 3
    assert "attempt_count >= max_attempts" in updates[0]
    assert all("principal_id = %s" in statement and "run_id = %s" in statement for statement in updates)
    _assert_update_transactions_are_authorized(world.events)


def test_enqueue_authorizes_principal_key_run_before_first_insert() -> None:
    world = _SqlWorld()
    world.enqueue_job_row = _job_row(
        job_id="job-enqueued",
        principal_id="principal-enqueue",
        run_id="run-enqueue",
        status="queued",
        lease_owner="",
    )
    queue = _queue(world)

    result = queue.enqueue_run(
        run_record={
            "principal_id": "principal-enqueue",
            "run_id": "run-enqueue",
            "status": "queued",
        },
        payload_json={"run_id": "run-enqueue"},
        idempotency_key="enqueue-key",
    )

    assert result.created is True
    statements = [event[0] for event in world.events]
    writer_index = next(index for index, sql in enumerate(statements) if "set_config" in sql)
    authority_index = next(
        index
        for index, sql in enumerate(statements)
        if "property_search_assert_write_allowed" in sql
    )
    first_insert_index = next(
        index for index, sql in enumerate(statements) if sql.startswith("insert into")
    )
    assert writer_index < authority_index < first_insert_index
    assert world.events[authority_index][1] == (
        _property_search_principal_key("principal-enqueue"),
        "run-enqueue",
    )
    assert not any(
        "property_search_assert_principal_write_allowed" in sql
        for sql in statements[:first_insert_index]
    )


def test_enqueue_base_authority_rejection_prevents_run_insert() -> None:
    world = _SqlWorld()
    world.reject_authority = True
    queue = _queue(world)

    with pytest.raises(RuntimeError, match="property_search_account_erased"):
        queue.enqueue_run(
            run_record={
                "principal_id": "principal-erased",
                "run_id": "new-run",
                "status": "queued",
            },
            payload_json={"run_id": "new-run"},
            idempotency_key="enqueue-erased-key",
        )

    assert not any(sql.startswith("insert into") for sql, _params in world.events)
