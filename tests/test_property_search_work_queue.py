from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.product.property_search_work_queue import (
    PROPERTY_FACT_ENRICHMENT_WORK_KIND,
    PROPERTY_OPPORTUNITY_LTD_IMAGE_WORK_KIND,
    InMemoryPropertySearchWorkQueue,
    PropertySearchWorkQueueSnapshot,
    property_fact_enrichment_work_idempotency_key,
    property_opportunity_ltd_image_work_idempotency_key,
    property_search_work_idempotency_key,
)


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def _record(*, principal_id: str = "principal-a", run_id: str = "run-a") -> dict[str, object]:
    return {
        "principal_id": principal_id,
        "run_id": run_id,
        "status": "queued",
        "created_at": "2026-07-13T08:00:00+00:00",
        "updated_at": "2026-07-13T08:00:00+00:00",
    }


def test_idempotent_enqueue_returns_the_original_job_without_a_duplicate() -> None:
    clock = _Clock()
    repository = InMemoryPropertySearchWorkQueue(now=clock)
    key = property_search_work_idempotency_key(
        principal_id="principal-a",
        run_id="run-a",
        requested_key="client-request-42",
    )

    first = repository.enqueue_run(
        run_record=_record(run_id="run-a"),
        payload_json={"actor": "first"},
        idempotency_key=key,
        max_attempts=3,
    )
    duplicate = repository.enqueue_run(
        run_record=_record(run_id="run-b"),
        payload_json={"actor": "duplicate"},
        idempotency_key=key,
        max_attempts=3,
    )

    assert first.created is True
    assert duplicate.created is False
    assert duplicate.job.job_id == first.job.job_id
    assert duplicate.job.run_id == "run-a"
    assert duplicate.job.payload_json == {"actor": "first"}
    assert len(repository.list_jobs()) == 1


def test_expired_lease_is_reclaimed_and_stale_owner_cannot_complete() -> None:
    clock = _Clock()
    repository = InMemoryPropertySearchWorkQueue(now=clock)
    queued = repository.enqueue_run(
        run_record=_record(),
        payload_json={},
        idempotency_key="queue-key",
        max_attempts=3,
    ).job

    first = repository.claim(lease_owner="worker-a", lease_seconds=30)
    assert first is not None
    assert first.job_id == queued.job_id
    assert first.attempt_count == 1
    assert repository.claim(lease_owner="worker-b", lease_seconds=30) is None

    clock.advance(20)
    assert repository.heartbeat(job_id=first.job_id, lease_owner="worker-a", lease_seconds=30) is True
    clock.advance(31)
    reclaimed = repository.claim(lease_owner="worker-b", lease_seconds=30)

    assert reclaimed is not None
    assert reclaimed.job_id == first.job_id
    assert reclaimed.attempt_count == 2
    assert reclaimed.lease_owner == "worker-b"
    assert repository.complete(job_id=first.job_id, lease_owner="worker-a") is None
    completed = repository.complete(job_id=reclaimed.job_id, lease_owner="worker-b")
    assert completed is not None
    assert completed.status == "completed"


def test_failure_backoff_and_attempt_budget_are_deterministic() -> None:
    clock = _Clock()
    repository = InMemoryPropertySearchWorkQueue(
        now=clock,
        backoff_seconds=lambda attempt: attempt * 10,
    )
    repository.enqueue_run(
        run_record=_record(),
        payload_json={},
        idempotency_key="queue-key",
        max_attempts=2,
    )

    first = repository.claim(lease_owner="worker-a", lease_seconds=30)
    assert first is not None
    retry = repository.fail(job_id=first.job_id, lease_owner="worker-a", error="transient")
    assert retry is not None
    assert retry.status == "queued"
    assert retry.last_error == "transient"

    clock.advance(9)
    assert repository.claim(lease_owner="worker-b", lease_seconds=30) is None
    clock.advance(1)
    second = repository.claim(lease_owner="worker-b", lease_seconds=30)
    assert second is not None
    assert second.attempt_count == 2
    terminal = repository.fail(job_id=second.job_id, lease_owner="worker-b", error="permanent")

    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.completed_at == clock.value
    assert repository.claim(lease_owner="worker-c", lease_seconds=30) is None


