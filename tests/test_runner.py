from __future__ import annotations

from contextlib import contextmanager
import importlib
import inspect
import json
import logging
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from app.domain.models import ConnectorBinding
from app.repositories.delivery_outbox import InMemoryDeliveryOutboxRepository


class _DeliveryOutboxMixin:
    def _init_delivery_outbox(self) -> None:
        self._delivery_outbox = InMemoryDeliveryOutboxRepository()

    def queue_delivery(self, channel, recipient, content, metadata=None, *, principal_id="", idempotency_key=""):
        return self._delivery_outbox.enqueue(
            channel,
            recipient,
            content,
            metadata,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
        )

    def get_delivery(self, delivery_id, *, principal_id=""):
        return self._delivery_outbox.get(delivery_id, principal_id=principal_id)

    def claim_delivery(self, delivery_id, *, lease_owner, lease_seconds, now=None):
        return self._delivery_outbox.claim(
            delivery_id,
            lease_owner=lease_owner,
            lease_seconds=lease_seconds,
            now=now,
        )

    def begin_delivery_attempt(self, delivery_id, *, principal_id, lease_owner, now=None):
        return self._delivery_outbox.begin_attempt(
            delivery_id,
            principal_id=principal_id,
            lease_owner=lease_owner,
            now=now,
        )

    def mark_delivery_sent(self, delivery_id, *, principal_id, receipt_json=None, lease_owner=""):
        return self._delivery_outbox.mark_sent(
            delivery_id,
            principal_id=principal_id,
            receipt_json=receipt_json,
            lease_owner=lease_owner,
        )

    def mark_delivery_failed(
        self,
        delivery_id,
        *,
        principal_id,
        error,
        next_attempt_at=None,
        dead_letter=False,
        lease_owner="",
    ):
        return self._delivery_outbox.mark_failed(
            delivery_id,
            principal_id=principal_id,
            error=error,
            next_attempt_at=next_attempt_at,
            dead_letter=dead_letter,
            lease_owner=lease_owner,
        )


def _load_runner_module(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(run=lambda *args, **kwargs: None))
    return importlib.import_module("app.runner")


