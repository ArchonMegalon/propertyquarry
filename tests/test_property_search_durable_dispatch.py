from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
import threading
from types import SimpleNamespace

import pytest

from app.product import service as product_service
from app.product import property_research_packet_links as packet_index
from app.product import property_search_work_queue as queue_module
from app.product.property_search_work_queue import (
    PostgresPropertySearchWorkQueue,
    PropertySearchWorkEnqueueResult,
    PropertySearchWorkJob,
    PropertySearchWorkQueueSnapshot,
)
from app.product.service import ProductService


def _job(
    *,
    run_id: str,
    principal_id: str,
    attempt_count: int = 0,
    max_attempts: int = 3,
    status: str = "queued",
) -> PropertySearchWorkJob:
    now = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)
    return PropertySearchWorkJob(
        job_id="job-1",
        principal_id=principal_id,
        run_id=run_id,
        idempotency_key="queue-key",
        payload_json={"actor": "api-actor", "force_refresh": False},
        status=status,
        attempt_count=attempt_count,
        max_attempts=max_attempts,
        available_at=now,
        created_at=now,
        updated_at=now,
    )


def _bare_service(monkeypatch: pytest.MonkeyPatch) -> ProductService:
    service = object.__new__(ProductService)
    monkeypatch.setattr(ProductService, "_open_property_market_bootstrap", lambda self, **_kwargs: None)
    monkeypatch.setattr(
        ProductService,
        "_resolve_property_search_run_preferences",
        lambda self, **kwargs: (
            tuple(kwargs.get("selected_platforms") or ()),
            dict(kwargs.get("property_preferences") or {}),
            kwargs.get("max_results_per_source"),
        ),
    )
    monkeypatch.setattr(ProductService, "_best_effort_propertyquarry_teable_sync", lambda self, **_kwargs: None)
    monkeypatch.setattr(product_service, "enforce_property_plan_limits", lambda **_kwargs: None)
    monkeypatch.setattr(product_service, "_prune_property_search_runs", lambda: None)
    return service


def _queue_job_row(*, run_id: str, principal_id: str) -> tuple[object, ...]:
    now = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
    return (
        "job-db-1",
        principal_id,
        run_id,
        "queue-key",
        {"run_id": run_id},
        "queued",
        0,
        3,
        now,
        None,
        None,
        None,
        "",
        now,
        now,
        None,
    )


class _QueueCursor:
    def __init__(self, connection: "_QueueConnection") -> None:
        self.connection = connection
        self.rowcount = 0
        self.row: tuple[object, ...] | None = None

    def __enter__(self) -> "_QueueCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: object = None) -> None:
        normalized = " ".join(sql.split())
        self.connection.executed.append(normalized)
        self.row = None
        self.rowcount = 0
        values = tuple(params or ())
        if "INSERT INTO property_search_runs" in normalized:
            principal_id, run_id = str(values[0]), str(values[1])
            self.connection.state["runs"][(principal_id, run_id)] = dict(
                values[3].obj
            )
            self.row = (run_id,)
            self.rowcount = 1
        elif "INSERT INTO property_search_work_jobs" in normalized:
            run_id = str(values[2])
            principal_id = str(values[1])
            if self.connection.conflicting_job:
                self.row = None
            else:
                self.row = _queue_job_row(run_id=run_id, principal_id=principal_id)
                self.connection.state["jobs"][run_id] = self.row
                self.rowcount = 1
        elif "DELETE FROM property_search_runs" in normalized:
            principal_id, run_id = str(values[0]), str(values[1])
            deleted = self.connection.state["runs"].pop(
                (principal_id, run_id), None
            )
            self.connection.state["memberships"].pop(run_id, None)
            self.rowcount = int(deleted is not None)
        elif "FROM property_search_work_jobs" in normalized:
            self.row = _queue_job_row(
                run_id="run-existing", principal_id="tenant-queue"
            )

    def fetchone(self) -> tuple[object, ...] | None:
        row = self.row
        self.row = None
        return row


