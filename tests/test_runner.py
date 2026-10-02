from __future__ import annotations

from contextlib import contextmanager
import importlib
import inspect
import json
import logging
from pathlib import Path
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


@pytest.fixture(autouse=True)
def _verified_source_refresh_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts import propertyquarry_ooda_source_refresh_settlement as settlement
    from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as trust_enrollment_preview
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_authorization as trust_enrollment_authorization
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness as trust_enrollment_execution_readiness
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_artifact_request as trust_candidate_artifact_request
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification as trust_candidate_artifact_notification
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_import as trust_candidate_import
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_manual_action as trust_candidate_manual_action
    from scripts import propertyquarry_ooda_source_refresh_trust_notification as trust_notification

    monkeypatch.setattr(
        settlement,
        "materialize_current_settlement_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "settlement_state": "no_prior_work",
            "producer_completion_recorded": False,
            "settlement_attributed": False,
            "settlement_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "progress": {"settled_count": 0},
        },
    )
    monkeypatch.setattr(
        trust_intake,
        "materialize_trust_intake_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "intake_state": "awaiting_producer_public_key_evidence",
            "request_staged": True,
            "candidates": [],
            "action_required": False,
            "interrupt_operator": False,
            "trust_enrollment_authorized": False,
            "trust_registry_modified": False,
            "intake_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        trust_candidate_import,
        "inspect_candidate_import_readiness",
        lambda *_args, **_kwargs: {
            "status": "verified",
            "import_state": "not_required",
            "action_required": False,
            "interrupt_operator": False,
            "candidate_import_authorized": False,
            "candidate_import_attempted": False,
            "candidate_imported": False,
            "public_key_candidate_recorded": False,
            "trust_enrollment_authorized": False,
            "trust_registry_modified": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
        },
    )
    monkeypatch.setattr(
        trust_candidate_artifact_request,
        "materialize_candidate_artifact_request",
        lambda report, **_kwargs: _verified_trust_candidate_artifact_request(
            state=str(report.get("import_state") or "not_required")
        ),
    )
    monkeypatch.setattr(
        trust_candidate_manual_action,
        "materialize_candidate_manual_action",
        lambda report, _artifact, **_kwargs: (
            _verified_trust_candidate_manual_action(
                state=str(report.get("import_state") or "not_required"),
                interrupt=report.get("interrupt_operator") is True,
            )
        ),
    )
    monkeypatch.setattr(
        trust_candidate_artifact_notification,
        "run_candidate_artifact_notification",
        lambda _report, _artifact, action, **_kwargs: (
            _verified_trust_candidate_artifact_notification(
                staged=action.get("operator_action_receipt_staged") is True,
                interrupt=action.get("interrupt_operator") is True,
            )
        ),
    )
    monkeypatch.setattr(
        trust_decision,
        "verify_candidate_review_decision_for_report",
        lambda *_args, **_kwargs: {
            "status": "not_required",
            "review_state": "not_required",
            "decision": "",
            "action_required": False,
            "interrupt_operator": False,
            "trust_enrollment_preview_authorized": False,
            "trust_enrollment_authorized": False,
            "trust_registry_modified": False,
            "private_key_material_requested": False,
            "private_key_material_recorded": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "protected_operation_executed": False,
        },
    )
    monkeypatch.setattr(
        trust_notification,
        "run_candidate_notification",
        lambda *_args, **_kwargs: {
            "status": "not_required",
            "candidate_review_id": "",
            "action_required": False,
            "interrupt_operator": False,
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "presentation_recorded": False,
            "receipt_persisted": True,
            "trust_enrollment_authorized": False,
            "trust_registry_modified": False,
        },
    )
    monkeypatch.setattr(
        trust_enrollment_preview,
        "materialize_enrollment_preview",
        lambda *_args, **_kwargs: _verified_trust_enrollment_preview(),
    )
    monkeypatch.setattr(
        trust_enrollment_authorization,
        "verify_authorization_for_preview",
        lambda *_args, **_kwargs: _verified_trust_enrollment_authorization(),
    )
    monkeypatch.setattr(
        trust_enrollment_execution_readiness,
        "materialize_execution_readiness",
        lambda *_args, **_kwargs: _verified_trust_enrollment_execution_readiness(),
    )