def test_scheduler_heartbeat_file_is_healthchecked(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    runner = _load_runner_module(monkeypatch)
    from app import scheduler_healthcheck

    heartbeat_path = tmp_path / "scheduler-heartbeat.json"
    monkeypatch.setenv("EA_ROLE", "scheduler")
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("EA_SCHEDULER_HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setenv("EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR", str(tmp_path / "fleet"))
    monkeypatch.setenv("EA_SCHEDULER_HEARTBEAT_MAX_AGE_SECONDS", "60")

    runner._write_scheduler_heartbeat(role="scheduler", status="idle")

    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert payload["role"] == "scheduler"
    assert payload["status"] == "idle"
    assert payload["profile"] == "property_only"
    assert scheduler_healthcheck.main() == 0

    heartbeat_path.write_text(json.dumps({"epoch": time.time() - 120, "role": "scheduler"}), encoding="utf-8")
    assert scheduler_healthcheck.main() == 1


def test_worker_heartbeat_is_role_aware_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from app import scheduler_healthcheck

    heartbeat_path = tmp_path / "worker-heartbeat.json"
    monkeypatch.setenv("EA_ROLE", "worker")
    monkeypatch.setenv("EA_WORKER_HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setenv("EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR", str(tmp_path / "fleet"))
    monkeypatch.setenv("EA_WORKER_HEARTBEAT_MAX_AGE_SECONDS", "60")

    runner._record_property_search_queue_metrics(None)
    runner._write_scheduler_heartbeat(role="worker", status="loop")

    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert payload["role"] == "worker"
    assert payload["status"] == "loop"
    assert payload["profile"] == ""
    assert int(payload["pid"]) > 0
    assert payload["property_search_work_queue"] == {"observed": False}
    assert scheduler_healthcheck.main() == 0

    heartbeat_path.write_text(
        json.dumps({**payload, "role": "scheduler"}),
        encoding="utf-8",
    )
    assert scheduler_healthcheck.main() == 1

    heartbeat_path.write_text(
        json.dumps({**payload, "pid": 2**31 - 1}),
        encoding="utf-8",
    )
    assert scheduler_healthcheck.main() == 1


def test_worker_queue_heartbeat_rejects_type_confusion_and_nonfinite_age(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = _load_runner_module(monkeypatch)
    heartbeat_path = tmp_path / "worker-heartbeat.json"
    monkeypatch.setenv("EA_WORKER_HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setenv("EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR", str(tmp_path / "fleet"))

    assert (
        runner._record_property_search_queue_metrics(
            SimpleNamespace(depth=True, oldest_item_age_seconds=1.0)
        )
        is False
    )
    runner._write_scheduler_heartbeat(role="worker", status="loop")
    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert payload["property_search_work_queue"] == {"observed": False}

    assert (
        runner._record_property_search_queue_metrics(
            SimpleNamespace(depth=1, oldest_item_age_seconds=float("inf"))
        )
        is False
    )


def test_execution_worker_refreshes_heartbeat_before_role_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)

    source = inspect.getsource(runner._run_execution_worker)
    readiness = source.index("_require_property_search_writer_readiness")
    started = source.index('_write_scheduler_heartbeat(role=role, status="started")')
    loop = source.index("while not stop_event.is_set():")
    heartbeat = source.index('_write_scheduler_heartbeat(role=role, status="loop")', loop)
    scheduler_branch = source.index('if role == "scheduler":', loop)
    queue_execution = source.index("container.orchestrator.run_next_queue_item", loop)

    assert readiness < started < loop < heartbeat < scheduler_branch < queue_execution


def test_execution_writer_readiness_fails_before_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    failed = SimpleNamespace(
        readiness=SimpleNamespace(
            _probe_database=lambda: (
                False,
                "property_search_erasure_key_not_ready:key_id_mismatch",
            )
        )
    )

    with pytest.raises(
        RuntimeError,
        match=(
            "property_search_writer_not_ready:"
            "property_search_erasure_key_not_ready:key_id_mismatch"
        ),
    ):
        runner._require_property_search_writer_readiness(
            failed, role="scheduler"
        )

    ready = SimpleNamespace(
        readiness=SimpleNamespace(_probe_database=lambda: (True, "ready"))
    )
    runner._require_property_search_writer_readiness(ready, role="worker")


def test_scheduler_step_watchdog_keeps_heartbeat_and_avoids_duplicate_launches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = _load_runner_module(monkeypatch)
    heartbeat_path = tmp_path / "scheduler-heartbeat.json"
    release = threading.Event()
    calls = {"count": 0}
    runner._SCHEDULER_STEP_THREADS.clear()
    monkeypatch.setenv("EA_ROLE", "scheduler")
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("EA_SCHEDULER_HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setenv("EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR", str(tmp_path / "fleet"))

    def slow_step() -> dict[str, object]:
        calls["count"] += 1
        release.wait(timeout=2.0)
        return {"ran": True, "attempted": 1, "errors": 0}

    timeout_result = {"ran": True, "attempted": 0, "errors": 1}
    first = runner._run_scheduler_step_with_heartbeat(
        role="scheduler",
        step_name="property_results_finalize",
        timeout_seconds=0.05,
        heartbeat_interval_seconds=0.01,
        timeout_result=timeout_result,
        log=logging.getLogger("test.runner"),
        fn=slow_step,
    )
    second = runner._run_scheduler_step_with_heartbeat(
        role="scheduler",
        step_name="property_results_finalize",
        timeout_seconds=0.05,
        heartbeat_interval_seconds=0.01,
        timeout_result=timeout_result,
        log=logging.getLogger("test.runner"),
        fn=slow_step,
    )

    payload = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    assert first["timeout"] is True
    assert second["timeout"] is True
    assert calls["count"] == 1
    assert payload["status"] == "property_results_finalize_running"

    release.set()
    completed = runner._run_scheduler_step_with_heartbeat(
        role="scheduler",
        step_name="property_results_finalize",
        timeout_seconds=1.0,
        heartbeat_interval_seconds=0.01,
        timeout_result=timeout_result,
        log=logging.getLogger("test.runner"),
        fn=slow_step,
    )
    assert completed == {"ran": True, "attempted": 1, "errors": 0}
    assert calls["count"] == 1
    runner._SCHEDULER_STEP_THREADS.clear()


def test_scheduler_step_watchdog_yields_promptly_for_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = _load_runner_module(monkeypatch)
    heartbeat_path = tmp_path / "scheduler-heartbeat.json"
    release = threading.Event()
    stop_event = threading.Event()
    runner._SCHEDULER_STEP_THREADS.clear()
    monkeypatch.setenv("EA_ROLE", "scheduler")
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("EA_SCHEDULER_HEARTBEAT_PATH", str(heartbeat_path))
    monkeypatch.setenv("EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR", str(tmp_path / "fleet"))

    def slow_step() -> dict[str, object]:
        release.wait(timeout=2.0)
        return {"ran": True, "errors": 0}

    timer = threading.Timer(0.05, stop_event.set)
    timer.start()
    started = time.monotonic()
    result = runner._run_scheduler_step_with_heartbeat(
        role="scheduler",
        step_name="property_results_finalize",
        timeout_seconds=5.0,
        heartbeat_interval_seconds=1.0,
        timeout_result={"ran": True, "errors": 1},
        log=logging.getLogger("test.runner"),
        fn=slow_step,
        stop_event=stop_event,
    )
    elapsed = time.monotonic() - started

    assert result["shutdown"] is True
    assert result["timeout"] is False
    assert result["running"] is True
    assert elapsed < 0.75

    release.set()
    timer.join(timeout=1.0)
    runner._SCHEDULER_STEP_THREADS["property_results_finalize"]["thread"].join(timeout=1.0)
    runner._SCHEDULER_STEP_THREADS.clear()


def test_scheduler_step_watchdog_enforces_global_single_flight(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    runner = _load_runner_module(monkeypatch)
    release = threading.Event()
    calls: list[str] = []
    runner._SCHEDULER_STEP_THREADS.clear()
    monkeypatch.setenv("EA_SCHEDULER_STEP_CONCURRENCY_LIMIT", "1")
    monkeypatch.setenv("EA_SCHEDULER_HEARTBEAT_PATH", str(tmp_path / "scheduler-heartbeat.json"))
    monkeypatch.setenv("EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR", str(tmp_path / "fleet"))

    def slow_step() -> dict[str, object]:
        calls.append("slow")
        release.wait(timeout=2.0)
        return {"ran": True, "errors": 0}

    first = runner._run_scheduler_step_with_heartbeat(
        role="scheduler",
        step_name="property_search_recovery",
        timeout_seconds=0.05,
        heartbeat_interval_seconds=0.01,
        timeout_result={"ran": True, "errors": 1},
        log=logging.getLogger("test.runner"),
        fn=slow_step,
    )
    second = runner._run_scheduler_step_with_heartbeat(
        role="scheduler",
        step_name="property_results_finalize",
        timeout_seconds=0.05,
        heartbeat_interval_seconds=0.01,
        timeout_result={"ran": True, "errors": 1},
        log=logging.getLogger("test.runner"),
        fn=lambda: calls.append("second") or {"ran": True, "errors": 0},
    )

    assert first["timeout"] is True
    assert second["deferred"] is True
    assert second["concurrency_limit"] == 1
    assert second["active_steps"] == ("property_search_recovery",)
    assert calls == ["slow"]

    release.set()
    runner._SCHEDULER_STEP_THREADS["property_search_recovery"]["thread"].join(timeout=1.0)
    runner._SCHEDULER_STEP_THREADS.clear()


def test_scheduler_property_search_recovery_is_heartbeat_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)

    monkeypatch.delenv("EA_SCHEDULER_PROPERTY_SEARCH_RECOVERY_TIMEOUT_SECONDS", raising=False)
    assert runner._scheduler_property_search_recovery_timeout_seconds() == 240.0
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_SEARCH_RECOVERY_TIMEOUT_SECONDS", "10")
    assert runner._scheduler_property_search_recovery_timeout_seconds() == 30.0
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_SEARCH_RECOVERY_TIMEOUT_SECONDS", "45")
    assert runner._scheduler_property_search_recovery_timeout_seconds() == 45.0

    source = inspect.getsource(runner._run_execution_worker)
    assert "now_at_startup = time.time()" in source
    assert "last_property_search_recovery_at = now_at_startup" in source
    assert "last_property_results_finalize_at = now_at_startup" in source
    recovery_block = source[
        source.rindex("_run_scheduler_step_with_heartbeat", 0, source.index('step_name="property_search_recovery"')) :
        source.index('if not property_only_scheduler and now - last_horizon_scan_at')
    ]
    assert "_run_scheduler_step_with_heartbeat" in recovery_block
    assert "_scheduler_property_search_recovery_timeout_seconds()" in recovery_block
    assert "timeout=%s" in recovery_block


def test_scheduler_onemin_billing_refresh_runs_browseract_and_provider_api_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route
    runner = _load_runner_module(monkeypatch)

    calls: list[tuple[str, str, str]] = []
    finished: list[bool] = []

    binding = ConnectorBinding(
        binding_id="binding-1",
        principal_id="principal-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={"onemin_account_name": "ONEMIN_AI_API_KEY"},
        status="enabled",
        created_at="2026-03-26T00:00:00Z",
        updated_at="2026-03-26T00:00:00Z",
    )

    container = SimpleNamespace(
        onemin_manager=SimpleNamespace(
            begin_billing_refresh=lambda: (True, 0.0, ""),
            finish_billing_refresh=lambda: finished.append(True),
        ),
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: [binding]
        ),
    )

    monkeypatch.setattr(providers_route, "_onemin_browseract_max_accounts_per_refresh", lambda: 2)
    monkeypatch.setattr(providers_route, "_onemin_direct_api_batch_backoff_seconds", lambda: 0.0)
    monkeypatch.setattr(providers_route, "_binding_run_url", lambda *args, **kwargs: "")
    monkeypatch.setattr(providers_route, "_binding_workflow_id", lambda *args, **kwargs: "")
    monkeypatch.setattr(providers_route, "_resolve_onemin_account_labels", lambda _binding: {"ONEMIN_AI_API_KEY"})
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_kwargs: True)

    def fake_invoke_browseract_tool(*, container, principal_id: str, tool_name: str, action_kind: str, payload_json: dict[str, object]):
        calls.append((principal_id, tool_name, str(payload_json.get("account_label") or "")))
        return {"account_label": payload_json.get("account_label"), "refresh_backend": tool_name}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(
        providers_route,
        "_refresh_onemin_via_provider_api",
        lambda **_kwargs: ([{"account_label": "ONEMIN_AI_API_KEY"}], [{"account_label": "ONEMIN_AI_API_KEY"}], [], 4, 0, False),
    )

    summary = runner._run_scheduler_onemin_billing_refresh(container, logging.getLogger("test.runner"))

    assert summary["ran"] is True
    assert summary["throttled"] is False
    assert summary["browseract_attempted"] == 1
    assert summary["browseract_refreshed"] == 1
    assert summary["member_reconciled"] == 1
    assert summary["api_attempted"] == 0
    assert summary["api_rate_limited"] is False
    assert summary["errors"] == 0
    assert calls == [
        ("principal-1", "browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY"),
        ("principal-1", "browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY"),
    ]
    assert finished == [True]


def test_scheduler_onemin_billing_refresh_provisions_fastestvpn_for_browseract_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route
    runner = _load_runner_module(monkeypatch)

    binding = ConnectorBinding(
        binding_id="binding-1",
        principal_id="principal-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={"onemin_account_name": "ONEMIN_AI_API_KEY"},
        status="enabled",
        created_at="2026-03-26T00:00:00Z",
        updated_at="2026-03-26T00:00:00Z",
    )

    container = SimpleNamespace(
        onemin_manager=SimpleNamespace(
            begin_billing_refresh=lambda: (True, 0.0, ""),
            finish_billing_refresh=lambda: None,
        ),
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: [binding]
        ),
    )

    monkeypatch.setenv("EA_UI_BROWSER_PROXY_SERVER", "http://ea-fastestvpn-proxy:3128")
    monkeypatch.setattr(providers_route, "_onemin_browseract_max_accounts_per_refresh", lambda: 1)
    monkeypatch.setattr(providers_route, "_onemin_direct_api_batch_backoff_seconds", lambda: 0.0)
    monkeypatch.setattr(providers_route, "_binding_run_url", lambda *args, **kwargs: "")
    monkeypatch.setattr(providers_route, "_binding_workflow_id", lambda *args, **kwargs: "")
    monkeypatch.setattr(providers_route, "_resolve_onemin_account_labels", lambda _binding: {"ONEMIN_AI_API_KEY"})
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_kwargs: True)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_kwargs: ([], [], [], 0, 0, False))
    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", lambda **_kwargs: {"account_label": "ONEMIN_AI_API_KEY", "refresh_backend": "browseract"})

    observed: list[tuple[tuple[str, ...], str]] = []

    @contextmanager
    def fake_managed_fastestvpn_services(*, service_names, reason):
        observed.append((tuple(service_names), reason))
        yield {}

    monkeypatch.setattr(providers_route, "_managed_fastestvpn_services", fake_managed_fastestvpn_services)

    summary = runner._run_scheduler_onemin_billing_refresh(container, logging.getLogger("test.runner"))

    assert summary["ran"] is True
    assert observed == [(("ea-fastestvpn-proxy",), "scheduler.onemin.browseract.refresh")]