class _QueueConnection:
    def __init__(
        self,
        state: dict[str, object],
        *,
        conflicting_job: bool = False,
    ) -> None:
        self.state = state
        self.snapshot = deepcopy(state)
        self.conflicting_job = conflicting_job
        self.executed: list[str] = []
        self.committed = False

    def __enter__(self) -> "_QueueConnection":
        return self

    def __exit__(self, exc_type: object, *_args: object) -> None:
        if exc_type is not None or not self.committed:
            self.state.clear()
            self.state.update(self.snapshot)

    def cursor(self) -> _QueueCursor:
        return _QueueCursor(self)

    def commit(self) -> None:
        self.committed = True


def _queue_state() -> dict[str, object]:
    return {"runs": {}, "links": {}, "memberships": {}, "jobs": {}}


def _candidate_queue_record(run_id: str = "run-queue") -> dict[str, object]:
    return {
        "principal_id": "tenant-queue",
        "run_id": run_id,
        "created_at": "2026-07-18T08:00:00+00:00",
        "updated_at": "2026-07-18T08:00:00+00:00",
        "status": "queued",
        "summary": {
            "ranked_candidates": [
                {
                    "candidate_ref": "candidate-queue",
                    "property_url": "https://example.test/candidate-queue",
                }
            ]
        },
    }


def test_postgres_queue_insert_atomically_dual_writes_packet_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _queue_state()
    connection = _QueueConnection(state)
    repository = object.__new__(PostgresPropertySearchWorkQueue)
    repository._connect = lambda: connection  # type: ignore[method-assign]

    def _upsert(_cursor, links):  # type: ignore[no-untyped-def]
        for link in links:
            state["links"][str(link["candidate_ref"])] = dict(link)
        return len(links)

    def _sync(_cursor, *, run_id, links, **_kwargs):  # type: ignore[no-untyped-def]
        assert all(str(link["candidate_ref"]) in state["links"] for link in links)
        state["memberships"][run_id] = {
            str(link["candidate_ref"]) for link in links
        }
        return len(links)

    monkeypatch.setattr(packet_index, "upsert_property_research_packet_links", _upsert)
    monkeypatch.setattr(packet_index, "sync_property_research_packet_run_memberships", _sync)

    result = repository.enqueue_run(
        run_record=_candidate_queue_record(),
        payload_json={"run_id": "run-queue"},
        idempotency_key="queue-key",
    )

    assert result.created is True
    assert ("tenant-queue", "run-queue") in state["runs"]
    assert "candidate-queue" in state["links"]
    assert state["memberships"]["run-queue"] == {"candidate-queue"}
    assert "run-queue" in state["jobs"]


def test_postgres_queue_packet_failure_rolls_back_run_and_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _queue_state()
    connection = _QueueConnection(state)
    repository = object.__new__(PostgresPropertySearchWorkQueue)
    repository._connect = lambda: connection  # type: ignore[method-assign]
    monkeypatch.setattr(
        packet_index,
        "upsert_property_research_packet_links",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("packet_index_write_failed")
        ),
    )

    with pytest.raises(RuntimeError, match="packet_index_write_failed"):
        repository.enqueue_run(
            run_record=_candidate_queue_record(),
            payload_json={"run_id": "run-queue"},
            idempotency_key="queue-key",
        )

    assert state == _queue_state()


def test_postgres_queue_zero_projection_still_syncs_exact_empty_membership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _queue_state()
    connection = _QueueConnection(state)
    repository = object.__new__(PostgresPropertySearchWorkQueue)
    repository._connect = lambda: connection  # type: ignore[method-assign]
    observed: list[tuple[str, int]] = []
    monkeypatch.setattr(
        packet_index,
        "upsert_property_research_packet_links",
        lambda _cursor, links: observed.append(("upsert", len(tuple(links)))) or 0,
    )
    monkeypatch.setattr(
        packet_index,
        "sync_property_research_packet_run_memberships",
        lambda _cursor, *, run_id, links, **_kwargs: observed.append(
            ("sync", len(tuple(links)))
        )
        or state["memberships"].update({run_id: set()})
        or 0,
    )
    record = _candidate_queue_record("run-empty")
    record["summary"] = {"ranked_candidates": []}

    repository.enqueue_run(
        run_record=record,
        payload_json={"run_id": "run-empty"},
        idempotency_key="queue-key",
    )

    assert observed == [("upsert", 0), ("sync", 0)]
    assert state["memberships"]["run-empty"] == set()