def test_scheduler_heartbeat_file_is_healthchecked(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    runner = _load_runner_module(monkeypatch)
    from app import scheduler_healthcheck

    heartbeat_path = tmp_path / "scheduler-heartbeat.json"
    monkeypatch.setenv("EA_ROLE", "scheduler")
    monkeypatch.setenv("PROPERTYQUARRY_SCHEDULER_PROFILE", "property_only")
    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_ENABLED", "0")
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
    assert payload["property_search_work_queue"] == {"observed": False}
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


def test_role_heartbeat_loop_uses_writer_ready_status_for_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    observed: list[tuple[str, str]] = []

    class _StopAfterOneWrite:
        calls = 0

        def wait(self, timeout: float) -> bool:
            assert timeout == 30.0
            self.calls += 1
            return self.calls > 1

    monkeypatch.setattr(
        runner,
        "_write_scheduler_heartbeat",
        lambda *, role, status: observed.append((role, status)),
    )

    runner._run_role_heartbeat_loop(
        role="worker",
        stop_event=_StopAfterOneWrite(),  # type: ignore[arg-type]
    )

    assert observed == [("worker", "loop")]


def test_property_only_worker_runs_independent_process_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    loop_started = threading.Event()
    loop_stopped = threading.Event()
    container = SimpleNamespace()

    monkeypatch.setattr(runner.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(runner, "build_container", lambda: container)
    monkeypatch.setattr(
        runner,
        "_require_property_search_writer_readiness",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(runner, "_worker_property_only_profile_enabled", lambda: True)
    monkeypatch.setattr(runner, "_write_scheduler_heartbeat", lambda **_kwargs: None)

    def _heartbeat_loop(*, role: str, stop_event: threading.Event) -> None:
        assert role == "worker"
        loop_started.set()
        assert stop_event.wait(timeout=2.0)
        loop_stopped.set()

    def _work_pool(
        observed_container: object,
        *,
        role: str,
        log: logging.Logger,
        stop_event: threading.Event,
    ) -> None:
        assert observed_container is container
        assert role == "worker"
        assert isinstance(log, logging.Logger)
        assert not stop_event.is_set()
        assert loop_started.wait(timeout=2.0)

    monkeypatch.setattr(runner, "_run_role_heartbeat_loop", _heartbeat_loop)
    monkeypatch.setattr(runner, "_run_property_search_work_pool", _work_pool)

    runner._run_execution_worker("worker")

    assert loop_stopped.wait(timeout=1.0)


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


def _verified_trust_enrollment_preview(
    *,
    state: str = "not_required",
) -> dict[str, object]:
    staged = state == "preview_staged"
    return {
        "status": "verified",
        "preview_state": state,
        "preview_id": "pqtrustpreview_" + "d" * 24 if staged else "",
        "current_trust_registry_sha256": "e" * 64,
        "proposed_trust_registry_sha256": "f" * 64 if staged else "",
        "action_required": staged,
        "interrupt_operator": staged,
        "preview_staged": staged,
        "trust_enrollment_preview_authorized": staged,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "preview_receipt_persisted": True,
        "verification_receipt_persisted": True,
    }


def _verified_trust_enrollment_authorization(
    *,
    state: str = "not_required",
) -> dict[str, object]:
    pending = state == "exact_preview_authorization_pending"
    recorded = state in {
        "exact_preview_authorized",
        "exact_preview_rejected",
        "deferred",
    }
    exact = state == "exact_preview_authorized"
    return {
        "status": "pending" if pending else "verified" if recorded else "not_required",
        "authorization_state": state,
        "authorization_id": "pqtrustauth_" + "a" * 24 if recorded else "",
        "decision": "authorize_exact_preview" if exact else "",
        "action_required": pending,
        "interrupt_operator": False,
        "exact_preview_authorized": exact,
        "trust_enrollment_authorized": exact,
        "trust_registry_modified": False,
        "protected_operation_executed": False,
    }


def _verified_trust_candidate_import(
    *,
    state: str = "awaiting_external_artifact",
) -> dict[str, object]:
    presentable = state in {
        "awaiting_external_artifact",
        "external_artifact_not_admissible",
        "ready_for_manual_import",
        "recovery_required",
    }
    recovery = state == "recovery_required"
    return {
        "schema": "propertyquarry.ooda_source_refresh_trust_candidate_import_verification.v1",
        "status": "verified",
        "import_state": state,
        "request_id": "pqtrustintake_" + "f" * 24 if presentable else "",
        "action_required": presentable,
        "interrupt_operator": presentable,
        "operator_review_required": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": recovery,
        "candidate_imported": False,
        "public_key_candidate_recorded": False,
        "private_key_material_requested": False,
        "trust_enrollment_authorized": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _verified_trust_enrollment_execution_readiness(
    *,
    state: str = "not_required",
) -> dict[str, object]:
    ready = state == "ready_for_governed_execution"
    return {
        "schema": "propertyquarry.ooda_source_refresh_trust_enrollment_execution_readiness_verification.v1",
        "status": "verified",
        "readiness_state": state,
        "readiness_id": "pqtrustready_" + "b" * 24,
        "verification_receipt_sha256": "1" * 64,
        "preview_id": "pqtrustpreview_" + "d" * 24 if ready else "",
        "authorization_id": "pqtrustauth_" + "a" * 24 if ready else "",
        "authorization_receipt_sha256": "2" * 64 if ready else "",
        "authorization_decision": "authorize_exact_preview" if ready else "",
        "current_trust_registry_sha256": "e" * 64 if ready else "",
        "proposed_trust_registry_sha256": "f" * 64 if ready else "",
        "action_required": ready,
        "interrupt_operator": False,
        "operator_review_required": ready,
        "explicit_authorization_recorded": ready,
        "exact_preview_authorized": ready,
        "trust_enrollment_authorized": ready,
        "execution_request_staged": ready,
        "execution_readiness_verified": ready,
        "governed_execution_available": ready,
        "trust_registry_modified": False,
        "automatic_execution_allowed": False,
        "execution_authorized": False,
        "protected_operation_executed": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
        "readiness_receipt_persisted": True,
        "verification_receipt_persisted": True,
        "progress": {
            "current_evidence_verified": True,
            "authorization_binding_verified": True,
            "registry_binding_verified": True,
        },
    }


def _verified_trust_candidate_artifact_request(
    *,
    state: str = "not_required",
) -> dict[str, object]:
    staged = state in {
        "awaiting_external_artifact",
        "external_artifact_not_admissible",
    }
    return {
        "status": "verified",
        "request_state": state,
        "artifact_request_staged": staged,
        "artifact_request_receipt_sha256": "a" * 64,
        "action_required": staged,
        "interrupt_operator": False,
        "receipt_persisted": True,
        "producer_contacted": False,
        "transport_delivery_attempted": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _verified_trust_candidate_manual_action(
    *,
    state: str = "not_required",
    interrupt: bool = False,
) -> dict[str, object]:
    staged = state in {
        "awaiting_external_artifact",
        "external_artifact_not_admissible",
        "ready_for_manual_import",
        "recovery_required",
    }
    return {
        "status": "verified",
        "action_state": state,
        "operator_action_receipt_staged": staged,
        "action_required": staged,
        "interrupt_operator": staged and interrupt,
        "operator_action_receipt_sha256": "b" * 64,
        "receipt_persisted": True,
        "producer_contacted": False,
        "transport_delivery_authorized": False,
        "transport_delivery_attempted": False,
        "notification_sent": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "candidate_imported": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
        "delivery_authorized": False,
    }


def _verified_trust_candidate_artifact_notification(
    *,
    staged: bool = False,
    interrupt: bool = False,
) -> dict[str, object]:
    status = (
        "action_required"
        if staged and interrupt
        else "deduplicated"
        if staged
        else "not_required"
    )
    return {
        "status": status,
        "action_required": staged,
        "interrupt_operator": status == "action_required",
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": status == "action_required",
        "presentation_recorded": False,
        "notification_receipt_sha256": "c" * 64,
        "receipt_persisted": True,
        "producer_contacted": False,
        "artifact_transport_authorized": False,
        "artifact_transport_attempted": False,
        "candidate_import_authorized": False,
        "candidate_import_attempted": False,
        "trust_registry_modified": False,
        "provider_quota_consumption_allowed": False,
    }


def test_scheduler_propertyquarry_ooda_cycle_defaults_to_evaluation_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_runtime_control as runtime_control
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification as trust_candidate_artifact_notification
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_import as trust_candidate_import
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as trust_enrollment_preview

    monkeypatch.delenv("PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED", raising=False)
    monkeypatch.delenv("PROPERTYQUARRY_OODA_NOTIFICATION_PRINCIPAL_ID", raising=False)
    observed: dict[str, object] = {}
    runtime_control_observed: dict[str, object] = {}
    artifact_notification_observed: dict[str, object] = {}

    def _run_cycle_once(**kwargs):
        observed.update(kwargs)
        return {
            "schema": notification_cycle.SCHEMA,
            "status": "action_required",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": True,
            "action_required_count": 1,
            "novel_action_count": 1,
        }

    monkeypatch.setattr(notification_cycle, "run_cycle_once", _run_cycle_once)
    monkeypatch.setattr(
        runtime_control,
        "stage_host_runtime_control_handoff",
        lambda **kwargs: (
            runtime_control_observed.update(kwargs)
            or {
                "status": "host_review_required",
                "host_review_required": True,
                "current_evidence_verified": True,
                "receipt_persisted": True,
                "execution_authorized": False,
                "deployment_or_restart_authorized": False,
                "protected_operation_executed": False,
                "provider_quota_consumption_allowed": False,
                "delivery_authorized": False,
            }
        ),
    )
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "request_state": "producer_refresh_staged",
            "request_staged": True,
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "handoff_state": "producer_pickup_available",
            "handoff_available": True,
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **kwargs: {
            "status": "verified",
            "claim_state": "unclaimed",
            "settlement_state": "awaiting_current_evidence",
            "producer_claim_recorded": False,
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "progress": {"claim_count": 0},
            "observed_kwargs": kwargs,
        },
    )
    monkeypatch.setattr(
        trust_enrollment_preview,
        "materialize_enrollment_preview",
        lambda *_args, **_kwargs: _verified_trust_enrollment_preview(),
    )
    monkeypatch.setattr(
        trust_candidate_import,
        "inspect_candidate_import_readiness",
        lambda *_args, **_kwargs: _verified_trust_candidate_import(),
    )
    monkeypatch.setattr(
        trust_candidate_import,
        "apply_candidate_import_presentation_state",
        lambda report, **_kwargs: dict(report),
    )
    monkeypatch.setattr(
        trust_candidate_artifact_notification,
        "run_candidate_artifact_notification",
        lambda _report, _artifact, action, **kwargs: (
            artifact_notification_observed.update(kwargs)
            or _verified_trust_candidate_artifact_notification(
                staged=action.get("operator_action_receipt_staged") is True,
                interrupt=action.get("interrupt_operator") is True,
            )
        ),
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert observed["send"] is False
    assert observed["gold_receipt"] == (
        "/run/propertyquarry/ooda-signals/property-gold-status.json"
    )
    assert observed["public_origin_observation"] == (
        "/run/propertyquarry/ooda-signals/public-origin-observation.json"
    )
    assert observed["approval_manifest"] == (
        "/run/propertyquarry/ooda-signals/manifest.json"
    )
    assert observed["require_approval_manifest"] is True
    assert observed["write"] == (
        "/data/artifacts/propertyquarry-ooda-notification/latest.json"
    )
    assert runtime_control_observed["cycle_receipt_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/latest.json"
    )
    assert runtime_control_observed["signal_dir"] == Path(
        "/run/propertyquarry/ooda-signals"
    )
    assert runtime_control_observed["receipt_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "runtime-control-host-handoff.json"
    )
    assert artifact_notification_observed["send"] is False
    assert artifact_notification_observed["receipt_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-candidate-artifact-notification.json"
    )
    assert artifact_notification_observed[
        "manual_action_receipt_path"
    ] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-candidate-manual-action.json"
    )
    assert summary == {
        "ran": True,
        "status": "action_required",
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": True,
        "action_required_count": 1,
        "novel_action_count": 1,
        "source_refresh_settlement_status": "no_prior_work",
        "source_refresh_producer_completion_recorded": False,
        "source_refresh_settlement_attributed": False,
        "source_refresh_settled_count": 0,
        "source_refresh_settlement_receipt_persisted": True,
        "source_refresh_settlement_verification_persisted": True,
        "source_refresh_request_status": "producer_refresh_staged",
        "source_refresh_request_staged": True,
        "source_refresh_request_receipt_persisted": True,
        "source_refresh_request_verification_persisted": True,
        "source_refresh_handoff_status": "producer_pickup_available",
        "source_refresh_handoff_available": True,
        "source_refresh_handoff_receipt_persisted": True,
        "source_refresh_handoff_verification_persisted": True,
        "source_refresh_claims_status": "unclaimed",
        "source_refresh_claims_settlement_status": "awaiting_current_evidence",
        "source_refresh_producer_claim_recorded": False,
        "source_refresh_claim_count": 0,
        "source_refresh_claims_receipt_persisted": True,
        "source_refresh_claims_verification_persisted": True,
        "source_refresh_trust_intake_status": (
            "awaiting_producer_public_key_evidence"
        ),
        "source_refresh_trust_intake_request_staged": True,
        "source_refresh_trust_candidate_count": 0,
        "source_refresh_trust_intake_receipt_persisted": True,
        "source_refresh_trust_intake_verification_persisted": True,
        "source_refresh_trust_decision_status": "not_required",
        "source_refresh_trust_review_state": "not_required",
        "source_refresh_trust_decision": "",
        "source_refresh_trust_action_required": False,
        "source_refresh_trust_interrupt_operator": False,
        "source_refresh_trust_enrollment_preview_authorized": False,
        "source_refresh_trust_enrollment_authorized": False,
        "source_refresh_trust_registry_modified": False,
        "source_refresh_trust_intake_verified": True,
        "source_refresh_trust_candidate_import_status": "verified",
        "source_refresh_trust_candidate_import_state": (
            "awaiting_external_artifact"
        ),
        "source_refresh_trust_candidate_import_request_id": (
            "pqtrustintake_" + "f" * 24
        ),
        "source_refresh_trust_candidate_import_action_required": True,
        "source_refresh_trust_candidate_import_interrupt_operator": True,
        "source_refresh_trust_candidate_import_authorized": False,
        "source_refresh_trust_candidate_import_attempted": False,
        "source_refresh_trust_candidate_imported": False,
            "source_refresh_trust_candidate_import_verified": True,
            "source_refresh_trust_candidate_artifact_request_status": (
                "verified"
            ),
            "source_refresh_trust_candidate_artifact_request_state": (
                "awaiting_external_artifact"
            ),
            "source_refresh_trust_candidate_artifact_request_staged": True,
            "source_refresh_trust_candidate_artifact_request_receipt_sha256": (
                "a" * 64
            ),
            "source_refresh_trust_candidate_artifact_request_receipt_persisted": (
                True
            ),
            "source_refresh_trust_candidate_artifact_request_verified": True,
            "source_refresh_trust_candidate_manual_action_status": "verified",
            "source_refresh_trust_candidate_manual_action_state": (
                "awaiting_external_artifact"
            ),
            "source_refresh_trust_candidate_manual_action_staged": True,
            "source_refresh_trust_candidate_manual_action_required": True,
            "source_refresh_trust_candidate_manual_action_interrupt_operator": (
                True
            ),
            "source_refresh_trust_candidate_manual_action_receipt_sha256": (
                "b" * 64
            ),
            "source_refresh_trust_candidate_manual_action_receipt_persisted": (
                True
            ),
            "source_refresh_trust_candidate_manual_action_verified": True,
            "source_refresh_trust_candidate_artifact_notification_status": (
                "action_required"
            ),
            "source_refresh_trust_candidate_artifact_notification_action_required": (
                True
            ),
            "source_refresh_trust_candidate_artifact_notification_interrupt_operator": (
                True
            ),
            "source_refresh_trust_candidate_artifact_notification_delivery_authorized": (
                False
            ),
            "source_refresh_trust_candidate_artifact_notification_delivery_attempted": (
                False
            ),
            "source_refresh_trust_candidate_artifact_notification_sent": False,
            "source_refresh_trust_candidate_artifact_notification_would_send": (
                True
            ),
            "source_refresh_trust_candidate_artifact_notification_presentation_recorded": (
                False
            ),
            "source_refresh_trust_candidate_artifact_notification_receipt_sha256": (
                "c" * 64
            ),
            "source_refresh_trust_candidate_artifact_notification_receipt_persisted": (
                True
            ),
            "source_refresh_trust_candidate_artifact_notification_verified": (
                True
            ),
            "source_refresh_trust_decision_verified": True,
        "source_refresh_trust_notification_status": "not_required",
        "source_refresh_trust_notification_candidate_review_id": "",
        "source_refresh_trust_notification_delivery_authorized": False,
        "source_refresh_trust_notification_delivery_attempted": False,
        "source_refresh_trust_notification_sent": False,
        "source_refresh_trust_notification_would_send": False,
        "source_refresh_trust_notification_presentation_recorded": False,
        "source_refresh_trust_notification_receipt_persisted": True,
        "source_refresh_trust_notification_verified": True,
        "source_refresh_trust_enrollment_preview_status": "not_required",
        "source_refresh_trust_enrollment_preview_id": "",
        "source_refresh_trust_enrollment_preview_action_required": False,
        "source_refresh_trust_enrollment_preview_interrupt_operator": False,
        "source_refresh_trust_enrollment_preview_staged": False,
        "source_refresh_trust_enrollment_preview_current_registry_sha256": (
            "e" * 64
        ),
        "source_refresh_trust_enrollment_preview_proposed_registry_sha256": "",
        "source_refresh_trust_enrollment_preview_receipt_persisted": True,
        "source_refresh_trust_enrollment_preview_verification_persisted": True,
        "source_refresh_trust_enrollment_preview_verified": True,
        "source_refresh_trust_enrollment_authorization_status": (
            "not_required"
        ),
        "source_refresh_trust_enrollment_authorization_state": (
            "not_required"
        ),
        "source_refresh_trust_enrollment_authorization_id": "",
        "source_refresh_trust_enrollment_authorization_decision": "",
        "source_refresh_trust_enrollment_authorization_action_required": False,
        "source_refresh_trust_exact_preview_authorized": False,
        "source_refresh_trust_enrollment_authorization_verified": True,
        "source_refresh_trust_enrollment_execution_readiness_status": (
            "verified"
        ),
        "source_refresh_trust_enrollment_execution_readiness_state": (
            "not_required"
        ),
        "source_refresh_trust_enrollment_execution_readiness_id": (
            "pqtrustready_" + "b" * 24
        ),
        "source_refresh_trust_enrollment_execution_request_staged": False,
        "source_refresh_trust_enrollment_execution_ready": False,
        "source_refresh_trust_enrollment_governed_execution_available": False,
        "source_refresh_trust_enrollment_execution_authorized": False,
        "source_refresh_trust_enrollment_execution_readiness_verified": True,
        "source_refresh_trust_enrollment_execution_status": "verified",
        "source_refresh_trust_enrollment_execution_state": "not_required",
        "source_refresh_trust_enrollment_execution_action_required": False,
        "source_refresh_trust_enrollment_execution_interrupt_operator": False,
        "source_refresh_trust_enrollment_authorization_consumed": False,
        "source_refresh_trust_enrollment_registry_write_attempted": False,
        "source_refresh_trust_enrollment_registry_modified": False,
        "source_refresh_trust_enrollment_rollback_available": False,
        "source_refresh_trust_enrollment_execution_verified": True,
        "runtime_control_handoff_status": "host_review_required",
        "runtime_control_host_review_required": True,
        "runtime_control_current_evidence_verified": True,
        "runtime_control_receipt_persisted": True,
        "runtime_control_execution_authorized": False,
        "runtime_control_deployment_or_restart_authorized": False,
        "runtime_control_protected_operation_executed": False,
        "runtime_control_provider_quota_consumption_allowed": False,
        "runtime_control_delivery_authorized": False,
        "errors": 0,
    }


def test_scheduler_propertyquarry_ooda_materializes_current_trust_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
    from scripts import propertyquarry_ooda_source_refresh_trust_decision as trust_decision
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_preview as trust_enrollment_preview
    from scripts import propertyquarry_ooda_source_refresh_trust_enrollment_execution_readiness as trust_enrollment_execution_readiness
    from scripts import propertyquarry_ooda_source_refresh_trust_intake as trust_intake
    from scripts import propertyquarry_ooda_source_refresh_trust_notification as trust_notification

    monkeypatch.delenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED",
        raising=False,
    )
    monkeypatch.setattr(
        notification_cycle,
        "run_cycle_once",
        lambda **_kwargs: {
            "status": "silent",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
    )
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "request_state": "producer_refresh_staged",
            "request_staged": True,
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "handoff_state": "producer_pickup_available",
            "handoff_available": True,
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "claim_state": "producer_trust_unconfigured",
            "settlement_state": "awaiting_producer_trust",
            "producer_claim_recorded": False,
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "progress": {"claim_count": 0},
        },
    )
    review_id = "pqtrustreview_" + "a" * 24
    verification_sha256 = "b" * 64
    observed: dict[str, object] = {}

    def materialize_trust(**kwargs):
        observed["intake_kwargs"] = kwargs
        return {
            "status": "verified",
            "intake_state": "candidate_ready_for_operator_review",
            "request_staged": True,
            "candidate_review_id": review_id,
            "verification_receipt_sha256": verification_sha256,
            "candidates": [{"public_key_sha256": "c" * 64}],
            "action_required": True,
            "interrupt_operator": True,
            "operator_review_required": True,
            "trust_enrollment_authorized": False,
            "trust_registry_modified": False,
            "intake_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }

    monkeypatch.setattr(
        trust_intake,
        "materialize_trust_intake_bundle",
        materialize_trust,
    )

    def verify_decision(report, **kwargs):
        observed["decision_report"] = report
        observed["decision_kwargs"] = kwargs
        return {
            "status": "pending",
            "review_state": "candidate_review_pending",
            "candidate_review_id": review_id,
            "trust_intake_verification_sha256": verification_sha256,
            "decision": "",
            "action_required": True,
            "interrupt_operator": False,
            "operator_review_required": True,
            "trust_enrollment_preview_authorized": False,
            "trust_enrollment_authorized": False,
            "trust_registry_modified": False,
            "private_key_material_requested": False,
            "private_key_material_recorded": False,
            "provider_quota_consumption_allowed": False,
            "delivery_authorized": False,
            "protected_operation_executed": False,
        }

    monkeypatch.setattr(
        trust_decision,
        "verify_candidate_review_decision_for_report",
        verify_decision,
    )
    monkeypatch.setattr(
        trust_notification,
        "run_candidate_notification",
        lambda intake_report, decision_report, **kwargs: (
            observed.update(
                {
                    "notification_intake": intake_report,
                    "notification_decision": decision_report,
                    "notification_kwargs": kwargs,
                }
            )
            or {
                "status": "action_required",
                "candidate_review_id": review_id,
                "action_required": True,
                "interrupt_operator": True,
                "delivery_authorized": False,
                "delivery_attempted": False,
                "sent": False,
                "would_send": True,
                "presentation_recorded": False,
                "receipt_persisted": True,
                "trust_enrollment_authorized": False,
                "trust_registry_modified": False,
            }
        ),
    )
    monkeypatch.setattr(
        trust_enrollment_preview,
        "materialize_enrollment_preview",
        lambda intake_report, decision_report, **kwargs: (
            observed.update(
                {
                    "preview_intake": intake_report,
                    "preview_decision": decision_report,
                    "preview_kwargs": kwargs,
                }
            )
            or _verified_trust_enrollment_preview(
                state="awaiting_decision"
            )
        ),
    )
    monkeypatch.setattr(
        trust_enrollment_execution_readiness,
        "materialize_execution_readiness",
        lambda preview_report, authorization_report, **kwargs: (
            observed.update(
                {
                    "readiness_preview": preview_report,
                    "readiness_authorization": authorization_report,
                    "readiness_kwargs": kwargs,
                }
            )
            or _verified_trust_enrollment_execution_readiness()
        ),
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    intake_kwargs = dict(observed["intake_kwargs"])
    decision_kwargs = dict(observed["decision_kwargs"])
    notification_kwargs = dict(observed["notification_kwargs"])
    preview_kwargs = dict(observed["preview_kwargs"])
    readiness_kwargs = dict(observed["readiness_kwargs"])
    assert intake_kwargs["claim_verification_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-claims-verification.json"
    )
    assert intake_kwargs["candidate_dir"] == Path(
        "/run/propertyquarry/ooda-producer-trust-candidates"
    )
    assert intake_kwargs["receipt_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-intake.json"
    )
    assert intake_kwargs["verification_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-intake-verification.json"
    )
    assert decision_kwargs["decision_dir"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-candidate-decisions"
    )
    assert notification_kwargs["presentation_state_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-candidate-presentation.json"
    )
    assert notification_kwargs["receipt_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-candidate-notification.json"
    )
    assert preview_kwargs["receipt_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-enrollment-preview.json"
    )
    assert preview_kwargs["verification_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-enrollment-preview-verification.json"
    )
    assert readiness_kwargs["receipt_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-enrollment-execution-readiness.json"
    )
    assert readiness_kwargs["verification_path"] == Path(
        "/data/artifacts/propertyquarry-ooda-notification/"
        "source-refresh-trust-enrollment-execution-readiness-verification.json"
    )
    assert dict(observed["readiness_preview"]).get("preview_state") == (
        "awaiting_decision"
    )
    assert dict(observed["readiness_authorization"]).get("status") == (
        "not_required"
    )
    assert summary["source_refresh_trust_intake_status"] == (
        "candidate_ready_for_operator_review"
    )
    assert summary["source_refresh_trust_decision_status"] == "pending"
    assert summary["source_refresh_trust_action_required"] is True
    assert summary["source_refresh_trust_interrupt_operator"] is True
    assert summary["source_refresh_trust_enrollment_authorized"] is False
    assert summary["source_refresh_trust_registry_modified"] is False
    assert summary["source_refresh_trust_notification_status"] == (
        "action_required"
    )
    assert summary[
        "source_refresh_trust_notification_candidate_review_id"
    ] == review_id
    assert summary["source_refresh_trust_notification_would_send"] is True
    assert summary["source_refresh_trust_notification_sent"] is False
    assert summary["source_refresh_trust_enrollment_preview_status"] == (
        "awaiting_decision"
    )
    assert summary["source_refresh_trust_enrollment_preview_staged"] is False
    assert summary["errors"] == 0


def test_scheduler_propertyquarry_ooda_trust_alert_failure_blocks_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
    from scripts import propertyquarry_ooda_source_refresh_trust_notification as trust_notification

    monkeypatch.delenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED",
        raising=False,
    )
    monkeypatch.setattr(
        notification_cycle,
        "run_cycle_once",
        lambda **_kwargs: {
            "status": "silent",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
    )
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "request_state": "producer_refresh_staged",
            "request_staged": True,
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "handoff_state": "producer_pickup_available",
            "handoff_available": True,
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "claim_state": "unclaimed",
            "settlement_state": "awaiting_current_evidence",
            "producer_claim_recorded": False,
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "progress": {"claim_count": 0},
        },
    )
    monkeypatch.setattr(
        trust_notification,
        "run_candidate_notification",
        lambda *_args, **_kwargs: {
            "status": "delivery_failed",
            "candidate_review_id": "pqtrustreview_" + "a" * 24,
            "action_required": True,
            "interrupt_operator": False,
            "delivery_authorized": True,
            "delivery_attempted": True,
            "sent": False,
            "would_send": False,
            "presentation_recorded": False,
            "receipt_persisted": True,
            "trust_enrollment_authorized": False,
            "trust_registry_modified": False,
        },
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert summary["source_refresh_trust_notification_status"] == (
        "delivery_failed"
    )
    assert summary[
        "source_refresh_trust_notification_delivery_attempted"
    ] is True
    assert summary["source_refresh_trust_notification_sent"] is False
    assert summary["source_refresh_trust_notification_verified"] is False
    assert summary["errors"] == 1


def test_scheduler_propertyquarry_ooda_artifact_alert_failure_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_artifact_notification as artifact_notification
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_import as candidate_import

    monkeypatch.delenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED",
        raising=False,
    )
    monkeypatch.setattr(
        notification_cycle,
        "run_cycle_once",
        lambda **_kwargs: {
            "status": "silent",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
    )
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "request_state": "producer_refresh_staged",
            "request_staged": True,
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "handoff_state": "producer_pickup_available",
            "handoff_available": True,
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "claim_state": "unclaimed",
            "settlement_state": "awaiting_current_evidence",
            "producer_claim_recorded": False,
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "progress": {"claim_count": 0},
        },
    )
    monkeypatch.setattr(
        candidate_import,
        "inspect_candidate_import_readiness",
        lambda *_args, **_kwargs: _verified_trust_candidate_import(),
    )
    monkeypatch.setattr(
        candidate_import,
        "apply_candidate_import_presentation_state",
        lambda report, **_kwargs: dict(report),
    )
    monkeypatch.setattr(
        artifact_notification,
        "run_candidate_artifact_notification",
        lambda *_args, **_kwargs: {
            **_verified_trust_candidate_artifact_notification(
                staged=True,
                interrupt=False,
            ),
            "status": "delivery_failed",
            "blocking_reason": "RuntimeError",
            "delivery_authorized": True,
            "delivery_attempted": True,
            "receipt_persisted": True,
        },
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert summary[
        "source_refresh_trust_candidate_artifact_notification_status"
    ] == "delivery_failed"
    assert summary[
        "source_refresh_trust_candidate_artifact_notification_delivery_attempted"
    ] is True
    assert summary[
        "source_refresh_trust_candidate_artifact_notification_sent"
    ] is False
    assert summary[
        "source_refresh_trust_candidate_artifact_notification_verified"
    ] is False
    assert summary["errors"] == 1


def test_scheduler_propertyquarry_ooda_stages_import_recovery_alert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
    from scripts import propertyquarry_ooda_source_refresh_trust_candidate_import as candidate_import

    monkeypatch.delenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED",
        raising=False,
    )
    monkeypatch.setattr(
        notification_cycle,
        "run_cycle_once",
        lambda **_kwargs: {
            "status": "silent",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
    )
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "request_state": "producer_refresh_staged",
            "request_staged": True,
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "handoff_state": "producer_pickup_available",
            "handoff_available": True,
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "claim_state": "unclaimed",
            "settlement_state": "awaiting_current_evidence",
            "producer_claim_recorded": False,
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "progress": {"claim_count": 0},
        },
    )
    monkeypatch.setattr(
        candidate_import,
        "inspect_candidate_import_readiness",
        lambda *_args, **_kwargs: _verified_trust_candidate_import(
            state="recovery_required"
        ),
    )
    monkeypatch.setattr(
        candidate_import,
        "apply_candidate_import_presentation_state",
        lambda report, **_kwargs: dict(report),
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert summary["source_refresh_trust_candidate_import_state"] == (
        "recovery_required"
    )
    assert summary[
        "source_refresh_trust_candidate_manual_action_staged"
    ] is True
    assert summary[
        "source_refresh_trust_candidate_manual_action_interrupt_operator"
    ] is True
    assert summary[
        "source_refresh_trust_candidate_artifact_notification_status"
    ] == "action_required"
    assert summary[
        "source_refresh_trust_candidate_artifact_notification_would_send"
    ] is True
    assert summary[
        "source_refresh_trust_candidate_artifact_notification_sent"
    ] is False
    assert summary["errors"] == 0


def test_scheduler_propertyquarry_ooda_send_requires_explicit_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle

    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED", "1")
    monkeypatch.delenv("PROPERTYQUARRY_OODA_NOTIFICATION_PRINCIPAL_ID", raising=False)
    monkeypatch.setattr(
        notification_cycle,
        "run_cycle_once",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("incomplete delivery authority must fail before cycle execution")
        ),
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert summary == {
        "ran": False,
        "status": "delivery_authority_incomplete",
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": False,
        "action_required_count": 0,
        "novel_action_count": 0,
        "errors": 1,
    }


def test_scheduler_source_settlement_failure_preserves_prior_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh
    from scripts import propertyquarry_ooda_source_refresh_settlement as settlement

    monkeypatch.delenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED",
        raising=False,
    )
    monkeypatch.setattr(
        notification_cycle,
        "run_cycle_once",
        lambda **_kwargs: {
            "status": "silent",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
    )
    monkeypatch.setattr(
        settlement,
        "materialize_current_settlement_bundle",
        lambda **_kwargs: {
            "status": "blocked",
            "settlement_state": "blocked",
            "producer_completion_recorded": False,
            "settlement_attributed": False,
            "settlement_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    must_not_replace = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("a blocked settlement must preserve the prior chain")
    )
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        must_not_replace,
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        must_not_replace,
    )
    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        must_not_replace,
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert summary["status"] == "silent"
    assert summary["source_refresh_settlement_status"] == "blocked"
    assert summary["source_refresh_request_status"] == "settlement_blocked"
    assert summary["source_refresh_handoff_status"] == (
        "settlement_or_request_blocked"
    )
    assert summary["source_refresh_claims_status"] == (
        "settlement_or_handoff_blocked"
    )
    assert summary["errors"] == 1
    assert summary["delivery_attempted"] is False
    assert summary["sent"] is False


def test_scheduler_propertyquarry_ooda_refresh_request_failure_blocks_witness_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh

    monkeypatch.delenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED",
        raising=False,
    )
    monkeypatch.setattr(
        notification_cycle,
        "run_cycle_once",
        lambda **_kwargs: {
            "status": "silent",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
    )
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "status": "blocked",
            "request_state": "blocked",
            "request_staged": False,
            "request_receipt_persisted": False,
            "verification_receipt_persisted": False,
        },
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "status": "blocked",
            "handoff_state": "blocked",
            "handoff_available": False,
            "handoff_receipt_persisted": False,
            "verification_receipt_persisted": False,
        },
    )
    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: {
            "status": "blocked",
            "claim_state": "blocked",
            "settlement_state": "unverified",
            "producer_claim_recorded": False,
            "lifecycle_receipt_persisted": False,
            "verification_receipt_persisted": False,
        },
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert summary["status"] == "silent"
    assert summary["source_refresh_request_status"] == "blocked"
    assert summary["source_refresh_request_staged"] is False
    assert summary["source_refresh_request_receipt_persisted"] is False
    assert summary["source_refresh_request_verification_persisted"] is False
    assert summary["source_refresh_handoff_status"] == (
        "settlement_or_request_blocked"
    )
    assert summary["source_refresh_handoff_available"] is False
    assert summary["errors"] == 1
    assert summary["delivery_authorized"] is False
    assert summary["delivery_attempted"] is False
    assert summary["sent"] is False