def test_scheduler_onemin_billing_refresh_recovers_browseract_failures_via_provider_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route
    runner = _load_runner_module(monkeypatch)

    calls: list[tuple[str, str, str]] = []
    refresh_calls: list[dict[str, object]] = []
    finished: list[bool] = []

    binding = ConnectorBinding(
        binding_id="binding-1",
        principal_id="principal-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={
            "onemin_account_names": [
                "ONEMIN_AI_API_KEY",
                "ONEMIN_AI_API_KEY_FALLBACK_1",
            ]
        },
        status="enabled",
        created_at="2026-03-26T00:00:00Z",
        updated_at="2026-03-26T00:00:00Z",
    )

    container = SimpleNamespace(
        onemin_manager=SimpleNamespace(
            begin_billing_refresh=lambda: (True, 0.0, ""),
            finish_billing_refresh=lambda: finished.append(True),
            select_billing_refresh_account_labels=lambda labels, limit: tuple(list(labels)[:limit]),
        ),
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: [binding]
        ),
    )

    monkeypatch.setattr(providers_route, "_onemin_browseract_max_accounts_per_refresh", lambda: 4)
    monkeypatch.setattr(providers_route, "_onemin_direct_api_batch_backoff_seconds", lambda: 0.0)
    monkeypatch.setattr(providers_route, "_binding_run_url", lambda *args, **kwargs: "")
    monkeypatch.setattr(providers_route, "_binding_workflow_id", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        providers_route,
        "_resolve_onemin_account_labels",
        lambda _binding: {"ONEMIN_AI_API_KEY", "ONEMIN_AI_API_KEY_FALLBACK_1"},
    )
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_kwargs: True)
    monkeypatch.setattr(
        providers_route,
        "_partition_onemin_browseract_account_labels",
        lambda **_kwargs: (
            ["ONEMIN_AI_API_KEY", "ONEMIN_AI_API_KEY_FALLBACK_1"],
            [],
        ),
    )
    monkeypatch.setattr(
        providers_route.upstream,
        "onemin_account_login_credentials",
        lambda **_kwargs: {"login_email": "owner@example.com", "login_password": "slotpass"},
    )

    def fake_invoke_browseract_tool(*, container, principal_id: str, tool_name: str, action_kind: str, payload_json: dict[str, object]):
        account_label = str(payload_json.get("account_label") or "")
        calls.append((principal_id, tool_name, account_label))
        if tool_name == "browseract.onemin_billing_usage" and account_label == "ONEMIN_AI_API_KEY_FALLBACK_1":
            raise providers_route.ToolExecutionError(
                "ui_service_worker_failed:onemin_billing_usage:auth_request_failed"
            )
        return {"account_label": account_label, "refresh_backend": tool_name}

    def fake_refresh(**kwargs):
        refresh_calls.append(dict(kwargs))
        return (
            [{"account_label": "ONEMIN_AI_API_KEY_FALLBACK_1"}],
            [{"account_label": "ONEMIN_AI_API_KEY_FALLBACK_1"}],
            [],
            1,
            0,
            False,
        )

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", fake_refresh)
    monkeypatch.setenv("EA_SCHEDULER_ONEMIN_GLOBAL_PROVIDER_API_SWEEP", "0")

    summary = runner._run_scheduler_onemin_billing_refresh(container, logging.getLogger("test.runner"))

    assert summary["ran"] is True
    assert summary["browseract_attempted"] == 2
    assert summary["browseract_refreshed"] == 1
    assert summary["browseract_failed"] == 1
    assert summary["member_reconciled"] == 2
    assert summary["api_attempted"] == 1
    assert summary["api_recovered"] == 1
    assert summary["errors"] == 0
    assert refresh_calls == [
        {
            "include_members": True,
            "timeout_seconds": 180,
            "all_accounts": False,
            "continue_on_rate_limit": False,
            "account_labels": {"ONEMIN_AI_API_KEY_FALLBACK_1"},
            "account_login_credentials": {
                "ONEMIN_AI_API_KEY_FALLBACK_1": {
                    "login_email": "owner@example.com",
                    "login_password": "slotpass",
                }
            },
        }
    ]
    assert sorted(calls) == sorted(
        [
            ("principal-1", "browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY"),
            ("principal-1", "browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_1"),
            ("principal-1", "browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY"),
        ]
    )
    assert finished == [True]


