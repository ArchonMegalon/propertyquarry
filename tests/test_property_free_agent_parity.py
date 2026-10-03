"""Contract: the free PropertyQuarry plan is entitlement-equivalent to agent."""

from __future__ import annotations

from app.services.property_billing import (
    PROPERTY_PLAN_PARITY_FIELDS,
    property_plan_spec,
    property_plans_equivalent,
    property_worker_cap,
)


def test_free_plan_matches_agent_on_every_parity_field() -> None:
    assert property_plans_equivalent("free", "agent") is True


def test_free_plan_matches_agent_field_by_field() -> None:
    free = property_plan_spec("free")
    agent = property_plan_spec("agent")
    for field in PROPERTY_PLAN_PARITY_FIELDS:
        assert getattr(free, field) == getattr(agent, field), (
            "free/agent parity broken on field:",
            field,
            getattr(free, field),
            getattr(agent, field),
        
        )


def test_free_plan_worker_concurrency_matches_agent() -> None:
    assert property_worker_cap("free") == property_worker_cap("agent")


def test_free_plan_features_copy_matches_agent() -> None:
    free = property_plan_spec("free")
    agent = property_plan_spec("agent")
    assert free.features == agent.features


def test_plus_plan_keeps_distinct_entitlements() -> None:
    plus = property_plan_spec("plus")
    agent = property_plan_spec("agent")
    assert plus.max_match_score == 45
    assert agent.max_match_score == 60
    assert plus.search_agent_limit != agent.search_agent_limit