def test_scheduler_propertyquarry_ooda_handoff_failure_blocks_witness_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh

    monkeypatch.delenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED",
        raising=False,
    )
    monkeypatch.setattr(
        notification_cycle,
        "run_cycle_once",
        lambda **_kwargs: {
            "status": "silent",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
    )
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "request_state": "producer_refresh_staged",
            "request_staged": True,
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "status": "blocked",
            "handoff_state": "blocked",
            "handoff_available": False,
            "handoff_receipt_persisted": False,
            "verification_receipt_persisted": False,
        },
    )
    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: {
            "status": "blocked",
            "claim_state": "blocked",
            "settlement_state": "unverified",
            "producer_claim_recorded": False,
            "lifecycle_receipt_persisted": False,
            "verification_receipt_persisted": False,
        },
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert summary["status"] == "silent"
    assert summary["source_refresh_request_status"] == (
        "producer_refresh_staged"
    )
    assert summary["source_refresh_handoff_status"] == "blocked"
    assert summary["source_refresh_handoff_available"] is False
    assert summary["source_refresh_handoff_receipt_persisted"] is False
    assert summary["source_refresh_handoff_verification_persisted"] is False
    assert summary["errors"] == 1
    assert summary["delivery_authorized"] is False
    assert summary["delivery_attempted"] is False
    assert summary["sent"] is False