def test_scheduler_onemin_billing_refresh_uses_owner_ledger_accounts_without_trusted_binding_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import providers as providers_route
    runner = _load_runner_module(monkeypatch)

    calls: list[tuple[str, str, str]] = []
    finished: list[bool] = []

    binding = ConnectorBinding(
        binding_id="binding-1",
        principal_id="principal-1",
        connector_name="browseract",
        external_account_ref="browseract-main",
        scope_json={},
        auth_metadata_json={},
        status="enabled",
        created_at="2026-03-26T00:00:00Z",
        updated_at="2026-03-26T00:00:00Z",
    )

    container = SimpleNamespace(
        onemin_manager=SimpleNamespace(
            begin_billing_refresh=lambda: (True, 0.0, ""),
            finish_billing_refresh=lambda: finished.append(True),
            select_billing_refresh_account_labels=lambda labels, limit: tuple(list(labels)[:limit]),
        ),
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: [binding]
        ),
    )

    monkeypatch.setattr(providers_route, "_onemin_browseract_max_accounts_per_refresh", lambda: 4)
    monkeypatch.setattr(providers_route, "_binding_run_url", lambda *args, **kwargs: "")
    monkeypatch.setattr(providers_route, "_binding_workflow_id", lambda *args, **kwargs: "")
    monkeypatch.setattr(providers_route, "_resolve_onemin_account_labels", lambda _binding: ())
    monkeypatch.setattr(
        providers_route,
        "_normalized_onemin_owner_rows",
        lambda **_kwargs: [
            {"account_name": "ONEMIN_AI_API_KEY", "owner_email": "owner-1@example.com"},
            {"account_name": "ONEMIN_AI_API_KEY_FALLBACK_1", "owner_email": "owner-2@example.com"},
        ],
    )
    monkeypatch.setattr(
        providers_route,
        "_partition_onemin_browseract_account_labels",
        lambda **_kwargs: (
            ["ONEMIN_AI_API_KEY", "ONEMIN_AI_API_KEY_FALLBACK_1"],
            [],
        ),
    )
    monkeypatch.setattr(providers_route, "_browseract_onemin_login_ready", lambda **_kwargs: True)
    monkeypatch.setattr(providers_route.upstream, "onemin_account_login_credentials", lambda **_kwargs: {})
    monkeypatch.setenv("EA_SCHEDULER_ONEMIN_GLOBAL_PROVIDER_API_SWEEP", "0")

    def fake_invoke_browseract_tool(*, container, principal_id: str, tool_name: str, action_kind: str, payload_json: dict[str, object]):
        account_label = str(payload_json.get("account_label") or "")
        calls.append((principal_id, tool_name, account_label))
        return {"account_label": account_label, "refresh_backend": tool_name}

    monkeypatch.setattr(providers_route, "_invoke_browseract_tool", fake_invoke_browseract_tool)
    monkeypatch.setattr(providers_route, "_refresh_onemin_via_provider_api", lambda **_kwargs: ([], [], [], 0, 0, False))

    summary = runner._run_scheduler_onemin_billing_refresh(container, logging.getLogger("test.runner"))

    assert summary["ran"] is True
    assert summary["browseract_attempted"] == 2
    assert summary["browseract_refreshed"] == 2
    assert summary["member_reconciled"] == 2
    assert summary["api_attempted"] == 0
    assert sorted(calls) == sorted(
        [
            ("principal-1", "browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY"),
            ("principal-1", "browseract.onemin_billing_usage", "ONEMIN_AI_API_KEY_FALLBACK_1"),
            ("principal-1", "browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY"),
            ("principal-1", "browseract.onemin_member_reconciliation", "ONEMIN_AI_API_KEY_FALLBACK_1"),
        ]
    )
    assert finished == [True]


def test_scheduler_onemin_billing_refresh_respects_manager_throttle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    finished: list[bool] = []
    container = SimpleNamespace(
        onemin_manager=SimpleNamespace(
            begin_billing_refresh=lambda: (False, 42.0, "cadence"),
            finish_billing_refresh=lambda: finished.append(True),
        ),
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: []
        ),
    )

    summary = runner._run_scheduler_onemin_billing_refresh(container, logging.getLogger("test.runner"))

    assert summary["ran"] is False
    assert summary["throttled"] is True
    assert summary["throttle_seconds_remaining"] == 42.0
    assert summary["throttle_reason"] == "cadence"
    assert summary["browseract_attempted"] == 0
    assert summary["api_attempted"] == 0
    assert finished == []


def test_scheduler_google_signal_sync_runs_for_enabled_google_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)

    calls: list[str] = []

    google_binding = ConnectorBinding(
        binding_id="binding-google-1",
        principal_id="principal-google-1",
        connector_name="google_workspace",
        external_account_ref="exec@example.com",
        scope_json={},
        auth_metadata_json={"google_email": "exec@example.com"},
        status="enabled",
        created_at="2026-03-26T00:00:00Z",
        updated_at="2026-03-26T00:00:00Z",
    )
    disabled_binding = ConnectorBinding(
        binding_id="binding-google-2",
        principal_id="principal-google-2",
        connector_name="google_workspace",
        external_account_ref="skip@example.com",
        scope_json={},
        auth_metadata_json={"google_email": "skip@example.com"},
        status="disabled",
        created_at="2026-03-26T00:00:00Z",
        updated_at="2026-03-26T00:00:00Z",
    )

    class _FakeService:
        def sync_google_workspace_signals(self, *, principal_id: str, actor: str, email_limit: int, calendar_limit: int):
            calls.append(f"{principal_id}|{actor}|{email_limit}|{calendar_limit}")
            return {"total": 2}

    container = SimpleNamespace(
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: [google_binding, disabled_binding]
        ),
    )

    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )

    summary = runner._run_scheduler_google_signal_sync(container, logging.getLogger("test.runner"))

    assert summary == {"ran": True, "attempted": 1, "synced": 1, "errors": 0, "skipped": 0}
    assert calls == ["principal-google-1|scheduler|5|5"]