def test_postgres_queue_conflict_cleanup_reselects_affected_packet_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _queue_state()
    connection = _QueueConnection(state, conflicting_job=True)
    repository = object.__new__(PostgresPropertySearchWorkQueue)
    repository._connect = lambda: connection  # type: ignore[method-assign]
    monkeypatch.setattr(
        packet_index,
        "upsert_property_research_packet_links",
        lambda _cursor, links: state["links"].update(
            {str(link["candidate_ref"]): dict(link) for link in links}
        )
        or len(links),
    )
    monkeypatch.setattr(
        packet_index,
        "sync_property_research_packet_run_memberships",
        lambda _cursor, *, run_id, links, **_kwargs: state["memberships"].update(
            {run_id: {str(link["candidate_ref"]) for link in links}}
        )
        or len(links),
    )
    refreshed: list[tuple[str, ...]] = []

    def _refresh(_cursor, *, candidate_refs, **_kwargs):  # type: ignore[no-untyped-def]
        refs = tuple(candidate_refs)
        refreshed.append(refs)
        for candidate_ref in refs:
            state["links"].pop(candidate_ref, None)
        return len(refs)

    monkeypatch.setattr(
        packet_index,
        "refresh_property_research_packet_links_for_refs",
        _refresh,
    )

    result = repository.enqueue_run(
        run_record=_candidate_queue_record(),
        payload_json={"run_id": "run-queue"},
        idempotency_key="queue-key",
    )

    assert result.created is False
    assert ("tenant-queue", "run-queue") not in state["runs"]
    assert "run-queue" not in state["memberships"]
    assert "candidate-queue" not in state["links"]
    assert refreshed == [("candidate-queue",)]