def test_crash_after_final_claim_becomes_terminal_when_lease_expires() -> None:
    clock = _Clock()
    repository = InMemoryPropertySearchWorkQueue(now=clock)
    queued = repository.enqueue_run(
        run_record=_record(),
        payload_json={},
        idempotency_key="queue-key",
        max_attempts=1,
    ).job
    claimed = repository.claim(lease_owner="crashed-worker", lease_seconds=30)
    assert claimed is not None

    clock.advance(31)
    assert repository.claim(lease_owner="recovery-worker", lease_seconds=30) is None
    terminal = repository.get(queued.job_id)

    assert terminal is not None
    assert terminal.status == "failed"
    assert terminal.last_error == "lease_expired_after_max_attempts"


def test_observability_snapshot_reports_only_active_queue_shape_and_age() -> None:
    clock = _Clock()
    repository = InMemoryPropertySearchWorkQueue(now=clock)
    first = repository.enqueue_run(
        run_record=_record(run_id="run-a"),
        payload_json={"private_marker": "must-not-escape"},
        idempotency_key="queue-key-a",
    ).job
    clock.advance(10)
    repository.enqueue_run(
        run_record=_record(run_id="run-b"),
        payload_json={"private_marker": "also-private"},
        idempotency_key="queue-key-b",
    )
    claimed = repository.claim(lease_owner="worker-a", lease_seconds=30)

    assert claimed is not None
    assert claimed.job_id == first.job_id
    assert repository.observability_snapshot() == PropertySearchWorkQueueSnapshot(
        depth=2,
        oldest_item_age_seconds=10.0,
    )

    completed = repository.complete(
        job_id=claimed.job_id,
        lease_owner="worker-a",
    )
    assert completed is not None
    assert repository.observability_snapshot() == PropertySearchWorkQueueSnapshot(
        depth=1,
        oldest_item_age_seconds=0.0,
    )


def test_idempotency_keys_are_tenant_scoped() -> None:
    first = property_search_work_idempotency_key(
        principal_id="principal-a",
        run_id="run-a",
        requested_key="same-client-key",
    )
    second = property_search_work_idempotency_key(
        principal_id="principal-b",
        run_id="run-b",
        requested_key="same-client-key",
    )

    assert first != second
    assert "same-client-key" not in first


def test_observability_snapshot_reports_only_identity_free_active_work() -> None:
    clock = _Clock()
    repository = InMemoryPropertySearchWorkQueue(now=clock)

    empty = repository.observability_snapshot()
    assert empty.depth == 0
    assert empty.oldest_item_age_seconds == 0.0

    queued = repository.enqueue_run(
        run_record=_record(),
        payload_json={"private": "never-exported"},
        idempotency_key="queue-observability",
    ).job
    clock.advance(12)

    active = repository.observability_snapshot()
    assert active.depth == 1
    assert active.oldest_item_age_seconds == 12.0
    claimed = repository.claim(lease_owner="worker-a", lease_seconds=30)
    assert claimed is not None
    assert repository.observability_snapshot() == active

    assert repository.complete(job_id=queued.job_id, lease_owner="worker-a") is not None
    completed = repository.observability_snapshot()
    assert completed.depth == 0
    assert completed.oldest_item_age_seconds == 0.0


def test_fact_attempt_can_share_a_run_with_the_search_job_idempotently() -> None:
    clock = _Clock()
    repository = InMemoryPropertySearchWorkQueue(now=clock)
    repository.enqueue_run(
        run_record=_record(),
        payload_json={"actor": "search"},
        idempotency_key="search-key",
    )
    fact_key = property_fact_enrichment_work_idempotency_key(
        principal_id="principal-a",
        run_id="run-a",
        candidate_ref="candidate-a",
        fact_job_id="pfe_123",
        attempt=1,
    )
    payload = {
        "work_kind": PROPERTY_FACT_ENRICHMENT_WORK_KIND,
        "candidate_ref": "candidate-a",
        "fact_job_id": "pfe_123",
    }

    fact = repository.enqueue_fact_enrichment(
        principal_id="principal-a",
        run_id="run-a",
        payload_json=payload,
        idempotency_key=fact_key,
    )
    duplicate = repository.enqueue_fact_enrichment(
        principal_id="principal-a",
        run_id="run-a",
        payload_json=payload,
        idempotency_key=fact_key,
    )

    assert fact.created is True
    assert duplicate.created is False
    assert duplicate.job.job_id == fact.job.job_id
    assert len(repository.list_jobs()) == 2