def test_scheduler_google_signal_sync_runs_configured_property_mailboxes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    monkeypatch.setenv("EA_PROPERTY_ALERT_ACCOUNT_EMAILS", "elisabeth.girschele@gmail.com")

    calls: list[str] = []
    property_calls: list[str] = []
    google_binding = ConnectorBinding(
        binding_id="binding-google-1",
        principal_id="principal-google-1",
        connector_name="google_workspace",
        external_account_ref="tibor@example.com",
        scope_json={},
        auth_metadata_json={"google_email": "tibor@example.com"},
        status="enabled",
        created_at="2026-03-26T00:00:00Z",
        updated_at="2026-03-26T00:00:00Z",
    )

    class _FakeService:
        def sync_google_workspace_signals(self, *, principal_id: str, actor: str, email_limit: int, calendar_limit: int):
            calls.append(f"{principal_id}|{actor}|{email_limit}|{calendar_limit}")
            return {"total": 0}

        def sync_google_willhaben_signals(self, *, principal_id: str, actor: str, account_email: str, email_limit: int):
            property_calls.append(f"{principal_id}|{actor}|{account_email}|{email_limit}")
            return {"synced_total": 2}

    container = SimpleNamespace(
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: [google_binding]
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )

    summary = runner._run_scheduler_google_signal_sync(container, logging.getLogger("test.runner"))

    assert summary == {
        "ran": True,
        "attempted": 1,
        "synced": 1,
        "errors": 0,
        "skipped": 0,
        "property_accounts": ["elisabeth.girschele@gmail.com"],
        "property_attempted": 1,
        "property_synced": 2,
    }
    assert calls == ["principal-google-1|scheduler|5|5"]
    assert property_calls == ["principal-google-1|scheduler|elisabeth.girschele@gmail.com|10"]


def test_scheduler_pocket_signal_sync_runs_for_default_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    monkeypatch.setenv("POCKET_API_KEY", "pk_test")
    monkeypatch.setenv("EA_SCHEDULER_POCKET_SIGNAL_SYNC_LIMIT", "7")

    calls: list[str] = []

    class _FakeService:
        def sync_pocket_recordings(self, *, principal_id: str, actor: str, limit: int):
            calls.append(f"{principal_id}|{actor}|{limit}")
            return {"total": 3}

    container = SimpleNamespace(
        settings=SimpleNamespace(
            auth=SimpleNamespace(default_principal_id="local-user"),
        ),
    )

    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )

    summary = runner._run_scheduler_pocket_signal_sync(container, logging.getLogger("test.runner"))

    assert summary == {
        "ran": True,
        "attempted": 1,
        "synced": 3,
        "errors": 0,
        "principal_ref": runner._scheduler_log_ref("local-user"),
    }
    assert "local-user" not in str(summary)
    assert calls == ["local-user|scheduler|7"]


def test_scheduler_property_scout_runs_for_configured_principals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    monkeypatch.setenv("EA_PROPERTY_SCOUT_PRINCIPAL_IDS", "principal-b, principal-a, principal-a")

    calls: list[str] = []

    class _FakeService:
        def sync_direct_property_scout(self, *, principal_id: str, actor: str):
            calls.append(f"{principal_id}|{actor}")
            return {"status": "processed", "review_created_total": 2}

    container = SimpleNamespace(settings=SimpleNamespace(auth=SimpleNamespace(default_principal_id="fallback")))
    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )

    summary = runner._run_scheduler_property_scout(container, logging.getLogger("test.runner"))

    assert summary == {
        "ran": True,
        "attempted": 2,
        "synced": 4,
        "launched": 0,
        "due": 0,
        "skipped_active": 0,
        "skipped_not_due": 0,
        "errors": 0,
        "principal_count": 2,
    }
    assert calls == ["principal-a|scheduler", "principal-b|scheduler"]


def test_scheduler_property_scout_principal_ids_discover_enabled_saved_search_principals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    monkeypatch.delenv("EA_PROPERTY_SCOUT_PRINCIPAL_IDS", raising=False)

    container = SimpleNamespace(
        onboarding=SimpleNamespace(
            list_property_search_agent_principals=lambda limit=1000: ("principal-b", "principal-a", "principal-a")
        ),
        settings=SimpleNamespace(auth=SimpleNamespace(default_principal_id="fallback")),
    )

    principals = runner._scheduler_property_scout_principal_ids(container)

    assert principals == ("principal-a", "principal-b")


def test_scheduler_property_scout_principal_ids_empty_discovery_does_not_use_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    monkeypatch.delenv("EA_PROPERTY_SCOUT_PRINCIPAL_IDS", raising=False)
    monkeypatch.setenv("EA_DEFAULT_PRINCIPAL_ID", "legacy-default")

    container = SimpleNamespace(
        onboarding=SimpleNamespace(
            list_property_search_agent_principals=lambda limit=1000: ()
        ),
        settings=SimpleNamespace(auth=SimpleNamespace(default_principal_id="fallback")),
    )

    assert runner._scheduler_property_scout_principal_ids(container) == ()


def test_scheduler_property_scout_principal_ids_failed_discovery_does_not_use_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    monkeypatch.delenv("EA_PROPERTY_SCOUT_PRINCIPAL_IDS", raising=False)
    monkeypatch.setenv("EA_DEFAULT_PRINCIPAL_ID", "legacy-default")

    def _failed_discovery(*, limit: int = 1000):
        raise RuntimeError("projection unavailable")

    container = SimpleNamespace(
        onboarding=SimpleNamespace(
            list_property_search_agent_principals=_failed_discovery
        ),
        settings=SimpleNamespace(auth=SimpleNamespace(default_principal_id="fallback")),
    )

    assert runner._scheduler_property_scout_principal_ids(container) == ()


def test_scheduler_log_ref_is_stable_without_disclosing_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner_module(monkeypatch)
    principal_id = "cf-email:person@example.test"

    first = runner._scheduler_log_ref(principal_id)
    second = runner._scheduler_log_ref(principal_id.upper())

    assert first == second
    assert first.startswith("ref:")
    assert principal_id not in first
    assert "person@example.test" not in first