def test_prod_start_atomically_enqueues_before_return_and_never_starts_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _bare_service(monkeypatch)
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    captured: dict[str, object] = {}

    class _Repository:
        def enqueue_run(self, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            record = dict(kwargs["run_record"])
            return PropertySearchWorkEnqueueResult(
                job=_job(run_id=str(record["run_id"]), principal_id=str(record["principal_id"])),
                created=True,
            )

    monkeypatch.setattr(product_service, "_property_search_work_queue_repository", lambda: _Repository())

    class _ForbiddenThread:
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("production start must not create an execution thread")

    monkeypatch.setattr(product_service.threading, "Thread", _ForbiddenThread)

    result = service.start_property_search_run(
        principal_id="principal-prod",
        actor="api-actor",
        selected_platforms=("willhaben",),
        property_search_preferences={"country_code": "AT"},
        dispatch_only=True,
        idempotency_key="client-request-1",
    )

    assert captured
    assert dict(captured["run_record"])["run_id"] == result["run_id"]
    assert dict(captured["payload_json"])["run_id"] == result["run_id"]
    assert result["status"] == "queued"
    assert result["summary"]["durable_queue"] is True
    assert result["summary"]["worker_start_mode"] == "durable_queue"
    assert result["summary"]["worker_started"] is False


def test_prod_start_fails_closed_and_removes_undurable_registry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _bare_service(monkeypatch)
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    before = set(product_service._PROPERTY_SEARCH_RUN_REGISTRY)

    class _Repository:
        def enqueue_run(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("postgres unavailable")

    monkeypatch.setattr(product_service, "_property_search_work_queue_repository", lambda: _Repository())

    with pytest.raises(RuntimeError, match="property_search_work_enqueue_failed"):
        service.start_property_search_run(
            principal_id="principal-prod-failure",
            actor="api-actor",
            selected_platforms=("willhaben",),
            property_search_preferences={"country_code": "AT"},
            dispatch_only=True,
        )

    assert set(product_service._PROPERTY_SEARCH_RUN_REGISTRY) == before


def test_durable_job_execution_is_synchronous_and_retries_only_failed_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProductService)
    job = _job(
        run_id="run-retry",
        principal_id="principal-retry",
        attempt_count=2,
        max_attempts=3,
        status="leased",
    )
    monkeypatch.setattr(
        product_service,
        "_load_property_search_run_record",
        lambda **_kwargs: {
            "run_id": job.run_id,
            "principal_id": job.principal_id,
            "status": "failed",
        },
    )
    captured: dict[str, object] = {}

    def _pickup(self, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return {"status": "completed", "run_id": job.run_id}

    monkeypatch.setattr(ProductService, "_pick_up_property_search_run_execution", _pickup)

    result = service.execute_property_search_work_job(job)

    assert result["status"] == "completed"
    assert captured["synchronous"] is True
    assert captured["allow_failed_retry"] is True
    assert captured["terminal_on_failure"] is False
    assert captured["force_refresh"] is False


def test_prod_stale_recovery_requeues_instead_of_starting_a_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProductService)
    monkeypatch.setenv("EA_RUNTIME_MODE", "prod")
    record = {
        "run_id": "run-stale",
        "principal_id": "principal-stale",
        "status": "in_progress",
        "created_at": "2026-07-13T08:00:00+00:00",
        "updated_at": "2026-07-13T08:00:00+00:00",
    }

    class _Repository:
        def enqueue_run(self, **kwargs):  # type: ignore[no-untyped-def]
            assert kwargs["run_record"] == record
            return PropertySearchWorkEnqueueResult(
                job=_job(run_id="run-stale", principal_id="principal-stale"),
                created=True,
            )

    monkeypatch.setattr(product_service, "_property_search_work_queue_repository", lambda: _Repository())

    class _ForbiddenThread:
        def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("production recovery must not create an execution thread")

    monkeypatch.setattr(product_service.threading, "Thread", _ForbiddenThread)

    result = service._pick_up_property_search_run_execution(
        record=record,
        actor="scheduler",
        reason="startup_checkpoint_stale",
    )

    assert result["status"] == "queued"
    assert result["durable_queue"] is True
    assert result["job_created"] is True


def test_worker_role_processes_property_job_on_main_execution_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import runner
    from app.product import property_search_work_queue as queue_module

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused-in-unit-test")
    job = _job(
        run_id="run-worker",
        principal_id="principal-worker",
        attempt_count=1,
        status="leased",
    )
    calls: list[tuple[str, str]] = []

    class _Repository:
        def __init__(self, _database_url: str) -> None:
            pass

        def claim(self, *, lease_owner: str, lease_seconds: int):  # type: ignore[no-untyped-def]
            calls.append(("claim", lease_owner))
            return job

        def heartbeat(self, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
            calls.append(("heartbeat", ""))
            return True

        def observability_snapshot(self) -> PropertySearchWorkQueueSnapshot:
            return PropertySearchWorkQueueSnapshot(
                depth=1,
                oldest_item_age_seconds=0.0,
            )

        def complete(self, *, job_id: str, lease_owner: str):  # type: ignore[no-untyped-def]
            calls.append(("complete", lease_owner))
            return _job(
                run_id=job.run_id,
                principal_id=job.principal_id,
                attempt_count=1,
                status="completed",
            )

        def fail(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("success path must not fail the job")

    execution_threads: list[threading.Thread] = []

    class _Service:
        def execute_property_search_work_job(self, claimed_job):  # type: ignore[no-untyped-def]
            assert claimed_job is job
            execution_threads.append(threading.current_thread())
            return {"status": "completed"}

    monkeypatch.setattr(queue_module, "PostgresPropertySearchWorkQueue", _Repository)
    monkeypatch.setattr(product_service, "build_product_service", lambda _container: _Service())

    result = runner._run_property_search_work_once(
        SimpleNamespace(),
        role="worker",
        log=logging.getLogger("test.property-search-worker"),
    )

    assert result["completed"] is True
    assert result["status"] == "completed"
    assert execution_threads == [threading.current_thread()]
    assert [name for name, _value in calls] == ["claim", "complete"]


def test_worker_role_refreshes_role_heartbeat_while_durable_job_is_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import runner
    from app.product import property_search_work_queue as queue_module

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused-in-unit-test")
    monkeypatch.setattr(
        queue_module,
        "property_search_work_heartbeat_seconds",
        lambda: 0.01,
    )
    job = _job(
        run_id="run-worker-long",
        principal_id="principal-worker-long",
        attempt_count=1,
        status="leased",
    )
    execution_started = threading.Event()
    release_execution = threading.Event()
    role_heartbeat_seen = threading.Event()
    calls: list[str] = []

    class _Repository:
        def __init__(self, _database_url: str) -> None:
            pass

        def claim(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append("claim")
            return job

        def heartbeat(self, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
            calls.append("lease_heartbeat")
            return True

        def observability_snapshot(self) -> PropertySearchWorkQueueSnapshot:
            calls.append("queue_snapshot")
            return PropertySearchWorkQueueSnapshot(
                depth=1,
                oldest_item_age_seconds=0.0,
            )

        def complete(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append("complete")
            return _job(
                run_id=job.run_id,
                principal_id=job.principal_id,
                attempt_count=1,
                status="completed",
            )

        def fail(self, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("success path must not fail the job")

    class _Service:
        def execute_property_search_work_job(self, claimed_job):  # type: ignore[no-untyped-def]
            assert claimed_job is job
            execution_started.set()
            if not release_execution.wait(timeout=2.0):
                raise AssertionError("test did not release durable job execution")
            return {"status": "completed"}

    def _write_role_heartbeat(*, role: str, status: str) -> None:
        assert role == "worker"
        assert status == "loop"
        calls.append("role_heartbeat")
        if "lease_heartbeat" in calls:
            role_heartbeat_seen.set()

    monkeypatch.setattr(queue_module, "PostgresPropertySearchWorkQueue", _Repository)
    monkeypatch.setattr(product_service, "build_product_service", lambda _container: _Service())
    monkeypatch.setattr(runner, "_write_scheduler_heartbeat", _write_role_heartbeat)

    outcome: dict[str, object] = {}

    def _run() -> None:
        try:
            outcome["result"] = runner._run_property_search_work_once(
                SimpleNamespace(),
                role="worker",
                log=logging.getLogger("test.property-search-worker-long"),
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            outcome["error"] = exc

    invocation_thread = threading.Thread(target=_run)
    invocation_thread.start()
    started = execution_started.wait(timeout=1.0)
    refreshed_while_running = role_heartbeat_seen.wait(timeout=1.0)
    was_still_running = invocation_thread.is_alive()
    release_execution.set()
    invocation_thread.join(timeout=2.0)

    assert started is True
    assert refreshed_while_running is True
    assert was_still_running is True
    assert invocation_thread.is_alive() is False
    assert "error" not in outcome
    assert outcome["result"]["completed"] is True  # type: ignore[index]
    assert calls[:4] == [
        "role_heartbeat",
        "queue_snapshot",
        "role_heartbeat",
        "claim",
    ]
    lease_heartbeat = calls.index("lease_heartbeat")
    in_flight_role_heartbeat = calls.index("role_heartbeat", lease_heartbeat)
    assert calls[lease_heartbeat : in_flight_role_heartbeat + 1] == [
        "lease_heartbeat",
        "queue_snapshot",
        "role_heartbeat",
    ]
    assert calls[-3:] == ["complete", "queue_snapshot", "role_heartbeat"]


@pytest.mark.parametrize(
    "snapshot",
    (
        SimpleNamespace(depth=True, oldest_item_age_seconds=0.0),
        SimpleNamespace(depth=1.5, oldest_item_age_seconds=0.0),
        SimpleNamespace(depth="1", oldest_item_age_seconds=0.0),
        SimpleNamespace(depth=-1, oldest_item_age_seconds=0.0),
        SimpleNamespace(depth=2**63, oldest_item_age_seconds=0.0),
        SimpleNamespace(depth=0, oldest_item_age_seconds=1.0),
        SimpleNamespace(depth=1, oldest_item_age_seconds=True),
        SimpleNamespace(depth=1, oldest_item_age_seconds=float("nan")),
        SimpleNamespace(
            depth=1,
            oldest_item_age_seconds=(10 * 365 * 24 * 60 * 60) + 1,
        ),
    ),
)
def test_worker_queue_metrics_reject_malformed_or_unbounded_snapshots(
    snapshot: object,
) -> None:
    from app import runner

    with pytest.raises(
        ValueError,
        match="property_search_work_queue_snapshot_invalid",
    ):
        runner._record_property_search_work_queue_metrics(snapshot)


def test_worker_queue_metrics_fail_closed_before_database_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import runner

    runner._record_property_search_work_queue_metrics(
        PropertySearchWorkQueueSnapshot(
            depth=4,
            oldest_item_age_seconds=9.0,
        )
    )
    observed_heartbeats: list[dict[str, object]] = []

    def _capture_heartbeat(*, role: str, status: str) -> None:
        assert role == "worker"
        assert status == "loop"
        observed_heartbeats.append(
            runner._property_search_work_queue_metrics_snapshot()
        )

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(
        runner,
        "_write_scheduler_heartbeat",
        _capture_heartbeat,
    )

    result = runner._run_property_search_work_once(
        SimpleNamespace(),
        role="worker",
        log=logging.getLogger("test.property-search-worker-unconfigured"),
    )

    assert result == {
        "claimed": False,
        "reason": "database_url_missing",
    }
    assert observed_heartbeats == [{"observed": False}]


def test_worker_queue_metrics_fail_closed_when_lease_heartbeat_probe_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import runner
    from app.product import property_search_work_queue as queue_module

    monkeypatch.setenv("DATABASE_URL", "postgresql://unused-in-unit-test")
    monkeypatch.setattr(
        queue_module,
        "property_search_work_heartbeat_seconds",
        lambda: 0.01,
    )
    job = _job(
        run_id="run-worker-heartbeat-failure",
        principal_id="principal-worker-heartbeat-failure",
        attempt_count=1,
        status="leased",
    )
    calls: list[str] = []
    fail_closed_heartbeat = threading.Event()

    class _Repository:
        def __init__(self, _database_url: str) -> None:
            pass

        def observability_snapshot(self) -> PropertySearchWorkQueueSnapshot:
            calls.append("queue_snapshot")
            return PropertySearchWorkQueueSnapshot(
                depth=1,
                oldest_item_age_seconds=0.0,
            )

        def claim(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append("claim")
            return job

        def heartbeat(self, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
            calls.append("lease_heartbeat_failure")
            raise RuntimeError("database heartbeat unavailable")

        def complete(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls.append("complete")
            return _job(
                run_id=job.run_id,
                principal_id=job.principal_id,
                attempt_count=1,
                status="completed",
            )

    class _Service:
        def execute_property_search_work_job(self, claimed_job):  # type: ignore[no-untyped-def]
            assert claimed_job is job
            assert fail_closed_heartbeat.wait(timeout=1.0)
            return {"status": "completed"}

    observed_heartbeats: list[dict[str, object]] = []

    def _capture_heartbeat(*, role: str, status: str) -> None:
        assert role == "worker"
        assert status == "loop"
        snapshot = runner._property_search_work_queue_metrics_snapshot()
        observed_heartbeats.append(snapshot)
        if (
            "lease_heartbeat_failure" in calls
            and snapshot == {"observed": False}
        ):
            fail_closed_heartbeat.set()

    monkeypatch.setattr(
        queue_module,
        "PostgresPropertySearchWorkQueue",
        _Repository,
    )
    monkeypatch.setattr(
        product_service,
        "build_product_service",
        lambda _container: _Service(),
    )
    monkeypatch.setattr(
        runner,
        "_write_scheduler_heartbeat",
        _capture_heartbeat,
    )

    result = runner._run_property_search_work_once(
        SimpleNamespace(),
        role="worker",
        log=logging.getLogger("test.property-search-worker-heartbeat-failure"),
    )

    assert result["completed"] is True
    assert fail_closed_heartbeat.is_set()
    assert {"observed": False} in observed_heartbeats
