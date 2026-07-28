from __future__ import annotations

from types import SimpleNamespace
from typing import NoReturn

import pytest

from app.product import service as product_service
from app.product.service import (
    ProductService,
    PropertySearchAliasDiscoveryIncompleteError,
    PropertySearchRunErasedError,
)


def _active_run(*, run_id: str, principal_id: str) -> dict[str, object]:
    now = product_service._now_iso()
    return {
        "run_id": run_id,
        "principal_id": principal_id,
        "created_at": now,
        "updated_at": now,
        "status": "in_progress",
        "selected_platforms": ["willhaben"],
        "property_search_preferences": {
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Vienna",
            "max_results_per_source": 1,
        },
        "summary": {"status": "in_progress"},
        "events": [],
    }


def _forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("erased property-search work must not reach this side effect")


def test_sync_direct_property_scout_does_not_swallow_erasure_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProductService)
    monkeypatch.setattr(
        service,
        "_prepare_property_search_request_preferences",
        lambda **_kwargs: {
            "country_code": "AT",
            "listing_mode": "rent",
            "location_query": "Vienna",
            "selected_platforms": ["willhaben"],
        },
    )
    monkeypatch.setattr(service, "_property_public_preview_cache_index", lambda: {})
    monkeypatch.setattr(
        service,
        "_property_search_notification_budget_state",
        lambda **_kwargs: {"remaining": 0},
    )
    monkeypatch.setattr(
        product_service,
        "_property_search_execution_platforms",
        lambda _selected, _preferences: (("willhaben",), (), ()),
    )
    monkeypatch.setattr(
        product_service,
        "_merged_property_scout_source_specs",
        lambda **_kwargs: [
            {
                "url": "https://example.test/search",
                "label": "Example",
                "platform": "willhaben",
                "provider_family": "willhaben",
            }
        ],
    )
    monkeypatch.setattr(product_service, "_property_search_prefetch_listing_urls", _forbidden)

    def _cancel(**_kwargs: object) -> None:
        raise PropertySearchRunErasedError("property_search_run_erased")

    with pytest.raises(PropertySearchRunErasedError, match="property_search_run_erased"):
        service.sync_direct_property_scout(
            principal_id="tenant-sync-cancel",
            actor="test",
            selected_platforms=("willhaben",),
            property_search_preferences={"country_code": "AT"},
            max_results_per_source=1,
            progress_callback=_cancel,
        )


def test_pickup_rejected_initial_event_returns_erased_without_work_or_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    service = object.__new__(ProductService)
    record = _active_run(run_id="pickup-initial-erased", principal_id="tenant-pickup-initial")
    monkeypatch.setattr(service, "_record_property_search_run_event", lambda **_kwargs: False)
    monkeypatch.setattr(service, "sync_direct_property_scout", _forbidden)
    monkeypatch.setattr(service, "_open_property_provider_repair_task", _forbidden)

    result = service._pick_up_property_search_run_execution(
        record=record,
        actor="worker",
        reason="test_initial_rejection",
        synchronous=True,
    )

    assert result["status"] == "erased"
    assert result["reason"] == "property_search_run_erased"
    assert "pickup-initial-erased" not in product_service._PROPERTY_SEARCH_RUN_REGISTRY


def test_pickup_rejected_progress_event_returns_erased_without_work_or_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    service = object.__new__(ProductService)
    record = _active_run(run_id="pickup-progress-erased", principal_id="tenant-pickup-progress")
    steps: list[str] = []

    def _record(**kwargs: object) -> bool:
        steps.append(str(kwargs.get("step") or ""))
        return len(steps) == 1

    monkeypatch.setattr(service, "_record_property_search_run_event", _record)
    monkeypatch.setattr(service, "sync_direct_property_scout", _forbidden)
    monkeypatch.setattr(service, "_open_property_provider_repair_task", _forbidden)

    result = service._pick_up_property_search_run_execution(
        record=record,
        actor="worker",
        reason="test_progress_rejection",
        synchronous=True,
    )

    assert result["status"] == "erased"
    assert steps == ["recovery_pickup_started", "starting"]
    assert "pickup-progress-erased" not in product_service._PROPERTY_SEARCH_RUN_REGISTRY