def test_scheduler_property_scout_queues_due_search_agents_when_service_supports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    monkeypatch.setenv("EA_PROPERTY_SCOUT_PRINCIPAL_IDS", "principal-b, principal-a")

    calls: list[str] = []

    class _FakeService:
        def launch_due_property_search_agents(self, *, principal_id: str, actor: str):
            calls.append(f"agents:{principal_id}|{actor}")
            return {
                "mode": "agents",
                "launched_total": 1,
                "due_total": 1,
                "skipped_active_total": 0,
                "skipped_not_due_total": 0,
            }

        def sync_direct_property_scout(self, *, principal_id: str, actor: str):
            raise AssertionError("scheduler should not fall back to sync_direct_property_scout when agent launcher is available")

    container = SimpleNamespace(settings=SimpleNamespace(auth=SimpleNamespace(default_principal_id="fallback")))
    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )

    summary = runner._run_scheduler_property_scout(container, logging.getLogger("test.runner"))

    assert summary == {
        "ran": True,
        "attempted": 2,
        "synced": 2,
        "launched": 2,
        "due": 2,
        "skipped_active": 0,
        "skipped_not_due": 0,
        "errors": 0,
        "principal_count": 2,
    }
    assert calls == ["agents:principal-a|scheduler", "agents:principal-b|scheduler"]


def test_scheduler_property_only_profile_helper_accepts_property_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)

    monkeypatch.delenv("PROPERTYQUARRY_SCHEDULER_PROFILE", raising=False)
    assert runner._scheduler_property_only_profile_enabled() is False

    for value in ("property_only", "property-only", "property"):
        monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", value)
        assert runner._scheduler_property_only_profile_enabled() is True

    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "full")
    assert runner._scheduler_property_only_profile_enabled() is False


def test_worker_property_only_profile_helper_accepts_property_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)

    monkeypatch.delenv("PROPERTYQUARRY_WORKER_PROFILE", raising=False)
    assert runner._worker_property_only_profile_enabled() is False

    for value in ("property_only", "property-only", "property"):
        monkeypatch.setenv("PROPERTYQUARRY_WORKER_PROFILE", value)
        assert runner._worker_property_only_profile_enabled() is True

    monkeypatch.setenv("PROPERTYQUARRY_WORKER_PROFILE", "full")
    assert runner._worker_property_only_profile_enabled() is False


def test_scheduler_morning_memo_delivery_sends_once_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner_module(monkeypatch)

    google_binding = ConnectorBinding(
        binding_id="binding-google-1",
        principal_id="principal-memo-1",
        connector_name="google_workspace",
        external_account_ref="exec@example.com",
        scope_json={},
        auth_metadata_json={"google_email": "exec@example.com"},
        status="enabled",
        created_at="2026-03-30T00:00:00Z",
        updated_at="2026-03-30T00:00:00Z",
    )
    preference = SimpleNamespace(
        preference_id="pref-memo-1",
        principal_id="principal-memo-1",
        channel="email",
        recipient_ref="morning_memo_primary",
        cadence="weekdays_morning",
        quiet_hours_json={
            "timezone": "UTC",
            "delivery_time_local": "08:00",
            "quiet_hours_start": "20:00",
            "quiet_hours_end": "07:00",
            "delivery_window_minutes": 120,
        },
        format_json={
            "schedule_kind": "morning_memo",
            "digest_key": "memo",
            "role": "principal",
            "display_name": "Exec One",
            "delivery_channel": "email",
            "retry_after_minutes": 60,
        },
        status="active",
    )

    service_calls: list[tuple[str, str, str]] = []
    ingested_events: list[tuple[str, str]] = []
    dedupe_index: dict[str, SimpleNamespace] = {}

    class _FakeChannelRuntime(_DeliveryOutboxMixin):
        def __init__(self) -> None:
            self._init_delivery_outbox()

        def find_observation_by_dedupe(self, dedupe_key: str, *, principal_id: str | None = None):
            return dedupe_index.get(dedupe_key)

        def list_recent_observations(self, limit: int = 50, principal_id: str | None = None):
            return []

        def ingest_observation(
            self,
            principal_id: str,
            channel: str,
            event_type: str,
            payload: dict[str, object] | None = None,
            *,
            source_id: str = "",
            external_id: str = "",
            dedupe_key: str = "",
            auth_context_json: dict[str, object] | None = None,
            raw_payload_uri: str = "",
        ):
            ingested_events.append((event_type, dedupe_key))
            row = SimpleNamespace(
                event_type=event_type,
                payload=dict(payload or {}),
                created_at="2026-03-30T08:05:00+00:00",
            )
            if dedupe_key:
                dedupe_index[dedupe_key] = row
            return row

    class _FakeService:
        def channel_digest_pack(self, *, principal_id: str, digest_key: str, operator_id: str = ""):
            return {"key": digest_key, "items": [{"title": "Memo", "tag": "Memo"}]}

        def issue_channel_digest_delivery(
            self,
            *,
            principal_id: str,
            digest_key: str,
            recipient_email: str,
            role: str,
            display_name: str = "",
            operator_id: str = "",
            delivery_channel: str = "email",
            expires_in_hours: int = 72,
            base_url: str = "",
            idempotency_key: str = "",
        ):
            assert idempotency_key.startswith("propertyquarry-morning-memo:")
            service_calls.append((principal_id, digest_key, recipient_email))
            return {
                "delivery_id": "digest-1",
                "digest_key": digest_key,
                "email_delivery_status": "sent",
            }

    container = SimpleNamespace(
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: [google_binding]
        ),
        memory_runtime=SimpleNamespace(
            list_delivery_preferences=lambda principal_id, limit=50, status=None: [preference]
        ),
        channel_runtime=_FakeChannelRuntime(),
    )

    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.registration_email",
        SimpleNamespace(email_delivery_enabled=lambda: True),
    )

    now_utc = runner.datetime(2026, 3, 30, 8, 5, tzinfo=runner.timezone.utc)
    summary = runner._run_scheduler_morning_memo_delivery(
        container,
        logging.getLogger("test.runner"),
        now_utc=now_utc,
    )

    assert summary == {
        "ran": True,
        "configured": 1,
        "due": 1,
        "sent": 1,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "queued": 1,
        "claimed": 1,
        "claim_conflicts": 0,
        "retried": 0,
        "dead_lettered": 0,
    }
    assert service_calls == [("principal-memo-1", "memo", "exec@example.com")]
    assert ingested_events == [
        ("scheduled_morning_memo_delivery_sent", "principal-memo-1|scheduled-morning-memo|pref-memo-1|2026-03-30|sent")
    ]

    second_summary = runner._run_scheduler_morning_memo_delivery(
        container,
        logging.getLogger("test.runner"),
        now_utc=now_utc,
    )
    assert second_summary["sent"] == 0
    assert second_summary["skipped"] == 1