def test_scheduler_propertyquarry_ooda_claim_failure_blocks_witness_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh

    monkeypatch.delenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED",
        raising=False,
    )
    monkeypatch.setattr(
        notification_cycle,
        "run_cycle_once",
        lambda **_kwargs: {
            "status": "silent",
            "delivery_authorized": False,
            "delivery_attempted": False,
            "sent": False,
            "would_send": False,
            "action_required_count": 0,
            "novel_action_count": 0,
        },
    )
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "request_state": "producer_refresh_staged",
            "request_staged": True,
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "handoff_state": "producer_pickup_available",
            "handoff_available": True,
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    observed: dict[str, object] = {}

    def blocked_claims(**kwargs):
        observed.update(kwargs)
        return {
            "status": "blocked",
            "claim_state": "blocked",
            "settlement_state": "unverified",
            "producer_claim_recorded": False,
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
        }

    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        blocked_claims,
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert observed["require_claim_dir"] is True
    assert str(observed["claim_dir"]) == (
        "/run/propertyquarry/ooda-producer-claims"
    )
    assert summary["source_refresh_claims_status"] == "blocked"
    assert summary["source_refresh_claims_settlement_status"] == "unverified"
    assert summary["source_refresh_producer_claim_recorded"] is False
    assert summary["source_refresh_claims_receipt_persisted"] is True
    assert summary["errors"] == 1
    assert summary["delivery_authorized"] is False
    assert summary["delivery_attempted"] is False
    assert summary["sent"] is False