def test_pickup_rejected_final_event_returns_erased_without_sync_or_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    service = object.__new__(ProductService)
    record = _active_run(run_id="pickup-final-erased", principal_id="tenant-pickup-final")
    steps: list[str] = []

    def _record(**kwargs: object) -> bool:
        step = str(kwargs.get("step") or "")
        steps.append(step)
        return step != "completed"

    monkeypatch.setattr(service, "_record_property_search_run_event", _record)
    monkeypatch.setattr(
        service,
        "sync_direct_property_scout",
        lambda **_kwargs: {"status": "processed", "sources_total": 0},
    )
    monkeypatch.setattr(service, "_open_property_provider_repair_task", _forbidden)
    monkeypatch.setattr(service, "_best_effort_propertyquarry_teable_sync", _forbidden)

    result = service._pick_up_property_search_run_execution(
        record=record,
        actor="worker",
        reason="test_final_rejection",
        synchronous=True,
    )

    assert result["status"] == "erased"
    assert steps == ["recovery_pickup_started", "starting", "completed"]
    assert "pickup-final-erased" not in product_service._PROPERTY_SEARCH_RUN_REGISTRY


def test_snapshot_rejected_stale_event_opens_no_repair_or_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProductService)
    run_id = "snapshot-stale-erased"
    principal_id = "tenant-snapshot-stale"
    product_service._PROPERTY_SEARCH_RUN_REGISTRY[run_id] = _active_run(
        run_id=run_id,
        principal_id=principal_id,
    )
    monkeypatch.setattr(product_service, "_load_property_search_run_record", lambda **_kwargs: None)
    monkeypatch.setattr(product_service, "_property_search_run_is_stale", lambda _state: True)
    monkeypatch.setattr(service, "_record_property_search_run_event", lambda **_kwargs: False)
    monkeypatch.setattr(service, "_open_property_search_run_interruption_repair", _forbidden)
    monkeypatch.setattr(service, "_maybe_advance_property_search_run_finalization", _forbidden)
    try:
        assert service._snapshot_property_search_run(
            run_id=run_id,
            principal_id=principal_id,
        ) is None
    finally:
        product_service._PROPERTY_SEARCH_RUN_REGISTRY.pop(run_id, None)


def test_inline_delivery_finalizer_rejected_email_gate_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProductService)
    steps: list[str] = []
    monkeypatch.setattr(
        service,
        "_refresh_property_search_results_delivery_state",
        lambda **kwargs: dict(kwargs["result"]),
    )
    monkeypatch.setattr(service, "_recent_product_event_exists", lambda **_kwargs: False)

    def _reject(**kwargs: object) -> bool:
        steps.append(str(kwargs.get("step") or ""))
        return False

    monkeypatch.setattr(service, "_record_property_search_run_event", _reject)
    monkeypatch.setattr(service, "_notify_property_search_results_ready", _forbidden)

    result = service._maybe_advance_property_search_run_finalization(
        principal_id="tenant-finalizer-inline",
        run_id="run-finalizer-inline",
        state={
            "status": "processed",
            "summary": {"status": "processed", "eligible_tour_total": 0},
        },
        allow_notifications=True,
    )

    assert result is None
    assert steps == ["results_email_sending"]


def test_waiting_delivery_finalizer_rejected_event_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProductService)
    steps: list[str] = []
    monkeypatch.setattr(service, "_recent_product_event_exists", lambda **_kwargs: False)
    monkeypatch.setattr(
        service,
        "_refresh_property_search_results_delivery_state",
        lambda **kwargs: dict(kwargs["result"]),
    )

    def _reject(**kwargs: object) -> bool:
        steps.append(str(kwargs.get("step") or ""))
        return False

    monkeypatch.setattr(service, "_record_property_search_run_event", _reject)
    monkeypatch.setattr(service, "_notify_property_search_results_ready", _forbidden)

    service._await_property_search_results_delivery_ready(
        principal_id="tenant-finalizer-wait",
        run_id="run-finalizer-wait",
        result={"status": "processed", "eligible_tour_total": 0},
        timeout_seconds=1,
        poll_interval_seconds=0.1,
    )

    assert steps == ["results_finalizing"]