def test_scheduler_morning_memo_delivery_respects_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner_module(monkeypatch)

    google_binding = ConnectorBinding(
        binding_id="binding-google-1",
        principal_id="principal-memo-2",
        connector_name="google_workspace",
        external_account_ref="exec@example.com",
        scope_json={},
        auth_metadata_json={"google_email": "exec@example.com"},
        status="enabled",
        created_at="2026-03-30T00:00:00Z",
        updated_at="2026-03-30T00:00:00Z",
    )
    preference = SimpleNamespace(
        preference_id="pref-memo-2",
        principal_id="principal-memo-2",
        channel="email",
        recipient_ref="morning_memo_primary",
        cadence="daily_morning",
        quiet_hours_json={
            "timezone": "UTC",
            "delivery_time_local": "08:00",
            "quiet_hours_start": "20:00",
            "quiet_hours_end": "07:00",
            "delivery_window_minutes": 120,
        },
        format_json={
            "schedule_kind": "morning_memo",
            "digest_key": "memo",
            "role": "principal",
            "display_name": "Exec Two",
            "delivery_channel": "email",
            "retry_after_minutes": 60,
        },
        status="active",
    )
    service_calls: list[str] = []

    class _FakeService:
        def channel_digest_pack(self, *, principal_id: str, digest_key: str, operator_id: str = ""):
            return {"key": digest_key, "items": [{"title": "Memo", "tag": "Memo"}]}

        def issue_channel_digest_delivery(self, **kwargs):
            service_calls.append("called")
            return {"delivery_id": "digest-2", "digest_key": "memo", "email_delivery_status": "failed"}

    class _FakeChannelRuntime(_DeliveryOutboxMixin):
        def __init__(self) -> None:
            self._init_delivery_outbox()

        def find_observation_by_dedupe(self, dedupe_key, principal_id=None):
            return None

        def list_recent_observations(self, limit=50, principal_id=None):
            return []

        def ingest_observation(self, *args, **kwargs):
            return None

    container = SimpleNamespace(
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: [google_binding]
        ),
        memory_runtime=SimpleNamespace(
            list_delivery_preferences=lambda principal_id, limit=50, status=None: [preference]
        ),
        channel_runtime=_FakeChannelRuntime(),
    )

    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.registration_email",
        SimpleNamespace(email_delivery_enabled=lambda: True),
    )

    now_utc = runner.datetime(2026, 3, 30, 8, 5, tzinfo=runner.timezone.utc)
    summary = runner._run_scheduler_morning_memo_delivery(
        container,
        logging.getLogger("test.runner"),
        now_utc=now_utc,
    )

    assert summary == {
        "ran": True,
        "configured": 1,
        "due": 1,
        "sent": 0,
        "blocked": 0,
        "failed": 1,
        "skipped": 0,
        "errors": 0,
        "queued": 1,
        "claimed": 1,
        "claim_conflicts": 0,
        "retried": 1,
        "dead_lettered": 0,
    }
    assert service_calls == ["called"]

    deferred = runner._run_scheduler_morning_memo_delivery(
        container,
        logging.getLogger("test.runner"),
        now_utc=now_utc,
    )
    assert deferred["blocked"] == 1
    assert deferred["sent"] == 0
    assert service_calls == ["called"]


def test_scheduler_actionable_nudge_delivery_sends_telegram_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner_module(monkeypatch)

    telegram_binding = ConnectorBinding(
        binding_id="binding-telegram-1",
        principal_id="principal-nudge-1",
        connector_name="telegram_identity",
        external_account_ref="1354554303",
        scope_json={},
        auth_metadata_json={"default_chat_ref": "1354554303"},
        status="enabled",
        created_at="2026-03-30T00:00:00Z",
        updated_at="2026-03-30T00:00:00Z",
    )
    preference = SimpleNamespace(
        preference_id="pref-nudge-1",
        principal_id="principal-nudge-1",
        channel="telegram",
        recipient_ref="assistant_nudge_primary",
        cadence="daily_morning",
        quiet_hours_json={
            "timezone": "UTC",
            "delivery_time_local": "08:00",
            "quiet_hours_start": "20:00",
            "quiet_hours_end": "07:00",
            "delivery_window_minutes": 120,
        },
        format_json={
            "schedule_kind": "assistant_nudge",
            "digest_key": "assistant_nudge",
            "role": "principal",
            "display_name": "Exec Nudge",
            "delivery_channel": "telegram",
            "retry_after_minutes": 60,
        },
        status="active",
    )

    service_calls: list[tuple[str, str, str, str]] = []
    ingested_events: list[tuple[str, str]] = []
    dedupe_index: dict[str, SimpleNamespace] = {}

    class _FakeChannelRuntime(_DeliveryOutboxMixin):
        def __init__(self) -> None:
            self._init_delivery_outbox()

        def find_observation_by_dedupe(self, dedupe_key: str, *, principal_id: str | None = None):
            return dedupe_index.get(dedupe_key)

        def list_recent_observations(self, limit: int = 50, principal_id: str | None = None):
            return []

        def ingest_observation(
            self,
            principal_id: str,
            channel: str,
            event_type: str,
            payload: dict[str, object] | None = None,
            *,
            source_id: str = "",
            external_id: str = "",
            dedupe_key: str = "",
            auth_context_json: dict[str, object] | None = None,
            raw_payload_uri: str = "",
        ):
            ingested_events.append((event_type, dedupe_key))
            row = SimpleNamespace(
                event_type=event_type,
                payload=dict(payload or {}),
                created_at="2026-03-30T08:05:00+00:00",
            )
            if dedupe_key:
                dedupe_index[dedupe_key] = row
            return row

    class _FakeService:
        def channel_digest_pack(self, *, principal_id: str, digest_key: str, operator_id: str = ""):
            assert principal_id == "principal-nudge-1"
            assert digest_key == "assistant_nudge"
            return {"key": "assistant_nudge", "items": [{"title": "Reply to landlord", "tag": "Approval"}]}

        def issue_channel_digest_delivery(
            self,
            *,
            principal_id: str,
            digest_key: str,
            recipient_email: str,
            role: str,
            display_name: str = "",
            operator_id: str = "",
            delivery_channel: str = "email",
            expires_in_hours: int = 72,
            base_url: str = "",
            idempotency_key: str = "",
        ):
            assert idempotency_key.startswith("propertyquarry-morning-memo:")
            service_calls.append((principal_id, digest_key, recipient_email, delivery_channel))
            return {
                "delivery_id": "digest-nudge-1",
                "digest_key": digest_key,
                "telegram_delivery_status": "sent",
            }

    container = SimpleNamespace(
        tool_runtime=SimpleNamespace(
            list_connector_bindings_for_connector=lambda connector_name, limit=1000: [telegram_binding]
            if connector_name == "telegram_identity"
            else []
        ),
        memory_runtime=SimpleNamespace(
            list_delivery_preferences=lambda principal_id, limit=50, status=None: [preference]
        ),
        channel_runtime=_FakeChannelRuntime(),
    )

    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.registration_email",
        SimpleNamespace(email_delivery_enabled=lambda: True),
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.telegram_onboarding_service",
        SimpleNamespace(TELEGRAM_IDENTITY_CONNECTOR="telegram_identity"),
    )

    now_utc = runner.datetime(2026, 3, 30, 8, 5, tzinfo=runner.timezone.utc)
    summary = runner._run_scheduler_morning_memo_delivery(
        container,
        logging.getLogger("test.runner"),
        now_utc=now_utc,
    )

    assert summary == {
        "ran": True,
        "configured": 1,
        "due": 1,
        "sent": 1,
        "blocked": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "queued": 1,
        "claimed": 1,
        "claim_conflicts": 0,
        "retried": 0,
        "dead_lettered": 0,
    }
    assert service_calls == [("principal-nudge-1", "assistant_nudge", "principal-nudge-1", "telegram")]
    assert ingested_events == [
        ("scheduled_morning_memo_delivery_sent", "principal-nudge-1|scheduled-morning-memo|pref-nudge-1|2026-03-30|sent")
    ]