def test_scheduler_propertyquarry_ooda_send_requires_both_explicit_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_notification_cycle as notification_cycle
    from scripts import propertyquarry_ooda_source_refresh_claims as source_claims
    from scripts import propertyquarry_ooda_source_refresh_handoff as source_handoff
    from scripts import propertyquarry_ooda_source_refresh_request as source_refresh

    monkeypatch.setenv("PROPERTYQUARRY_OODA_NOTIFICATION_SEND_ENABLED", "1")
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_PRINCIPAL_ID",
        "propertyquarry-operator-test",
    )
    observed: dict[str, object] = {}

    def _run_cycle_once(**kwargs):
        observed.update(kwargs)
        return {
            "status": "completed",
            "delivery_authorized": True,
            "delivery_attempted": True,
            "sent": True,
            "would_send": False,
            "action_required_count": 1,
            "novel_action_count": 1,
        }

    monkeypatch.setattr(notification_cycle, "run_cycle_once", _run_cycle_once)
    monkeypatch.setattr(
        source_refresh,
        "materialize_current_source_refresh_request_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "request_state": "current_sources_verified",
            "request_staged": False,
            "request_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_handoff,
        "materialize_current_source_refresh_handoff_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "handoff_state": "current_sources_verified",
            "handoff_available": False,
            "handoff_receipt_persisted": True,
            "verification_receipt_persisted": True,
        },
    )
    monkeypatch.setattr(
        source_claims,
        "materialize_current_claim_lifecycle_bundle",
        lambda **_kwargs: {
            "status": "verified",
            "claim_state": "not_required",
            "settlement_state": "current_evidence_verified",
            "producer_claim_recorded": False,
            "lifecycle_receipt_persisted": True,
            "verification_receipt_persisted": True,
            "progress": {"claim_count": 0},
        },
    )

    summary = runner._run_scheduler_propertyquarry_ooda_notification_cycle(
        logging.getLogger("test.runner")
    )

    assert observed["send"] is True
    assert observed["principal_id"] == "propertyquarry-operator-test"
    assert summary["delivery_authorized"] is True
    assert summary["delivery_attempted"] is True
    assert summary["sent"] is True
    assert summary["errors"] == 0