def test_irreversible_alias_discovery_bypasses_cache_and_expands_all_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product_service._PROPERTY_SEARCH_RUN_PRINCIPAL_CACHE.clear()
    service = object.__new__(ProductService)
    calls: list[tuple[int, int]] = []
    generation = {"value": "cached"}

    def _candidates(**kwargs: object) -> tuple[dict[str, object], ...]:
        observation_limit = int(kwargs.get("observation_limit") or 0)
        per_principal_limit = int(kwargs.get("per_principal_limit") or 0)
        calls.append((observation_limit, per_principal_limit))
        rows: list[dict[str, object]] = [{"principal_id": "workspace:cached"}]
        if generation["value"] == "fresh" and observation_limit >= 4_000:
            rows.append({"principal_id": "workspace:historical"})
        return tuple(rows)

    monkeypatch.setattr(service, "_workspace_sign_in_candidates", _candidates)
    principal_id = "cf-email:alias-refresh@example.test"
    first = service._property_search_run_principal_ids(
        principal_id=principal_id,
        account_email="alias-refresh@example.test",
    )
    generation["value"] = "fresh"
    refreshed = service._property_search_run_principal_ids(
        principal_id=principal_id,
        account_email="alias-refresh@example.test",
        refresh=True,
        exhaustive=True,
    )

    assert "workspace:historical" not in first
    assert "workspace:historical" in refreshed
    assert calls == [
        (500, 100),
        (500, 100),
        (1_000, 500),
        (2_000, 1_000),
        (4_000, 2_000),
        (5_000, 5_000),
    ]


def test_account_erasure_requests_fresh_exhaustive_aliases_and_forwards_hold_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(ProductService)
    captured: dict[str, object] = {}

    def _aliases(**kwargs: object) -> tuple[str, ...]:
        captured.update(kwargs)
        return ("tenant-erasure", "cf-email:owner@example.test")

    monkeypatch.setattr(service, "_property_search_run_principal_ids", _aliases)
    monkeypatch.setattr(
        product_service,
        "_erase_property_search_account_data_storage",
        lambda **_kwargs: {
            "runs_deleted": 2,
            "work_jobs_deleted": 1,
            "packet_links_deleted": 3,
            "packet_links_legal_hold_retained": 4,
        },
    )

    result = service.erase_property_search_account_data(
        principal_id="tenant-erasure",
        account_email="owner@example.test",
    )

    assert captured["refresh"] is True
    assert captured["exhaustive"] is True
    assert result == {
        "principal_count": 2,
        "runs_deleted": 2,
        "work_jobs_deleted": 1,
        "packet_links_deleted": 3,
        "packet_links_legal_hold_retained": 4,
    }


def test_complete_alias_discovery_rejects_saturated_sign_in_history() -> None:
    service = object.__new__(ProductService)
    service._container = SimpleNamespace(
        channel_runtime=SimpleNamespace(
            list_recent_observations=lambda **_kwargs: [object()] * 5_000,
        ),
    )

    with pytest.raises(
        PropertySearchAliasDiscoveryIncompleteError,
        match="property_search_alias_discovery_incomplete",
    ):
        service._workspace_sign_in_candidates(
            email="saturated@example.test",
            observation_limit=5_000,
            per_principal_limit=5_000,
            require_complete=True,
        )


def test_account_erasure_saturation_fails_before_storage_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    product_service._PROPERTY_SEARCH_RUN_PRINCIPAL_CACHE.clear()
    service = object.__new__(ProductService)
    calls: list[dict[str, object]] = []

    def _saturated(**kwargs: object) -> tuple[dict[str, object], ...]:
        calls.append(dict(kwargs))
        if kwargs.get("require_complete"):
            raise PropertySearchAliasDiscoveryIncompleteError(
                "property_search_alias_discovery_incomplete"
            )
        return ({"principal_id": "workspace:saturated"},)

    monkeypatch.setattr(service, "_workspace_sign_in_candidates", _saturated)
    monkeypatch.setattr(
        product_service,
        "_erase_property_search_account_data_storage",
        _forbidden,
    )

    with pytest.raises(
        PropertySearchAliasDiscoveryIncompleteError,
        match="property_search_alias_discovery_incomplete",
    ):
        service.erase_property_search_account_data(
            principal_id="cf-email:saturated@example.test",
            account_email="saturated@example.test",
        )

    assert calls[-1]["observation_limit"] == 5_000
    assert calls[-1]["require_complete"] is True
