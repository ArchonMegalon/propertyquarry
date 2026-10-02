from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app import runner
from app.api.routes.onboarding import OnboardingPropertySearchPreferencesIn
from app.product import property_search_schema
from app.product import service as product_service
from app.product.property_search_work_queue import (
    InMemoryPropertySearchWorkQueue,
    PROPERTY_SEARCH_WORK_GLOBAL_CONCURRENCY,
    PROPERTY_SEARCH_WORK_PRIORITY_FREE,
    PROPERTY_SEARCH_WORK_PRIORITY_PAID,
    PostgresPropertySearchWorkQueue,
)
from app.services.property_billing import (
    enforce_property_plan_limits,
    property_plan_catalog,
)


ROOT = Path(__file__).resolve().parents[1]


def test_every_plan_can_request_the_full_provider_catalog() -> None:
    plans = property_plan_catalog()

    assert {plan.plan_key for plan in plans} == {"free", "plus", "agent"}
    assert all(plan.max_platforms == 0 for plan in plans)
    enforce_property_plan_limits(
        property_preferences={
            "property_commercial": {"active_plan_key": "free", "status": "free"}
        },
        selected_platforms=tuple(f"provider-{index}" for index in range(20)),
        max_results_per_source=None,
    )


@pytest.mark.parametrize(
    ("configured", "expected"),
    (("", 2), ("1", 1), ("2", 2), ("3", 2), ("999", 2), ("invalid", 2)),
)
def test_run_worker_concurrency_is_hard_capped_at_two(
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    expected: int,
) -> None:
    if configured:
        monkeypatch.setenv("PROPERTYQUARRY_SEARCH_RUN_WORKER_CONCURRENCY", configured)
    else:
        monkeypatch.delenv("PROPERTYQUARRY_SEARCH_RUN_WORKER_CONCURRENCY", raising=False)

    assert runner._property_search_work_batch_concurrency() == expected
    assert product_service._property_search_run_worker_concurrency() == expected
    assert PROPERTY_SEARCH_WORK_GLOBAL_CONCURRENCY == 2


def test_queue_priority_is_derived_from_persisted_commercial_state() -> None:
    free_record = {
        "property_search_preferences": {
            "property_commercial": {"active_plan_key": "free", "status": "free"}
        }
    }
    paid_record = {
        "property_search_preferences": {
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
            }
        }
    }

    assert (
        product_service._property_search_work_priority_for_record(free_record)
        == PROPERTY_SEARCH_WORK_PRIORITY_FREE
    )
    assert (
        product_service._property_search_work_priority_for_record(paid_record)
        == PROPERTY_SEARCH_WORK_PRIORITY_PAID
    )


def test_public_preferences_cannot_self_assign_paid_queue_priority() -> None:
    request = OnboardingPropertySearchPreferencesIn(
        country_code="AT",
        selected_platforms=["willhaben"],
        property_commercial={
            "active_plan_key": "agent",
            "status": "active",
            "active_until": "2999-01-01T00:00:00+00:00",
        },
    )

    persisted = dict(request.model_dump())

    assert "property_commercial" not in persisted


def test_postgres_queue_serializes_global_claims_and_orders_paid_first() -> None:
    candidate_source = inspect.getsource(
        PostgresPropertySearchWorkQueue._nonlocking_claim_candidate_job_ids
    )
    claim_source = inspect.getsource(PostgresPropertySearchWorkQueue.claim)

    assert "ORDER BY priority_class DESC" in candidate_source
    assert "pg_advisory_xact_lock" in claim_source
    assert "globally_active_jobs" in claim_source
    assert "PROPERTY_SEARCH_WORK_GLOBAL_CONCURRENCY" in claim_source


def test_paid_work_jumps_queued_free_work_without_preempting_active_work() -> None:
    queue = InMemoryPropertySearchWorkQueue()

    def enqueue(label: str, priority_class: int) -> str:
        return queue.enqueue_run(
            run_record={
                "principal_id": f"principal-{label}",
                "run_id": f"run-{label}",
            },
            payload_json={"work_kind": "property_search_run"},
            idempotency_key=f"queue-policy-{label}",
            priority_class=priority_class,
        ).job.job_id

    active_free_id = enqueue("active-free", PROPERTY_SEARCH_WORK_PRIORITY_FREE)
    active_free = queue.claim(lease_owner="worker-free", lease_seconds=60)
    assert active_free is not None
    assert active_free.job_id == active_free_id

    queued_free_id = enqueue("queued-free", PROPERTY_SEARCH_WORK_PRIORITY_FREE)
    paid_id = enqueue("paid", PROPERTY_SEARCH_WORK_PRIORITY_PAID)
    paid = queue.claim(lease_owner="worker-paid", lease_seconds=60)

    assert paid is not None
    assert paid.job_id == paid_id
    assert queue.claim(lease_owner="worker-third", lease_seconds=60) is None
    assert queue.get(active_free_id).status == "leased"
    assert queue.get(paid_id).status == "leased"
    assert queue.get(queued_free_id).status == "queued"


def test_priority_schema_migration_and_production_worker_contract_are_explicit() -> None:
    migration = property_search_schema.PROPERTY_SEARCH_MIGRATIONS[-1]
    compose = (ROOT / "docker-compose.property.yml").read_text(encoding="utf-8")

    assert migration.version == 21
    assert migration.name == "paid_first_bounded_work_queue"
    assert "ADD COLUMN priority_class SMALLINT NOT NULL DEFAULT 0" in migration.sql
    assert "CHECK (priority_class IN (0, 100))" in migration.sql
    assert "PROPERTYQUARRY_SEARCH_RUN_WORKER_CONCURRENCY: \"2\"" in compose