def test_scheduler_property_results_finalize_reconciles_ready_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _load_runner_module(monkeypatch)
    observed: list[dict[str, object]] = []

    class _FakeService:
        def reconcile_property_search_results_delivery(
            self,
            *,
            principal_id: str = "",
            limit: int = 20,
            allow_notifications: bool = True,
        ):
            observed.append({"limit": limit, "allow_notifications": allow_notifications})
            return {"attempted": 2, "finalized": 1, "emailed": 1, "pending": 1}

    container = SimpleNamespace()
    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )

    summary = runner._run_scheduler_property_results_finalize(container, logging.getLogger("test.runner"))

    assert summary == {
        "ran": True,
        "attempted": 2,
        "finalized": 1,
        "emailed": 1,
        "pending": 1,
        "errors": 0,
        "repair_resolved_total": 0,
        "repair_deferred_total": 0,
        "visual_followup_resolved_total": 0,
        "visual_followup_failed_total": 0,
    }
    assert observed == [{"limit": 40, "allow_notifications": False}]


def test_scheduler_property_results_finalize_bounds_maintenance_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    principal_id = "cf-email:person@example.test"
    monkeypatch.setenv("EA_PROPERTY_SCOUT_PRINCIPAL_IDS", principal_id)
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_RESULTS_FINALIZE_LIMIT", "17")
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_PROVIDER_REPAIR_LIMIT", "3")
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_TOUR_FOLLOWUP_LIMIT", "2")
    observed: list[tuple[str, str, int]] = []

    class _FakeService:
        def reconcile_property_search_results_delivery(
            self,
            *,
            principal_id: str = "",
            limit: int = 20,
            allow_notifications: bool = True,
        ):
            observed.append(("finalize", principal_id, limit))
            assert allow_notifications is False
            return {"attempted": 0, "finalized": 0, "emailed": 0, "pending": 0}

        def process_property_provider_repair_tasks(self, *, principal_id: str, actor: str, limit: int):
            assert actor == "scheduler"
            observed.append(("repair", principal_id, limit))
            return {"resolved_total": 1, "deferred_total": 0}

        def process_property_tour_followup_tasks(self, *, principal_id: str, actor: str, limit: int):
            assert actor == "scheduler"
            observed.append(("tour", principal_id, limit))
            return {"resolved_total": 1, "failed_total": 0}

    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )

    summary = runner._run_scheduler_property_results_finalize(
        SimpleNamespace(),
        logging.getLogger("test.runner"),
    )

    assert observed == [
        ("finalize", "", 17),
        ("repair", principal_id, 3),
        ("tour", principal_id, 2),
    ]
    assert summary["repair_resolved_total"] == 1
    assert summary["visual_followup_resolved_total"] == 1
    assert principal_id not in str(summary)


def test_scheduler_property_results_finalize_shares_global_budgets_across_principals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    principals = ("principal-a", "principal-b", "principal-c", "principal-d")
    monkeypatch.setattr(runner, "_scheduler_property_scout_principal_ids", lambda _container: principals)
    monkeypatch.setattr(runner, "_SCHEDULER_PROPERTY_MAINTENANCE_ROTATION", 0)
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_PROVIDER_REPAIR_LIMIT", "3")
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_TOUR_FOLLOWUP_LIMIT", "2")
    observed: list[tuple[str, str, int]] = []

    class _FakeService:
        def reconcile_property_search_results_delivery(self, **_kwargs):
            return {"attempted": 0, "finalized": 0, "emailed": 0, "pending": 0}

        def process_property_provider_repair_tasks(self, *, principal_id: str, actor: str, limit: int):
            assert actor == "scheduler"
            observed.append(("repair", principal_id, limit))
            attempted = 2 if principal_id == "principal-a" else 1
            return {"attempted_total": attempted, "resolved_total": attempted, "deferred_total": 0}

        def process_property_tour_followup_tasks(self, *, principal_id: str, actor: str, limit: int):
            assert actor == "scheduler"
            observed.append(("tour", principal_id, limit))
            return {"attempted_total": 1, "resolved_total": 1, "failed_total": 0}

    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )

    summary = runner._run_scheduler_property_results_finalize(
        SimpleNamespace(),
        logging.getLogger("test.runner"),
    )

    assert observed == [
        ("repair", "principal-a", 3),
        ("tour", "principal-a", 2),
        ("repair", "principal-b", 1),
        ("tour", "principal-b", 1),
    ]
    assert summary["repair_resolved_total"] == 3
    assert summary["visual_followup_resolved_total"] == 2


def test_scheduler_property_results_finalize_rotates_maintenance_first_principal_each_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    principals = ("principal-a", "principal-b", "principal-c")
    monkeypatch.setattr(runner, "_scheduler_property_scout_principal_ids", lambda _container: principals)
    monkeypatch.setattr(runner, "_SCHEDULER_PROPERTY_MAINTENANCE_ROTATION", 0)
    monkeypatch.setattr(runner, "time", SimpleNamespace(time=lambda: 0.0))
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_PROVIDER_REPAIR_LIMIT", "1")
    monkeypatch.setenv("EA_SCHEDULER_PROPERTY_TOUR_FOLLOWUP_LIMIT", "1")
    first_principals: list[str] = []

    class _FakeService:
        def reconcile_property_search_results_delivery(self, **_kwargs):
            return {"attempted": 0, "finalized": 0, "emailed": 0, "pending": 0}

        def process_property_provider_repair_tasks(self, *, principal_id: str, actor: str, limit: int):
            assert actor == "scheduler"
            assert limit == 1
            first_principals.append(principal_id)
            return {"attempted_total": 1, "resolved_total": 1, "deferred_total": 0}

        def process_property_tour_followup_tasks(self, *, principal_id: str, actor: str, limit: int):
            assert actor == "scheduler"
            assert limit == 1
            return {"attempted_total": 1, "resolved_total": 1, "failed_total": 0}

    monkeypatch.setitem(
        sys.modules,
        "app.product.service",
        SimpleNamespace(build_product_service=lambda _container: _FakeService()),
    )

    for _ in range(4):
        runner._run_scheduler_property_results_finalize(
            SimpleNamespace(),
            logging.getLogger("test.runner"),
        )

    assert first_principals == ["principal-a", "principal-b", "principal-c", "principal-a"]