def test_scheduler_propertyquarry_ooda_bounds_interval_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    monkeypatch.setenv("EA_SCHEDULER_PROPERTYQUARRY_OODA_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("EA_SCHEDULER_PROPERTYQUARRY_OODA_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("EA_SCHEDULER_PROPERTYQUARRY_OODA_APPROVAL_MAX_AGE_SECONDS", "999999")

    assert runner._scheduler_propertyquarry_ooda_interval_seconds() == 60.0
    assert runner._scheduler_propertyquarry_ooda_timeout_seconds() == 15.0
    assert runner._scheduler_propertyquarry_ooda_approval_max_age_seconds() == 86400.0


def test_property_only_scheduler_loop_wires_ooda_cycle_through_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    source = inspect.getsource(runner._run_execution_worker)

    assert "property_only_scheduler and _scheduler_propertyquarry_ooda_enabled()" in source
    assert 'step_name="propertyquarry_ooda_notification_cycle"' in source
    assert "_scheduler_propertyquarry_ooda_timeout_seconds()" in source
    assert "_record_scheduler_propertyquarry_ooda_iteration(" in source
    assert '"status": "iteration_exception"' in source


def test_scheduler_propertyquarry_ooda_records_cycle_bound_iteration_witness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_scheduler_witness as scheduler_witness

    cycle_path = tmp_path / "latest.json"
    witness_path = tmp_path / "scheduler-iteration.json"
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_NOTIFICATION_RECEIPT_PATH",
        str(cycle_path),
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_OODA_SCHEDULER_ITERATION_RECEIPT_PATH",
        str(witness_path),
    )
    observed: dict[str, object] = {}

    def persist(summary, **kwargs):
        observed["summary"] = dict(summary)
        observed.update(kwargs)
        return {
            "status": "completed",
            "receipt_persisted": True,
        }

    monkeypatch.setattr(
        scheduler_witness,
        "persist_scheduler_iteration_receipt",
        persist,
    )
    summary = {
        "ran": True,
        "status": "silent",
        "delivery_authorized": False,
        "delivery_attempted": False,
        "sent": False,
        "would_send": False,
        "action_required_count": 0,
        "novel_action_count": 0,
        "errors": 0,
    }

    result = runner._record_scheduler_propertyquarry_ooda_iteration(
        summary,
        log=logging.getLogger("test.runner"),
    )

    assert observed["summary"] == summary
    assert observed["receipt_path"] == witness_path
    assert observed["cycle_receipt_path"] == cycle_path
    assert observed["error_type"] == ""
    assert result == {
        "status": "completed",
        "receipt_persisted": True,
        "persistent_reevaluation_verified": False,
    }


def test_scheduler_propertyquarry_ooda_witness_failure_does_not_grant_continuity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_runner_module(monkeypatch)
    from scripts import propertyquarry_ooda_scheduler_witness as scheduler_witness

    monkeypatch.setattr(
        scheduler_witness,
        "persist_scheduler_iteration_receipt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("witness unavailable")
        ),
    )

    result = runner._record_scheduler_propertyquarry_ooda_iteration(
        {
            "ran": False,
            "status": "iteration_exception",
            "errors": 1,
        },
        log=logging.getLogger("test.runner"),
        error_type="RuntimeError",
    )

    assert result == {
        "status": "unavailable",
        "receipt_persisted": False,
        "persistent_reevaluation_verified": False,
    }