def test_opportunity_ltd_result_is_idempotent_principal_bound_and_sanitized() -> None:
    clock = _Clock()
    repository = InMemoryPropertySearchWorkQueue(now=clock)
    key = property_opportunity_ltd_image_work_idempotency_key(
        principal_id="principal-a",
        run_id="run-a",
        candidate_ref="property-scout:123",
        opportunity_id="property_opportunity:abc",
    )
    payload = {
        "work_kind": PROPERTY_OPPORTUNITY_LTD_IMAGE_WORK_KIND,
        "candidate_ref": "property-scout:123",
        "opportunity_id": "property_opportunity:abc",
    }

    first = repository.enqueue_opportunity_ltd_image(
        principal_id="principal-a",
        run_id="run-a",
        payload_json=payload,
        idempotency_key=key,
    )
    duplicate = repository.enqueue_opportunity_ltd_image(
        principal_id="principal-a",
        run_id="run-a",
        payload_json=payload,
        idempotency_key=key,
    )
    assert first.created is True
    assert duplicate.created is False
    assert duplicate.job.job_id == first.job.job_id

    claimed = repository.claim(lease_owner="worker-a", lease_seconds=30)
    assert claimed is not None
    result = {
        "status": "ready",
        "artifact": {"asset_url": "https://assets.example.test/cover.png"},
        "receipt": {
            "status": "verified",
            "principal_bound": True,
            "proof_scope": "provider_call",
        },
    }
    completed = repository.complete(
        job_id=claimed.job_id,
        lease_owner="worker-a",
        result_json=result,
    )

    assert completed is not None
    assert completed.status == "completed"
    assert completed.payload_json["result_json"] == result
    assert key != property_opportunity_ltd_image_work_idempotency_key(
        principal_id="principal-b",
        run_id="run-a",
        candidate_ref="property-scout:123",
        opportunity_id="property_opportunity:abc",
    )


def test_completed_ltd_result_compare_and_swap_is_principal_scoped() -> None:
    clock = _Clock()
    repository = InMemoryPropertySearchWorkQueue(now=clock)
    queued = repository.enqueue_opportunity_ltd_image(
        principal_id="principal-a",
        run_id="run-a",
        payload_json={
            "work_kind": PROPERTY_OPPORTUNITY_LTD_IMAGE_WORK_KIND,
            "candidate_ref": "property-scout:123",
            "opportunity_id": "property_opportunity:abc",
        },
        idempotency_key="cover-key",
    ).job
    claimed = repository.claim(lease_owner="worker-a", lease_seconds=30)
    assert claimed is not None
    legacy = {
        "status": "ready",
        "artifact": {"asset_url": "https://provider.example.test/legacy.png"},
    }
    completed = repository.complete(
        job_id=queued.job_id,
        lease_owner="worker-a",
        result_json=legacy,
    )
    assert completed is not None
    replacement = {
        "status": "ready",
        "artifact": {"asset_url": "/app/api/property/opportunities/asset"},
    }

    assert repository.replace_completed_result(
        job_id=queued.job_id,
        principal_id="principal-other",
        expected_result_json=legacy,
        result_json=replacement,
    ) is None
    assert repository.replace_completed_result(
        job_id=queued.job_id,
        principal_id="principal-a",
        expected_result_json={"status": "stale"},
        result_json=replacement,
    ) is None

    clock.advance(1)
    updated = repository.replace_completed_result(
        job_id=queued.job_id,
        principal_id="principal-a",
        expected_result_json=legacy,
        result_json=replacement,
    )
    assert updated is not None
    assert updated.status == "completed"
    assert updated.completed_at == completed.completed_at
    assert updated.updated_at == clock.value
    assert updated.payload_json["result_json"] == replacement
    assert "provider.example.test" not in repr(updated.payload_json)
