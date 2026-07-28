from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import signal
import threading
import time
import urllib.error
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import uvicorn

from app.api.routes.channels import (
    _resolve_telegram_bot_config,
    _telegram_async_assistant_reply_worker,
)
from app.container import build_container
from app.logging_utils import configure_logging, log_event
from app.observability import (
    bind_runtime_trace_context,
    child_trace_context,
    runtime_build_identity,
    runtime_trace_context_from_mapping,
)
from app.product.property_research_packet_fleet_proof import (
    PROPERTY_RESEARCH_PACKET_WRITER_READY_STATUSES,
)
from app.settings import get_settings

_IDLE_BACKOFF_START_SECONDS = 1.0
_IDLE_BACKOFF_MAX_SECONDS = 15.0
_ERROR_BACKOFF_SECONDS = 2.0
_SCHEDULER_SCAN_INTERVAL_SECONDS = 900.0
_SCHEDULER_ONEMIN_REFRESH_INTERVAL_SECONDS = 86400.0
_SCHEDULER_GOOGLE_SIGNAL_SYNC_INTERVAL_SECONDS = 900.0
_SCHEDULER_POCKET_SIGNAL_SYNC_INTERVAL_SECONDS = 900.0
_SCHEDULER_MORNING_MEMO_INTERVAL_SECONDS = 300.0
_SCHEDULER_TELEGRAM_ASYNC_RECOVERY_INTERVAL_SECONDS = 5.0
_SCHEDULER_TELEGRAM_ASYNC_RECOVERY_MIN_AGE_SECONDS = 0.0
_SCHEDULER_MORNING_MEMO_DELIVERY_WINDOW_MINUTES = 120
_SCHEDULER_MORNING_MEMO_RETRY_AFTER_MINUTES = 60
_SCHEDULER_MORNING_MEMO_LEASE_SECONDS = 900
_SCHEDULER_MORNING_MEMO_MAX_ATTEMPTS = 3
_SCHEDULER_GOOGLE_SIGNAL_SYNC_FORBIDDEN_COOLDOWNS: dict[str, float] = {}
_SCHEDULER_STEP_LOCK = threading.Lock()
_SCHEDULER_STEP_THREADS: dict[str, dict[str, object]] = {}
_EXECUTION_INSTANCE_ID = str(os.environ.get("EA_EXECUTION_INSTANCE_ID") or uuid4().hex).strip()
_EXECUTION_STARTED_AT_EPOCH = time.time()
_SCHEDULER_DELIVERY_METRICS_LOCK = threading.Lock()
_PROPERTY_SEARCH_WORK_QUEUE_METRICS_LOCK = threading.Lock()
_PROPERTY_SEARCH_WORK_QUEUE_METRICS: dict[str, object] = {"observed": False}
_MAX_PROPERTY_SEARCH_QUEUE_DEPTH = (2**63) - 1
_MAX_PROPERTY_SEARCH_QUEUE_AGE_SECONDS = 10 * 365 * 24 * 60 * 60
_SCHEDULER_PROPERTY_MAINTENANCE_ROTATION_LOCK = threading.Lock()
_SCHEDULER_PROPERTY_MAINTENANCE_ROTATION = 0
_SCHEDULER_DELIVERY_METRICS = {
    key: 0
    for key in (
        "queued",
        "claimed",
        "claim_conflicts",
        "sent",
        "retried",
        "dead_lettered",
        "failed",
    )
}
_PROPERTY_SEARCH_QUEUE_METRICS_LOCK = threading.Lock()
_PROPERTY_SEARCH_QUEUE_METRICS: dict[str, object] = {"observed": False}


def _record_property_search_queue_metrics(snapshot: object | None) -> bool:
    observed = False
    next_snapshot: dict[str, object] = {"observed": False}
    if snapshot is not None:
        try:
            raw_depth = getattr(snapshot, "depth")
            raw_oldest_age = getattr(snapshot, "oldest_item_age_seconds")
            if type(raw_depth) is not int or isinstance(raw_oldest_age, bool):
                raise ValueError("property_search_queue_snapshot_invalid")
            depth = raw_depth
            oldest_age = float(raw_oldest_age)
            if (
                depth < 0
                or depth > 2**63 - 1
                or not math.isfinite(oldest_age)
                or oldest_age < 0
                or oldest_age > 10 * 365 * 24 * 60 * 60
            ):
                raise ValueError("property_search_queue_snapshot_invalid")
            next_snapshot = {
                "observed": True,
                "depth": depth,
                "oldest_item_age_seconds": oldest_age,
            }
            observed = True
        except (AttributeError, TypeError, ValueError, OverflowError):
            pass
    with _PROPERTY_SEARCH_QUEUE_METRICS_LOCK:
        _PROPERTY_SEARCH_QUEUE_METRICS.clear()
        _PROPERTY_SEARCH_QUEUE_METRICS.update(next_snapshot)
    return observed


def _property_search_queue_metrics_snapshot() -> dict[str, object]:
    with _PROPERTY_SEARCH_QUEUE_METRICS_LOCK:
        return dict(_PROPERTY_SEARCH_QUEUE_METRICS)


def _refresh_property_search_queue_metrics(repository: object) -> bool:
    try:
        snapshot = repository.observability_snapshot()  # type: ignore[attr-defined]
    except Exception:
        snapshot = None
    return _record_property_search_queue_metrics(snapshot)


def _property_search_writer_contract() -> dict[str, int]:
    """Version receipt shared by workers so mixed deployments are observable."""
    from app.product.property_research_packet_links import (
        PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
        PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
    )
    from app.product.property_search_schema import LATEST_PROPERTY_SEARCH_SCHEMA_VERSION
    from app.product.property_search_storage import _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION

    return {
        "compact_schema_version": _PROPERTY_SEARCH_RUN_COMPACT_SCHEMA_VERSION,
        "research_packet_schema_version": PROPERTY_RESEARCH_PACKET_SCHEMA_VERSION,
        "writer_contract_version": PROPERTY_RESEARCH_PACKET_WRITER_CONTRACT_VERSION,
        "property_search_schema_version": LATEST_PROPERTY_SEARCH_SCHEMA_VERSION,
    }


def _scheduler_log_ref(value: object) -> str:
    """Return a process-local correlation reference without logging identity data."""

    normalized = str(value or "").strip().lower()
    if not normalized:
        return "none"
    digest = hashlib.sha256(f"{_EXECUTION_INSTANCE_ID}\0{normalized}".encode("utf-8")).hexdigest()
    return f"ref:{digest[:12]}"


def _record_scheduler_delivery_metrics(summary: dict[str, object]) -> None:
    with _SCHEDULER_DELIVERY_METRICS_LOCK:
        for key in tuple(_SCHEDULER_DELIVERY_METRICS):
            _SCHEDULER_DELIVERY_METRICS[key] += max(0, int(summary.get(key) or 0))


def _scheduler_delivery_metrics_snapshot() -> dict[str, int]:
    with _SCHEDULER_DELIVERY_METRICS_LOCK:
        return dict(_SCHEDULER_DELIVERY_METRICS)


def _record_property_search_work_queue_metrics(snapshot: object) -> None:
    raw_depth = getattr(snapshot, "depth")
    raw_oldest_item_age_seconds = getattr(snapshot, "oldest_item_age_seconds")
    if (
        type(raw_depth) is not int
        or isinstance(raw_oldest_item_age_seconds, bool)
        or not isinstance(raw_oldest_item_age_seconds, (int, float))
    ):
        raise ValueError("property_search_work_queue_snapshot_invalid")
    depth = raw_depth
    oldest_item_age_seconds = float(raw_oldest_item_age_seconds)
    if (
        not (0 <= depth <= _MAX_PROPERTY_SEARCH_QUEUE_DEPTH)
        or not math.isfinite(oldest_item_age_seconds)
        or not (
            0.0
            <= oldest_item_age_seconds
            <= _MAX_PROPERTY_SEARCH_QUEUE_AGE_SECONDS
        )
        or (depth == 0 and oldest_item_age_seconds != 0.0)
    ):
        raise ValueError("property_search_work_queue_snapshot_invalid")
    with _PROPERTY_SEARCH_WORK_QUEUE_METRICS_LOCK:
        _PROPERTY_SEARCH_WORK_QUEUE_METRICS.clear()
        _PROPERTY_SEARCH_WORK_QUEUE_METRICS.update(
            {
                "observed": True,
                "depth": depth,
                "oldest_item_age_seconds": oldest_item_age_seconds,
            }
        )


def _property_search_work_queue_metrics_unobserved() -> None:
    with _PROPERTY_SEARCH_WORK_QUEUE_METRICS_LOCK:
        _PROPERTY_SEARCH_WORK_QUEUE_METRICS.clear()
        _PROPERTY_SEARCH_WORK_QUEUE_METRICS["observed"] = False


def _property_search_work_queue_metrics_snapshot() -> dict[str, object]:
    with _PROPERTY_SEARCH_WORK_QUEUE_METRICS_LOCK:
        return dict(_PROPERTY_SEARCH_WORK_QUEUE_METRICS)


def _refresh_property_search_work_queue_metrics(
    repository: object,
    *,
    log: logging.Logger,
) -> None:
    try:
        snapshot = repository.observability_snapshot()  # type: ignore[attr-defined]
        _record_property_search_work_queue_metrics(snapshot)
    except Exception:
        _property_search_work_queue_metrics_unobserved()
        log.debug(
            "property search work queue observability snapshot failed",
            exc_info=True,
        )


def _scheduler_heartbeat_path() -> Path:
    return Path(
        str(
            os.environ.get("EA_SCHEDULER_HEARTBEAT_PATH")
            or "/data/artifacts/propertyquarry-scheduler-heartbeat.json"
        ).strip()
    )


def _writer_heartbeat_path(*, role: str) -> Path:
    instance_digest = hashlib.sha256(_EXECUTION_INSTANCE_ID.encode("utf-8")).hexdigest()[:20]
    filename = f"{role}-{instance_digest}.json"
    root = Path(
        str(
            os.environ.get("EA_PROPERTY_SEARCH_WRITER_HEARTBEAT_DIR")
            or "/data/artifacts/propertyquarry-writer-heartbeats"
        ).strip()
    )
    return root / filename


def _role_healthcheck_heartbeat_path(role: str) -> Path | None:
    if role == "scheduler":
        return _scheduler_heartbeat_path()
    if role == "worker":
        return Path(
            str(
                os.environ.get("EA_WORKER_HEARTBEAT_PATH")
                or "/data/artifacts/propertyquarry-worker-heartbeat.json"
            ).strip()
        )
    configured_api_path = str(os.environ.get("EA_API_HEARTBEAT_PATH") or "").strip()
    return Path(configured_api_path) if role == "api" and configured_api_path else None


def _execution_role_heartbeat_path(role: str) -> Path | None:
    normalized_role = str(role or "").strip().lower()
    if normalized_role in {"api", "worker", "scheduler"}:
        return _writer_heartbeat_path(role=normalized_role)
    return None


def _write_scheduler_heartbeat(*, role: str, status: str) -> None:
    normalized_role = str(role or "").strip().lower()
    path = _execution_role_heartbeat_path(normalized_role)
    if path is None:
        return
    try:
        now = time.time()
        normalized_status = str(status or "loop").strip().lower() or "loop"
        writer_ready = normalized_status in (
            PROPERTY_RESEARCH_PACKET_WRITER_READY_STATUSES.get(
                normalized_role,
                frozenset(),
            )
        )
        payload = {
            "instance_id": _EXECUTION_INSTANCE_ID,
            "started_at_epoch": _EXECUTION_STARTED_AT_EPOCH,
            "role": normalized_role,
            "status": normalized_status,
            "writer_ready": writer_ready,
            "epoch": now,
            "observed_at": datetime.fromtimestamp(now, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "pid": os.getpid(),
            "profile": _propertyquarry_scheduler_profile() if normalized_role == "scheduler" else "",
            "property_search_writer_contract": _property_search_writer_contract(),
        }
        if normalized_role == "scheduler":
            payload["delivery_outbox"] = _scheduler_delivery_metrics_snapshot()
        if normalized_role == "worker":
            payload["property_search_work_queue"] = _property_search_queue_metrics_snapshot()
        healthcheck_path = _role_healthcheck_heartbeat_path(normalized_role)
        paths = (
            [healthcheck_path, path]
            if healthcheck_path is not None and healthcheck_path != path
            else [path]
        )
        serialized = json.dumps(payload, sort_keys=True)
        for receipt_path in paths:
            try:
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = receipt_path.with_name(
                    f".{receipt_path.name}.{os.getpid()}.{uuid4().hex}.tmp"
                )
                tmp_path.write_text(serialized, encoding="utf-8")
                tmp_path.replace(receipt_path)
            except Exception:
                logging.getLogger("ea.runner").debug(
                    "execution role heartbeat receipt write failed role=%s",
                    normalized_role,
                    exc_info=True,
                )
    except Exception:
        logging.getLogger("ea.runner").debug(
            "execution role heartbeat write failed role=%s",
            normalized_role,
            exc_info=True,
        )


def _run_scheduler_step_with_heartbeat(
    *,
    role: str,
    step_name: str,
    timeout_seconds: float,
    timeout_result: dict[str, object],
    log: logging.Logger,
    fn,  # type: ignore[no-untyped-def]
    heartbeat_interval_seconds: float = 15.0,
    stop_event: threading.Event | None = None,
) -> dict[str, object]:
    normalized_step = re.sub(r"[^a-zA-Z0-9_]+", "_", str(step_name or "scheduler_step").strip().lower()).strip("_")
    if not normalized_step:
        normalized_step = "scheduler_step"
    timeout = max(0.01, float(timeout_seconds or 0.0))
    heartbeat_interval = max(0.01, float(heartbeat_interval_seconds or 0.0))

    def timeout_payload(*, running: bool = True) -> dict[str, object]:
        payload = dict(timeout_result)
        payload["timeout"] = True
        payload["running"] = running
        payload["step"] = normalized_step
        return payload

    def shutdown_payload(*, running: bool = True) -> dict[str, object]:
        payload = dict(timeout_result)
        payload["timeout"] = False
        payload["shutdown"] = True
        payload["running"] = running
        payload["step"] = normalized_step
        return payload

    def saturated_payload(*, active_steps: tuple[str, ...]) -> dict[str, object]:
        payload = dict(timeout_result)
        payload["timeout"] = False
        payload["deferred"] = True
        payload["running"] = False
        payload["step"] = normalized_step
        payload["active_steps"] = active_steps
        payload["concurrency_limit"] = _scheduler_step_concurrency_limit()
        return payload

    if stop_event is not None and stop_event.is_set():
        return shutdown_payload(running=False)

    def finish_state(state: dict[str, object]) -> dict[str, object]:
        result_queue = state.get("queue")
        if not isinstance(result_queue, queue.Queue):
            return timeout_payload(running=False)
        try:
            kind, payload = result_queue.get_nowait()
        except queue.Empty:
            return timeout_payload(running=False)
        if kind == "error":
            raise payload
        return dict(payload or {})

    now = time.monotonic()
    with _SCHEDULER_STEP_LOCK:
        state = _SCHEDULER_STEP_THREADS.get(normalized_step)
        if state:
            thread = state.get("thread")
            if isinstance(thread, threading.Thread) and thread.is_alive():
                _write_scheduler_heartbeat(role=role, status=f"{normalized_step}_running")
                started_at = float(state.get("started_at") or now)
                if now - started_at >= timeout:
                    if not bool(state.get("timeout_logged")):
                        state["timeout_logged"] = True
                        log.error(
                            "role=%s scheduler step timed out step=%s timeout=%.1fs",
                            role,
                            normalized_step,
                            timeout,
                        )
                    return timeout_payload(running=True)
                wait_seconds = min(heartbeat_interval, max(0.01, timeout - (now - started_at)))
            else:
                _SCHEDULER_STEP_THREADS.pop(normalized_step, None)
                if state:
                    return finish_state(state)
        else:
            active_steps = tuple(
                sorted(
                    name
                    for name, active_state in _SCHEDULER_STEP_THREADS.items()
                    if isinstance(active_state.get("thread"), threading.Thread)
                    and active_state["thread"].is_alive()
                )
            )
            if len(active_steps) >= _scheduler_step_concurrency_limit():
                log.warning(
                    "role=%s scheduler step deferred step=%s active=%s limit=%s",
                    role,
                    normalized_step,
                    ",".join(active_steps),
                    _scheduler_step_concurrency_limit(),
                )
                return saturated_payload(active_steps=active_steps)
            result_queue: queue.Queue = queue.Queue(maxsize=1)

            def _target() -> None:
                try:
                    result_queue.put(("ok", fn()))
                except BaseException as exc:  # pragma: no cover - re-raised on scheduler thread
                    result_queue.put(("error", exc))

            thread = threading.Thread(
                target=_target,
                name=f"propertyquarry-scheduler-{normalized_step}",
                daemon=True,
            )
            state = {
                "thread": thread,
                "queue": result_queue,
                "started_at": now,
                "timeout_logged": False,
            }
            _SCHEDULER_STEP_THREADS[normalized_step] = state
            thread.start()
            wait_seconds = min(heartbeat_interval, timeout)

    deadline = time.monotonic() + timeout
    while True:
        if stop_event is not None and stop_event.is_set():
            return shutdown_payload(running=thread.is_alive())
        _write_scheduler_heartbeat(role=role, status=f"{normalized_step}_running")
        thread = state.get("thread") if isinstance(state, dict) else None
        if not isinstance(thread, threading.Thread):
            return timeout_payload(running=False)
        remaining = deadline - time.monotonic()
        join_seconds = max(
            0.01,
            min(
                heartbeat_interval,
                0.25 if stop_event is not None else heartbeat_interval,
                remaining if remaining > 0 else heartbeat_interval,
            ),
        )
        thread.join(join_seconds)
        if not thread.is_alive():
            with _SCHEDULER_STEP_LOCK:
                _SCHEDULER_STEP_THREADS.pop(normalized_step, None)
            return finish_state(state)
        if time.monotonic() >= deadline:
            with _SCHEDULER_STEP_LOCK:
                current = _SCHEDULER_STEP_THREADS.get(normalized_step)
                if current is state and not bool(current.get("timeout_logged")):
                    current["timeout_logged"] = True
                    log.error(
                        "role=%s scheduler step timed out step=%s timeout=%.1fs",
                        role,
                        normalized_step,
                        timeout,
                    )
            return timeout_payload(running=True)


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception:
        value = default
    return max(0.0, value)


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except Exception:
        value = default
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _scheduler_step_concurrency_limit() -> int:
    return max(1, min(_env_int("EA_SCHEDULER_STEP_CONCURRENCY_LIMIT", 1), 4))


def _scheduler_onemin_refresh_interval_seconds() -> float:
    return _env_float(
        "EA_SCHEDULER_ONEMIN_REFRESH_INTERVAL_SECONDS",
        _SCHEDULER_ONEMIN_REFRESH_INTERVAL_SECONDS,
    )


def _scheduler_onemin_global_provider_api_sweep_enabled() -> bool:
    return _env_bool("EA_SCHEDULER_ONEMIN_GLOBAL_PROVIDER_API_SWEEP", True)


def _scheduler_google_signal_sync_interval_seconds() -> float:
    return _env_float(
        "EA_SCHEDULER_GOOGLE_SIGNAL_SYNC_INTERVAL_SECONDS",
        _SCHEDULER_GOOGLE_SIGNAL_SYNC_INTERVAL_SECONDS,
    )


def _scheduler_property_alert_account_emails() -> tuple[str, ...]:
    raw = str(
        os.environ.get("EA_PROPERTY_ALERT_ACCOUNT_EMAILS")
        or os.environ.get("EA_GOOGLE_PROPERTY_ALERT_ACCOUNT_EMAILS")
        or ""
    ).strip()
    values = [part.strip().lower() for part in re.split(r"[\s,;]+", raw) if part.strip()]
    if values:
        return tuple(sorted(set(values)))
    source_raw = str(os.environ.get("EA_PROPERTY_SCOUT_URLS_JSON") or "").strip()
    if not source_raw:
        return ()
    try:
        parsed = json.loads(source_raw)
    except Exception:
        return ()
    if not isinstance(parsed, list):
        return ()
    for item in parsed:
        if isinstance(item, dict):
            account_email = str(item.get("account_email") or "").strip().lower()
            if account_email:
                values.append(account_email)
    return tuple(sorted(set(values)))


def _scheduler_google_signal_sync_enabled() -> bool:
    return _env_bool("EA_SCHEDULER_GOOGLE_SIGNAL_SYNC_ENABLED", True)


def _scheduler_google_signal_sync_forbidden_cooldown_seconds() -> float:
    return _env_float(
        "EA_SCHEDULER_GOOGLE_SIGNAL_SYNC_FORBIDDEN_COOLDOWN_SECONDS",
        21600.0,
    )


def _scheduler_pocket_signal_sync_interval_seconds() -> float:
    return _env_float(
        "EA_SCHEDULER_POCKET_SIGNAL_SYNC_INTERVAL_SECONDS",
        _SCHEDULER_POCKET_SIGNAL_SYNC_INTERVAL_SECONDS,
    )


def _scheduler_pocket_signal_sync_enabled() -> bool:
    return _env_bool("EA_SCHEDULER_POCKET_SIGNAL_SYNC_ENABLED", bool(str(os.environ.get("POCKET_API_KEY") or "").strip()))


def _scheduler_pocket_signal_sync_limit() -> int:
    return max(1, min(_env_int("EA_SCHEDULER_POCKET_SIGNAL_SYNC_LIMIT", 5), 100))


def _scheduler_morning_memo_interval_seconds() -> float:
    return _env_float(
        "EA_SCHEDULER_MORNING_MEMO_INTERVAL_SECONDS",
        _SCHEDULER_MORNING_MEMO_INTERVAL_SECONDS,
    )


def _scheduler_morning_memo_enabled() -> bool:
    return _env_bool("EA_SCHEDULER_MORNING_MEMO_ENABLED", True)


def _scheduler_morning_memo_lease_seconds() -> int:
    return max(
        60,
        _env_int(
            "EA_SCHEDULER_MORNING_MEMO_LEASE_SECONDS",
            _SCHEDULER_MORNING_MEMO_LEASE_SECONDS,
        ),
    )


def _scheduler_morning_memo_max_attempts(value: object = None) -> int:
    try:
        configured = int(value) if value is not None else _env_int(
            "EA_SCHEDULER_MORNING_MEMO_MAX_ATTEMPTS",
            _SCHEDULER_MORNING_MEMO_MAX_ATTEMPTS,
        )
    except (TypeError, ValueError):
        configured = _SCHEDULER_MORNING_MEMO_MAX_ATTEMPTS
    return max(1, min(configured, 10))


def _scheduler_telegram_async_recovery_interval_seconds() -> float:
    return _env_float(
        "EA_SCHEDULER_TELEGRAM_ASYNC_RECOVERY_INTERVAL_SECONDS",
        _SCHEDULER_TELEGRAM_ASYNC_RECOVERY_INTERVAL_SECONDS,
    )


def _scheduler_telegram_async_recovery_min_age_seconds() -> float:
    return _env_float(
        "EA_SCHEDULER_TELEGRAM_ASYNC_RECOVERY_MIN_AGE_SECONDS",
        _SCHEDULER_TELEGRAM_ASYNC_RECOVERY_MIN_AGE_SECONDS,
    )


def _scheduler_telegram_async_recovery_enabled() -> bool:
    return _env_bool("EA_SCHEDULER_TELEGRAM_ASYNC_RECOVERY_ENABLED", True)


def _scheduler_public_base_url() -> str:
    return str(os.environ.get("EA_PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")


def _normalize_scheduler_time(value: str, *, default: str) -> tuple[int, int]:
    normalized = str(value or "").strip() or default
    hour, sep, minute = normalized.partition(":")
    try:
        hour_int = int(hour)
        minute_int = int(minute) if sep else 0
    except Exception:
        return _normalize_scheduler_time(default, default="08:00") if normalized != default else (8, 0)
    if 0 <= hour_int <= 23 and 0 <= minute_int <= 59:
        return hour_int, minute_int
    if normalized != default:
        return _normalize_scheduler_time(default, default="08:00")
    return 8, 0


def _schedule_timezone(name: str):
    normalized = str(name or "").strip()
    if not normalized:
        return timezone.utc
    try:
        return ZoneInfo(normalized)
    except ZoneInfoNotFoundError:
        return timezone.utc


def _is_local_time_within_quiet_hours(
    local_now: datetime,
    *,
    quiet_start: tuple[int, int],
    quiet_end: tuple[int, int],
) -> bool:
    start_minutes = quiet_start[0] * 60 + quiet_start[1]
    end_minutes = quiet_end[0] * 60 + quiet_end[1]
    now_minutes = local_now.hour * 60 + local_now.minute
    if start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes


def _morning_memo_cadence_allows_now(cadence: str, *, local_now: datetime) -> bool:
    normalized = str(cadence or "daily_morning").strip().lower() or "daily_morning"
    if normalized in {"weekdays", "weekdays_morning"}:
        return local_now.weekday() < 5
    return True


def _parse_observation_created_at(value: str) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except Exception:
        return None


def _recent_morning_memo_failure_within_retry(
    channel_runtime,
    *,
    principal_id: str,
    schedule_key: str,
    local_day: str,
    observed_at: datetime,
    retry_after_minutes: int,
):
    for row in channel_runtime.list_recent_observations(limit=50, principal_id=principal_id):
        if str(getattr(row, "event_type", "") or "").strip() != "scheduled_morning_memo_delivery_failed":
            continue
        payload = dict(getattr(row, "payload", {}) or {})
        if str(payload.get("schedule_key") or "").strip() != schedule_key:
            continue
        if str(payload.get("local_day") or "").strip() != local_day:
            continue
        created_at = _parse_observation_created_at(str(getattr(row, "created_at", "") or ""))
        if created_at is None:
            continue
        if observed_at - created_at < timedelta(minutes=max(retry_after_minutes, 5)):
            return row
    return None


def _run_scheduler_onemin_billing_refresh(container, log: logging.Logger) -> dict[str, object]:  # type: ignore[no-untyped-def]
    from app.api.routes import providers as providers_route

    refresh_allowed, throttle_seconds_remaining, throttle_reason = container.onemin_manager.begin_billing_refresh()
    if not refresh_allowed:
        return {
            "ran": False,
            "throttled": True,
            "throttle_seconds_remaining": max(float(throttle_seconds_remaining), 0.0),
            "throttle_reason": str(throttle_reason or ""),
            "browseract_attempted": 0,
            "browseract_refreshed": 0,
            "member_reconciled": 0,
            "api_attempted": 0,
            "api_rate_limited": False,
            "errors": 0,
        }

    browseract_attempted = 0
    browseract_refreshed = 0
    member_reconciled = 0
    api_attempted = 0
    api_rate_limited = False
    api_recovered = 0
    browseract_failed = 0
    error_count = 0
    browseract_max_accounts = max(1, int(providers_route._onemin_browseract_max_accounts_per_refresh()))
    browseract_parallelism = max(1, int(providers_route._onemin_browseract_parallelism()))
    browseract_timeout_seconds = max(30, int(providers_route._onemin_browseract_timeout_seconds()))

    try:
        bindings = [
            binding
            for binding in container.tool_runtime.list_connector_bindings_for_connector("browseract", limit=1000)
            if str(binding.status or "").strip().lower() == "enabled"
        ]
        binding_jobs: list[dict[str, object]] = []
        bound_account_label_order: list[str] = []
        seen_bound_account_labels: set[str] = set()
        all_account_login_credentials: dict[str, dict[str, str]] = {}
        for binding in bindings:
            principal_id = str(binding.principal_id or "").strip()
            if not principal_id:
                continue
            binding_metadata = dict(binding.auth_metadata_json or {})
            billing_run_url = providers_route._binding_run_url(
                binding_metadata,
                "onemin_billing_usage_run_url",
                "browseract_onemin_billing_usage_run_url",
                "run_url",
            )
            billing_workflow_id = providers_route._binding_workflow_id(
                binding_metadata,
                "onemin_billing_usage_workflow_id",
                "browseract_onemin_billing_usage_workflow_id",
                "workflow_id",
            )
            members_run_url = providers_route._binding_run_url(
                binding_metadata,
                "onemin_members_run_url",
                "browseract_onemin_members_run_url",
            )
            members_workflow_id = providers_route._binding_workflow_id(
                binding_metadata,
                "onemin_members_workflow_id",
                "browseract_onemin_members_workflow_id",
            )
            account_labels = providers_route._resolve_onemin_account_labels(binding)
            for account_label in account_labels:
                if account_label and account_label not in seen_bound_account_labels:
                    seen_bound_account_labels.add(account_label)
                    bound_account_label_order.append(account_label)
                credentials = providers_route.upstream.onemin_account_login_credentials(
                    account_name=account_label,
                    binding_metadata=binding_metadata,
                )
                if credentials:
                    all_account_login_credentials[account_label] = credentials
            binding_jobs.append(
                {
                    "binding": binding,
                    "principal_id": principal_id,
                    "binding_id": binding.binding_id,
                    "external_account_ref": binding.external_account_ref,
                    "binding_metadata": binding_metadata,
                    "billing_run_url": billing_run_url,
                    "billing_workflow_id": billing_workflow_id,
                    "members_run_url": members_run_url,
                    "members_workflow_id": members_workflow_id,
                    "account_labels": tuple(account_labels),
                }
            )
        all_owner_account_rows = providers_route._normalized_onemin_owner_rows()
        for row in all_owner_account_rows:
            account_label = str(row.get("account_name") or "").strip()
            if not account_label or account_label in all_account_login_credentials:
                continue
            credentials = providers_route.upstream.onemin_account_login_credentials(
                account_name=account_label,
                binding_metadata={},
            )
            if credentials:
                all_account_login_credentials[account_label] = credentials

        if bound_account_label_order:
            browseract_target_label_order = list(bound_account_label_order)
        elif all_owner_account_rows and binding_jobs:
            browseract_target_label_order = [
                str(row.get("account_name") or "").strip()
                for row in all_owner_account_rows
                if str(row.get("account_name") or "").strip()
            ]
        else:
            browseract_target_label_order = []
        select_refresh_account_labels = getattr(container.onemin_manager, "select_billing_refresh_account_labels", None)
        stale_labels, actual_labels = providers_route._partition_onemin_browseract_account_labels(
            container=container,
            principal_id="",
            binding_rows=[],
            account_labels=browseract_target_label_order,
        )
        selected_browseract_labels: set[str] = set()
        if stale_labels:
            if callable(select_refresh_account_labels):
                selected_browseract_labels.update(
                    select_refresh_account_labels(
                        stale_labels,
                        limit=min(browseract_max_accounts, len(stale_labels)),
                    )
                )
            else:
                selected_browseract_labels.update(list(stale_labels)[: min(browseract_max_accounts, len(stale_labels))])
        remaining_browseract_slots = max(browseract_max_accounts - len(selected_browseract_labels), 0)
        if remaining_browseract_slots > 0 and actual_labels:
            if callable(select_refresh_account_labels):
                selected_browseract_labels.update(
                    select_refresh_account_labels(
                        actual_labels,
                        limit=min(remaining_browseract_slots, len(actual_labels)),
                    )
                )
            else:
                selected_browseract_labels.update(list(actual_labels)[: min(remaining_browseract_slots, len(actual_labels))])

        browseract_billing_jobs: list[dict[str, object]] = []
        browseract_attempted_labels: set[str] = set()
        for account_label in browseract_target_label_order:
            if account_label not in selected_browseract_labels:
                continue
            selected_binding_job = providers_route._select_onemin_browseract_binding_job(
                binding_jobs=binding_jobs,
                account_label=account_label,
                require_members=False,
            )
            if selected_binding_job is None:
                continue
            binding_metadata = dict(selected_binding_job.get("binding_metadata") or {})
            browseract_billing_jobs.append(
                {
                    "principal_id": str(selected_binding_job.get("principal_id") or ""),
                    "binding_id": str(selected_binding_job.get("binding_id") or ""),
                    "external_account_ref": str(selected_binding_job.get("external_account_ref") or ""),
                    "account_label": account_label,
                    "billing_run_url": str(selected_binding_job.get("billing_run_url") or ""),
                    "billing_workflow_id": str(selected_binding_job.get("billing_workflow_id") or ""),
                    "members_run_url": str(selected_binding_job.get("members_run_url") or ""),
                    "members_workflow_id": str(selected_binding_job.get("members_workflow_id") or ""),
                    "member_login_ready": providers_route._browseract_onemin_login_ready(
                        account_label=account_label,
                        binding_metadata=binding_metadata,
                    ),
                }
            )

        browseract_attempted = len(browseract_billing_jobs)
        with providers_route._managed_fastestvpn_services(
            service_names=providers_route._browseract_fastestvpn_service_names(browseract_billing_jobs),
            reason="scheduler.onemin.browseract.refresh",
        ):
            effective_browseract_parallelism = 1 if all_owner_account_rows else max(
                1,
                min(
                    browseract_parallelism,
                    len(
                        {
                            str(job.get("binding_id") or "").strip()
                            for job in browseract_billing_jobs
                            if str(job.get("binding_id") or "").strip()
                        }
                    )
                    or 1,
                ),
            )
            billing_results, billing_errors = providers_route._run_onemin_browseract_jobs(
                jobs=browseract_billing_jobs,
                max_workers=effective_browseract_parallelism,
                tool_name="browseract.onemin_billing_usage",
                stop_on_failure_codes={
                    "auth_request_failed",
                    "challenge_required",
                    "session_expired",
                    "timeout",
                    "ui_worker_failed",
                    "lane_unavailable",
                },
                max_consecutive_stop_failures=providers_route._onemin_browseract_systemic_failure_threshold(),
                invoke_job=lambda job: providers_route._invoke_browseract_tool(
                    container=container,
                    principal_id=str(job.get("principal_id") or ""),
                    tool_name="browseract.onemin_billing_usage",
                    action_kind="billing.inspect",
                    payload_json={
                        "binding_id": str(job.get("binding_id") or ""),
                        "account_label": str(job.get("account_label") or ""),
                        "capture_raw_text": False,
                        **({"run_url": str(job.get("billing_run_url") or "")} if str(job.get("billing_run_url") or "").strip() else {}),
                        **({"workflow_id": str(job.get("billing_workflow_id") or "")} if str(job.get("billing_workflow_id") or "").strip() else {}),
                        **providers_route._browseract_proxy_payload(
                            binding_metadata=dict(job.get("binding_metadata") or {}),
                            account_label=str(job.get("account_label") or ""),
                        ),
                        "timeout_seconds": browseract_timeout_seconds,
                    },
                ),
            )
            browseract_attempted_labels = {
                str(row.get("account_label") or "").strip()
                for row in [*billing_results, *billing_errors]
                if str(row.get("account_label") or "").strip()
            }
            browseract_attempted = len(browseract_attempted_labels)
            browseract_refreshed = len(billing_results)

            successful_labels = {
                str(row.get("account_label") or "").strip()
                for row in billing_results
                if str(row.get("account_label") or "").strip()
            }
            browseract_member_jobs = [
                dict(job)
                for job in browseract_billing_jobs
                if str(job.get("account_label") or "").strip() in successful_labels
                and (
                    str(job.get("members_run_url") or "").strip()
                    or str(job.get("members_workflow_id") or "").strip()
                    or bool(job.get("member_login_ready"))
                )
            ]
            effective_member_parallelism = 1 if all_owner_account_rows else max(
                1,
                min(
                    browseract_parallelism,
                    len(
                        {
                            str(job.get("binding_id") or "").strip()
                            for job in browseract_member_jobs
                            if str(job.get("binding_id") or "").strip()
                        }
                    )
                    or 1,
                ),
            )
            member_results, member_errors = providers_route._run_onemin_browseract_jobs(
                jobs=browseract_member_jobs,
                max_workers=effective_member_parallelism,
                tool_name="browseract.onemin_member_reconciliation",
                stop_on_failure_codes={
                    "auth_request_failed",
                    "challenge_required",
                    "session_expired",
                    "timeout",
                    "ui_worker_failed",
                    "lane_unavailable",
                },
                max_consecutive_stop_failures=providers_route._onemin_browseract_systemic_failure_threshold(),
                invoke_job=lambda job: providers_route._invoke_browseract_tool(
                    container=container,
                    principal_id=str(job.get("principal_id") or ""),
                    tool_name="browseract.onemin_member_reconciliation",
                    action_kind="billing.reconcile_members",
                    payload_json={
                        "binding_id": str(job.get("binding_id") or ""),
                        "account_label": str(job.get("account_label") or ""),
                        "capture_raw_text": False,
                        **({"run_url": str(job.get("members_run_url") or "")} if str(job.get("members_run_url") or "").strip() else {}),
                        **({"workflow_id": str(job.get("members_workflow_id") or "")} if str(job.get("members_workflow_id") or "").strip() else {}),
                        **providers_route._browseract_proxy_payload(
                            binding_metadata=dict(job.get("binding_metadata") or {}),
                            account_label=str(job.get("account_label") or ""),
                        ),
                        "timeout_seconds": browseract_timeout_seconds,
                    },
                ),
            )
            member_reconciled = len(member_results)
        browseract_failed_labels = {
            str(row.get("account_label") or "").strip()
            for row in [*billing_errors, *member_errors]
            if str(row.get("account_label") or "").strip()
        }
        browseract_failed = len(browseract_failed_labels)
        recovered_browseract_labels: set[str] = set()
        fallback_api_errors: list[dict[str, object]] = []
        unattempted_browseract_labels = {
            label
            for label in browseract_target_label_order
            if label and label not in browseract_attempted_labels
        }
        if browseract_failed_labels:
            with providers_route._managed_fastestvpn_services(
                service_names=providers_route._onemin_direct_api_fastestvpn_service_names(
                    account_labels=browseract_failed_labels | unattempted_browseract_labels
                ),
                reason="scheduler.onemin.provider_api.recovery",
            ):
                (
                    api_billing_results,
                    api_member_results,
                    fallback_api_errors,
                    api_attempted,
                    _api_skipped,
                    api_rate_limited,
                ) = providers_route._refresh_onemin_via_provider_api(
                    include_members=True,
                    timeout_seconds=180,
                    all_accounts=False,
                    continue_on_rate_limit=False,
                    account_labels=browseract_failed_labels | unattempted_browseract_labels,
                    account_login_credentials={
                        account_label: credentials
                        for account_label, credentials in all_account_login_credentials.items()
                        if account_label in (browseract_failed_labels | unattempted_browseract_labels)
                    },
                )
            recovered_browseract_labels.update(
                str(row.get("account_label") or "").strip()
                for row in [*api_billing_results, *api_member_results]
                if str(row.get("account_label") or "").strip()
            )
            api_recovered = len(recovered_browseract_labels & browseract_failed_labels)
            member_reconciled = len(
                {
                    str(row.get("account_label") or "").strip()
                    for row in [*member_results, *api_member_results]
                    if str(row.get("account_label") or "").strip()
                }
            )
        elif _scheduler_onemin_global_provider_api_sweep_enabled():
            if unattempted_browseract_labels:
                with providers_route._managed_fastestvpn_services(
                    service_names=providers_route._onemin_direct_api_fastestvpn_service_names(
                        account_labels=unattempted_browseract_labels
                    ),
                    reason="scheduler.onemin.provider_api.gap_recovery",
                ):
                    (
                        _api_billing_results,
                        _api_member_results,
                        api_errors,
                        global_api_attempted,
                        _api_skipped,
                        global_api_rate_limited,
                    ) = providers_route._refresh_onemin_via_provider_api(
                        include_members=True,
                        timeout_seconds=180,
                        all_accounts=False,
                        continue_on_rate_limit=False,
                        account_labels=unattempted_browseract_labels,
                        account_login_credentials={
                            account_label: credentials
                            for account_label, credentials in all_account_login_credentials.items()
                            if account_label in unattempted_browseract_labels
                        },
                    )
                api_attempted += global_api_attempted
                api_rate_limited = api_rate_limited or global_api_rate_limited
                error_count += len(api_errors)
            elif not browseract_target_label_order:
                with providers_route._managed_fastestvpn_services(
                    service_names=providers_route._onemin_direct_api_fastestvpn_service_names(account_labels=None),
                    reason="scheduler.onemin.provider_api.global",
                ):
                    (
                        _api_billing_results,
                        _api_member_results,
                        api_errors,
                        global_api_attempted,
                        _api_skipped,
                        global_api_rate_limited,
                    ) = providers_route._refresh_onemin_via_provider_api(
                        include_members=True,
                        timeout_seconds=180,
                        all_accounts=True,
                        continue_on_rate_limit=False,
                    )
                api_attempted += global_api_attempted
                api_rate_limited = api_rate_limited or global_api_rate_limited
                error_count += len(api_errors)

        unrecovered_billing_errors = [
            row
            for row in billing_errors
            if str(row.get("account_label") or "").strip() not in recovered_browseract_labels
        ]
        unrecovered_member_errors = [
            row
            for row in member_errors
            if str(row.get("account_label") or "").strip() not in recovered_browseract_labels
        ]
        error_count += len(unrecovered_billing_errors)
        error_count += len(unrecovered_member_errors)
        error_count += len(fallback_api_errors)
        for row in unrecovered_billing_errors:
            log.warning(
                "scheduler onemin billing browseract refresh failed principal_ref=%s binding_ref=%s account_ref=%s code=%s error=%s",
                _scheduler_log_ref(next((str(job.get("principal_id") or "") for job in browseract_billing_jobs if str(job.get("account_label") or "") == str(row.get("account_label") or "")), "")),
                _scheduler_log_ref(row.get("binding_id")),
                _scheduler_log_ref(row.get("account_label")),
                row.get("failure_code"),
                row.get("error"),
            )
        for row in unrecovered_member_errors:
            log.warning(
                "scheduler onemin member reconciliation failed principal_ref=%s binding_ref=%s account_ref=%s code=%s error=%s",
                _scheduler_log_ref(next((str(job.get("principal_id") or "") for job in browseract_member_jobs if str(job.get("account_label") or "") == str(row.get("account_label") or "")), "")),
                _scheduler_log_ref(row.get("binding_id")),
                _scheduler_log_ref(row.get("account_label")),
                row.get("failure_code"),
                row.get("error"),
            )
        if recovered_browseract_labels:
            log.info(
                "scheduler onemin recovered browseract failures via provider api accounts=%s",
                ",".join(_scheduler_log_ref(value) for value in sorted(recovered_browseract_labels & browseract_failed_labels)),
            )
        for row in fallback_api_errors:
            log.warning(
                "scheduler onemin provider api fallback failed account_ref=%s error=%s",
                _scheduler_log_ref(row.get("account_label")),
                row.get("error"),
            )

        return {
            "ran": True,
            "throttled": False,
            "throttle_seconds_remaining": 0.0,
            "throttle_reason": "",
            "browseract_attempted": browseract_attempted,
            "browseract_refreshed": browseract_refreshed,
            "member_reconciled": member_reconciled,
            "api_attempted": api_attempted,
            "api_rate_limited": api_rate_limited,
            "api_recovered": api_recovered,
            "browseract_failed": browseract_failed,
            "errors": error_count,
        }
    finally:
        container.onemin_manager.finish_billing_refresh()


def _run_scheduler_google_signal_sync(container, log: logging.Logger) -> dict[str, object]:  # type: ignore[no-untyped-def]
    from app.product.service import build_product_service
    from app.services.google_oauth import GOOGLE_CONNECTOR_NAME

    service = build_product_service(container)
    bindings = [
        binding
        for binding in container.tool_runtime.list_connector_bindings_for_connector(GOOGLE_CONNECTOR_NAME, limit=1000)
        if str(binding.status or "").strip().lower() == "enabled" and str(binding.principal_id or "").strip()
    ]
    principal_ids = tuple(sorted({str(binding.principal_id or "").strip() for binding in bindings}))
    attempted = 0
    synced = 0
    error_count = 0
    skipped = 0
    property_accounts = _scheduler_property_alert_account_emails()
    property_attempted = 0
    property_synced = 0
    forbidden_cooldown_seconds = _scheduler_google_signal_sync_forbidden_cooldown_seconds()
    now_epoch = time.time()
    for principal_id in principal_ids:
        blocked_until = float(_SCHEDULER_GOOGLE_SIGNAL_SYNC_FORBIDDEN_COOLDOWNS.get(principal_id) or 0.0)
        if blocked_until > now_epoch:
            skipped += 1
            log.info(
                "scheduler google signal sync skipped principal_ref=%s reason=http_403_cooldown retry_in=%ss",
                _scheduler_log_ref(principal_id),
                max(1, int(blocked_until - now_epoch)),
            )
            continue
        if blocked_until > 0.0:
            _SCHEDULER_GOOGLE_SIGNAL_SYNC_FORBIDDEN_COOLDOWNS.pop(principal_id, None)
        attempted += 1
        try:
            summary = service.sync_google_workspace_signals(
                principal_id=principal_id,
                actor="scheduler",
                email_limit=5,
                calendar_limit=5,
            )
            if int(summary.get("total") or 0) >= 0:
                synced += 1
            for account_email in property_accounts:
                property_attempted += 1
                property_summary = service.sync_google_willhaben_signals(
                    principal_id=principal_id,
                    actor="scheduler",
                    account_email=account_email,
                    email_limit=10,
                )
                property_synced += int(property_summary.get("synced_total") or 0)
        except RuntimeError as exc:
            error_count += 1
            log.info(
                "scheduler google signal sync skipped principal_ref=%s reason=%s",
                _scheduler_log_ref(principal_id),
                str(exc or "unknown_error"),
            )
        except urllib.error.HTTPError as exc:
            error_count += 1
            if exc.code in {401, 403} and forbidden_cooldown_seconds > 0.0:
                blocked_until = time.time() + forbidden_cooldown_seconds
                _SCHEDULER_GOOGLE_SIGNAL_SYNC_FORBIDDEN_COOLDOWNS[principal_id] = blocked_until
                log.warning(
                    "scheduler google signal sync auth blocked principal_ref=%s status=%s cooldown_until=%s",
                    _scheduler_log_ref(principal_id),
                    exc.code,
                    datetime.fromtimestamp(blocked_until, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                )
                continue
            log.exception("scheduler google signal sync failed principal_ref=%s", _scheduler_log_ref(principal_id))
        except Exception:
            error_count += 1
            log.exception("scheduler google signal sync failed principal_ref=%s", _scheduler_log_ref(principal_id))
    result = {
        "ran": True,
        "attempted": attempted,
        "synced": synced,
        "errors": error_count,
        "skipped": skipped,
    }
    if property_accounts:
        result.update(
            {
                "property_accounts": list(property_accounts),
                "property_attempted": property_attempted,
                "property_synced": property_synced,
            }
        )
    return result


def _scheduler_property_scout_enabled() -> bool:
    normalized = str(os.environ.get("EA_SCHEDULER_PROPERTY_SCOUT_ENABLED") or "").strip().lower()
    if not normalized:
        return True
    return normalized in {"1", "true", "yes", "on", "y"}


def _propertyquarry_scheduler_profile() -> str:
    return str(os.environ.get("PROPERTYQUARRY_SCHEDULER_PROFILE") or "").strip().lower()


def _scheduler_property_only_profile_enabled() -> bool:
    return _propertyquarry_scheduler_profile() in {"property_only", "property-only", "property"}


def _propertyquarry_worker_profile() -> str:
    return str(os.environ.get("PROPERTYQUARRY_WORKER_PROFILE") or "").strip().lower()


def _worker_property_only_profile_enabled() -> bool:
    return _propertyquarry_worker_profile() in {"property_only", "property-only", "property"}


def _scheduler_property_scout_interval_seconds() -> float:
    try:
        return max(300.0, float(os.environ.get("EA_SCHEDULER_PROPERTY_SCOUT_INTERVAL_SECONDS") or 1800.0))
    except Exception:
        return 1800.0


def _scheduler_property_scout_timeout_seconds() -> float:
    return max(30.0, _env_float("EA_SCHEDULER_PROPERTY_SCOUT_TIMEOUT_SECONDS", 600.0))


def _scheduler_property_results_finalize_interval_seconds() -> float:
    try:
        return max(15.0, float(os.environ.get("EA_SCHEDULER_PROPERTY_RESULTS_FINALIZE_INTERVAL_SECONDS") or 60.0))
    except Exception:
        return 60.0


def _scheduler_property_results_finalize_timeout_seconds() -> float:
    return max(30.0, _env_float("EA_SCHEDULER_PROPERTY_RESULTS_FINALIZE_TIMEOUT_SECONDS", 240.0))


def _scheduler_property_results_finalize_limit() -> int:
    return max(1, min(_env_int("EA_SCHEDULER_PROPERTY_RESULTS_FINALIZE_LIMIT", 40), 200))


def _scheduler_property_provider_repair_limit() -> int:
    return max(1, min(_env_int("EA_SCHEDULER_PROPERTY_PROVIDER_REPAIR_LIMIT", 5), 40))


def _scheduler_property_tour_followup_limit() -> int:
    return max(1, min(_env_int("EA_SCHEDULER_PROPERTY_TOUR_FOLLOWUP_LIMIT", 1), 20))


def _scheduler_property_maintenance_order(principal_ids: tuple[str, ...]) -> tuple[str, ...]:
    global _SCHEDULER_PROPERTY_MAINTENANCE_ROTATION

    if not principal_ids:
        return principal_ids
    with _SCHEDULER_PROPERTY_MAINTENANCE_ROTATION_LOCK:
        rotation = _SCHEDULER_PROPERTY_MAINTENANCE_ROTATION % len(principal_ids)
        _SCHEDULER_PROPERTY_MAINTENANCE_ROTATION += 1
    if not rotation:
        return principal_ids
    return principal_ids[rotation:] + principal_ids[:rotation]


def _scheduler_property_search_recovery_interval_seconds() -> float:
    try:
        return max(15.0, float(os.environ.get("EA_SCHEDULER_PROPERTY_SEARCH_RECOVERY_INTERVAL_SECONDS") or 60.0))
    except Exception:
        return 60.0


def _scheduler_property_search_recovery_timeout_seconds() -> float:
    return max(30.0, _env_float("EA_SCHEDULER_PROPERTY_SEARCH_RECOVERY_TIMEOUT_SECONDS", 240.0))


def _scheduler_property_scout_principal_ids(container) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
    raw = str(os.environ.get("EA_PROPERTY_SCOUT_PRINCIPAL_IDS") or "").strip()
    if raw:
        values = tuple(sorted({part.strip() for part in raw.split(",") if part.strip()}))
        if values:
            return values
    onboarding_service = getattr(container, "onboarding", None)
    if onboarding_service is not None:
        list_principals = getattr(onboarding_service, "list_property_search_agent_principals", None)
        if callable(list_principals):
            try:
                discovered = tuple(
                    sorted(
                        {
                            str(value or "").strip()
                            for value in list_principals(limit=1000)
                            if str(value or "").strip()
                        }
                    )
                )
            except Exception:
                return ()
            # An available discovery service is authoritative. An empty result
            # means every saved search is paused; it must not launch a legacy
            # default principal.
            return discovered
    settings = getattr(container, "settings", None)
    principal_candidates = {
        str(os.environ.get("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID") or "").strip(),
        str(getattr(getattr(settings, "auth", None), "default_principal_id", "") or "").strip(),
        str(os.environ.get("EA_DEFAULT_PRINCIPAL_ID") or "").strip(),
    }
    return tuple(sorted(value for value in principal_candidates if value))


def _run_scheduler_property_scout(container, log: logging.Logger) -> dict[str, object]:  # type: ignore[no-untyped-def]
    from app.product.service import build_product_service

    service = build_product_service(container)
    attempted = 0
    synced = 0
    errors = 0
    launched = 0
    due = 0
    skipped_active = 0
    skipped_not_due = 0
    principals = _scheduler_property_scout_principal_ids(container)
    for principal_id in principals:
        attempted += 1
        try:
            launch_due_agents = getattr(service, "launch_due_property_search_agents", None)
            if callable(launch_due_agents):
                launch_summary = dict(launch_due_agents(principal_id=principal_id, actor="scheduler") or {})
            else:
                launch_summary = {"mode": "fallback"}
            if str(launch_summary.get("mode") or "").strip() == "agents":
                launched_total = int(launch_summary.get("launched_total") or 0)
                launched += launched_total
                due += int(launch_summary.get("due_total") or 0)
                skipped_active += int(launch_summary.get("skipped_active_total") or 0)
                skipped_not_due += int(launch_summary.get("skipped_not_due_total") or 0)
                synced += launched_total
                continue
            summary = service.sync_direct_property_scout(
                principal_id=principal_id,
                actor="scheduler",
            )
            if str(summary.get("status") or "").strip() in {"processed", "noop"}:
                synced += max(
                    int(summary.get("review_created_total") or 0),
                    int(summary.get("notified_total") or 0),
                    int(summary.get("tour_created_total") or 0),
                )
        except Exception:
            errors += 1
            log.exception("scheduler property scout failed principal_ref=%s", _scheduler_log_ref(principal_id))
    return {
        "ran": True,
        "attempted": attempted,
        "synced": synced,
        "launched": launched,
        "due": due,
        "skipped_active": skipped_active,
        "skipped_not_due": skipped_not_due,
        "errors": errors,
        "principal_count": len(principals),
    }


def _run_scheduler_property_search_recovery(container, log: logging.Logger) -> dict[str, object]:  # type: ignore[no-untyped-def]
    from app.product.service import build_product_service

    service = build_product_service(container)
    principal_ids = tuple(str(value or "").strip() for value in _scheduler_property_scout_principal_ids(container) if str(value or "").strip())
    aggregate = {"scanned": 0, "stale_total": 0, "repaired": 0, "replacement_started": 0, "errors": 0}
    if principal_ids:
        for principal_id in principal_ids:
            try:
                summary = service.reconcile_stale_property_search_runs(principal_id=principal_id, limit=80)
            except Exception:
                aggregate["errors"] += 1
                log.exception(
                    "scheduler property search recovery failed principal_ref=%s",
                    _scheduler_log_ref(principal_id),
                )
                continue
            for key in ("scanned", "stale_total", "repaired", "replacement_started", "errors"):
                aggregate[key] += int(summary.get(key) or 0)
    else:
        try:
            summary = service.reconcile_stale_property_search_runs(limit=80)
        except Exception:
            log.exception("scheduler property search recovery failed")
            return {"ran": True, "scanned": 0, "stale_total": 0, "repaired": 0, "replacement_started": 0, "errors": 1}
        for key in ("scanned", "stale_total", "repaired", "replacement_started", "errors"):
            aggregate[key] = int(summary.get(key) or 0)
    return {
        "ran": True,
        "scanned": int(aggregate.get("scanned") or 0),
        "stale_total": int(aggregate.get("stale_total") or 0),
        "repaired": int(aggregate.get("repaired") or 0),
        "replacement_started": int(aggregate.get("replacement_started") or 0),
        "errors": int(aggregate.get("errors") or 0),
    }


def _run_scheduler_property_results_finalize(container, log: logging.Logger) -> dict[str, object]:  # type: ignore[no-untyped-def]
    from app.product.service import build_product_service

    service = build_product_service(container)
    try:
        summary = service.reconcile_property_search_results_delivery(
            limit=_scheduler_property_results_finalize_limit(),
            allow_notifications=False,
        )
    except Exception:
        log.exception("scheduler property results finalize failed")
        return {"ran": True, "attempted": 0, "finalized": 0, "emailed": 0, "pending": 0, "errors": 1}
    repair_resolved_total = 0
    repair_deferred_total = 0
    repair_errors = 0
    visual_followup_resolved_total = 0
    visual_followup_failed_total = 0
    principal_ids = _scheduler_property_maintenance_order(
        tuple(_scheduler_property_scout_principal_ids(container))
    )
    repair_remaining = _scheduler_property_provider_repair_limit()
    tour_remaining = _scheduler_property_tour_followup_limit()
    for principal_id in principal_ids:
        if repair_remaining <= 0 and tour_remaining <= 0:
            break
        if repair_remaining > 0:
            try:
                repair_summary = service.process_property_provider_repair_tasks(
                    principal_id=principal_id,
                    actor="scheduler",
                    limit=repair_remaining,
                )
                repair_resolved = int(repair_summary.get("resolved_total") or 0)
                repair_deferred = int(repair_summary.get("deferred_total") or 0)
                repair_resolved_total += repair_resolved
                repair_deferred_total += repair_deferred
                repair_consumed = int(
                    repair_summary.get("attempted_total")
                    or repair_resolved + repair_deferred
                    or 1
                )
                repair_remaining = max(0, repair_remaining - repair_consumed)
            except Exception:
                repair_errors += 1
                repair_remaining = max(0, repair_remaining - 1)
                log.exception(
                    "scheduler property provider repair processing failed principal_ref=%s",
                    _scheduler_log_ref(principal_id),
                )
        if tour_remaining > 0:
            try:
                visual_summary = service.process_property_tour_followup_tasks(
                    principal_id=principal_id,
                    actor="scheduler",
                    limit=tour_remaining,
                )
                visual_resolved = int(visual_summary.get("resolved_total") or 0)
                visual_failed = int(visual_summary.get("failed_total") or 0)
                visual_followup_resolved_total += visual_resolved
                visual_followup_failed_total += visual_failed
                visual_consumed = int(
                    visual_summary.get("attempted_total")
                    or visual_resolved + visual_failed
                    or 1
                )
                tour_remaining = max(0, tour_remaining - visual_consumed)
            except Exception:
                visual_followup_failed_total += 1
                tour_remaining = max(0, tour_remaining - 1)
                log.exception(
                    "scheduler property tour followup processing failed principal_ref=%s",
                    _scheduler_log_ref(principal_id),
                )
    return {
        "ran": True,
        "attempted": int(summary.get("attempted") or 0),
        "finalized": int(summary.get("finalized") or 0),
        "emailed": int(summary.get("emailed") or 0),
        "pending": int(summary.get("pending") or 0),
        "repair_resolved_total": repair_resolved_total,
        "repair_deferred_total": repair_deferred_total,
        "visual_followup_resolved_total": visual_followup_resolved_total,
        "visual_followup_failed_total": visual_followup_failed_total,
        "errors": repair_errors,
    }


def _run_scheduler_pocket_signal_sync(container, log: logging.Logger) -> dict[str, object]:  # type: ignore[no-untyped-def]
    from app.product.service import build_product_service

    if not str(os.environ.get("POCKET_API_KEY") or "").strip():
        return {"ran": False, "attempted": 0, "synced": 0, "errors": 0, "reason": "pocket_api_key_missing"}
    principal_id = str(getattr(getattr(container.settings, "auth", None), "default_principal_id", "") or "").strip() or "local-user"
    service = build_product_service(container)
    try:
        summary = service.sync_pocket_recordings(
            principal_id=principal_id,
            actor="scheduler",
            limit=_scheduler_pocket_signal_sync_limit(),
        )
        synced_total = max(int(summary.get("synced_total") or summary.get("total") or 0), 0)
        failed_total = max(int(summary.get("failed_total") or 0), 0)
        return {
            "ran": True,
            "attempted": 1,
            "synced": synced_total,
            "errors": failed_total,
            "principal_ref": _scheduler_log_ref(principal_id),
        }
    except RuntimeError as exc:
        log.info(
            "scheduler pocket signal sync skipped principal_ref=%s reason=%s",
            _scheduler_log_ref(principal_id),
            str(exc or "unknown_error"),
        )
        return {
            "ran": True,
            "attempted": 1,
            "synced": 0,
            "errors": 1,
            "principal_ref": _scheduler_log_ref(principal_id),
        }
    except Exception:
        log.exception("scheduler pocket signal sync failed principal_ref=%s", _scheduler_log_ref(principal_id))
        return {
            "ran": True,
            "attempted": 1,
            "synced": 0,
            "errors": 1,
            "principal_ref": _scheduler_log_ref(principal_id),
        }


def _scheduled_morning_memo_idempotency_keys(
    *,
    principal_id: str,
    schedule_key: str,
    local_day: str,
    delivery_channel: str,
    digest_key: str,
) -> tuple[str, str]:
    canonical = "\0".join(
        (
            str(principal_id or "").strip(),
            str(schedule_key or "").strip(),
            str(local_day or "").strip(),
        )
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return (f"scheduled-morning-memo:{digest}", f"propertyquarry-morning-memo:{digest}")


def _scheduler_delivery_lease_owner() -> str:
    host = str(os.environ.get("HOSTNAME") or "local").strip() or "local"
    return f"scheduler:{host}:{os.getpid()}:{_EXECUTION_INSTANCE_ID}"


def _dispatch_scheduled_morning_memo(
    *,
    container,
    service,
    observed_at: datetime,
    principal_id: str,
    schedule_key: str,
    local_day: str,
    digest_key: str,
    recipient_email: str,
    role: str,
    display_name: str,
    delivery_channel: str,
    retry_after_minutes: int,
    max_attempts: int,
) -> dict[str, object]:  # type: ignore[no-untyped-def]
    outbox_key, provider_key = _scheduled_morning_memo_idempotency_keys(
        principal_id=principal_id,
        schedule_key=schedule_key,
        local_day=local_day,
        delivery_channel=delivery_channel,
        digest_key=digest_key,
    )
    provider_idempotency_supported = delivery_channel == "email"
    request_payload = {
        "delivery_kind": "scheduled_morning_memo",
        "schedule_key": schedule_key,
        "local_day": local_day,
        "digest_key": digest_key,
        "role": role,
        "display_name": display_name,
        "delivery_channel": delivery_channel,
    }
    row = container.channel_runtime.queue_delivery(
        channel=delivery_channel,
        recipient=recipient_email,
        content=json.dumps(request_payload, sort_keys=True, separators=(",", ":")),
        metadata={
            **request_payload,
            "principal_id": principal_id,
            "provider_idempotency_key": provider_key,
            "provider_idempotency_supported": provider_idempotency_supported,
            "max_attempts": max_attempts,
            "retry_after_minutes": retry_after_minutes,
            "defer_if_focus": False,
        },
        principal_id=principal_id,
        idempotency_key=outbox_key,
    )
    persisted_request = dict(row.metadata or {})
    delivery_channel = (
        str(persisted_request.get("delivery_channel") or row.channel or delivery_channel).strip().lower()
        or delivery_channel
    )
    digest_key = str(persisted_request.get("digest_key") or digest_key).strip().lower() or digest_key
    recipient_email = str(row.recipient or recipient_email).strip() or recipient_email
    role = str(persisted_request.get("role") or role).strip().lower() or role
    display_name = str(persisted_request.get("display_name") or display_name).strip() or display_name
    provider_key = str(persisted_request.get("provider_idempotency_key") or provider_key).strip() or provider_key
    provider_idempotency_supported = bool(
        persisted_request.get("provider_idempotency_supported") is True
        or str(persisted_request.get("provider_idempotency_supported") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    try:
        max_attempts = max(1, int(persisted_request.get("max_attempts") or max_attempts))
    except (TypeError, ValueError):
        max_attempts = max(1, int(max_attempts))
    try:
        retry_after_minutes = max(
            5,
            int(persisted_request.get("retry_after_minutes") or retry_after_minutes),
        )
    except (TypeError, ValueError):
        retry_after_minutes = max(5, int(retry_after_minutes))
    result: dict[str, object] = {
        "outcome": "queued",
        "outbox": row,
        "payload": {},
        "delivery_channel": delivery_channel,
        "queued": 1
        if str(row.status or "").strip().lower() == "queued" and int(row.attempt_count or 0) == 0
        else 0,
        "claimed": 0,
        "claim_conflicts": 0,
        "retried": 0,
        "dead_lettered": 0,
    }
    if str(row.status or "").strip().lower() == "sent":
        result["outcome"] = "already_sent"
        result["queued"] = 0
        return result
    if str(row.status or "").strip().lower() == "dead_lettered":
        result["outcome"] = "dead_lettered"
        result["queued"] = 0
        result["dead_lettered"] = 1
        return result

    lease_owner = _scheduler_delivery_lease_owner()
    claimed = container.channel_runtime.claim_delivery(
        row.delivery_id,
        lease_owner=lease_owner,
        lease_seconds=_scheduler_morning_memo_lease_seconds(),
        now=observed_at,
    )
    if claimed is None:
        current = container.channel_runtime.get_delivery(
            row.delivery_id,
            principal_id=principal_id,
        )
        current_status = str(getattr(current, "status", "") or "").strip().lower()
        if current_status == "dead_lettered":
            result["outcome"] = "dead_lettered"
            result["dead_lettered"] = 1
        elif current_status == "sent":
            result["outcome"] = "already_sent"
        elif current_status == "retry":
            result["outcome"] = "retry_deferred"
        else:
            result["outcome"] = "claim_conflict"
            result["claim_conflicts"] = 1
        result["outbox"] = current or row
        return result

    result["claimed"] = 1
    attempt = container.channel_runtime.begin_delivery_attempt(
        claimed.delivery_id,
        principal_id=principal_id,
        lease_owner=lease_owner,
        now=observed_at,
    )
    if attempt is None:
        result["outcome"] = "claim_conflict"
        result["claim_conflicts"] = 1
        return result
    result["outbox"] = attempt

    try:
        payload = service.issue_channel_digest_delivery(
            principal_id=principal_id,
            digest_key=digest_key,
            recipient_email=recipient_email,
            role=role,
            display_name=display_name,
            operator_id="",
            delivery_channel=delivery_channel,
            expires_in_hours=72,
            base_url=_scheduler_public_base_url(),
            idempotency_key=provider_key,
        )
    except Exception as exc:
        dead_letter = (not provider_idempotency_supported) or attempt.attempt_count >= max_attempts
        failed_row = container.channel_runtime.mark_delivery_failed(
            attempt.delivery_id,
            principal_id=principal_id,
            error=f"delivery_dispatch_failed:{exc.__class__.__name__}",
            next_attempt_at=(observed_at + timedelta(minutes=retry_after_minutes)).isoformat(),
            dead_letter=dead_letter,
            lease_owner=lease_owner,
        )
        if failed_row is None:
            result["outcome"] = "completion_ownership_lost"
            return result
        result["outbox"] = failed_row
        result["outcome"] = "dead_lettered" if dead_letter else "retry"
        result["dead_lettered" if dead_letter else "retried"] = 1
        return result

    normalized_payload = dict(payload or {})
    result["payload"] = normalized_payload
    delivery_status = str(
        normalized_payload.get("telegram_delivery_status")
        if delivery_channel == "telegram"
        else normalized_payload.get("email_delivery_status") or ""
    ).strip().lower()
    if delivery_status == "sent":
        sent_row = container.channel_runtime.mark_delivery_sent(
            attempt.delivery_id,
            principal_id=principal_id,
            receipt_json={
                "provider": str(normalized_payload.get("email_provider") or delivery_channel).strip(),
                "provider_message_id": str(normalized_payload.get("email_message_id") or "").strip(),
                "telegram_message_ids": list(normalized_payload.get("telegram_message_ids") or []),
                "delivery_id": str(normalized_payload.get("delivery_id") or "").strip(),
            },
            lease_owner=lease_owner,
        )
        if sent_row is None:
            # Preserve dispatching state. A later email claim repeats the same
            # provider key; a later Telegram claim dead-letters as ambiguous.
            result["outcome"] = "completion_ownership_lost"
            return result
        result["outbox"] = sent_row
        result["outcome"] = "sent"
        return result

    dead_letter = (not provider_idempotency_supported) or attempt.attempt_count >= max_attempts
    failed_row = container.channel_runtime.mark_delivery_failed(
        attempt.delivery_id,
        principal_id=principal_id,
        error=f"{delivery_channel}_delivery_{delivery_status or 'failed'}",
        next_attempt_at=(observed_at + timedelta(minutes=retry_after_minutes)).isoformat(),
        dead_letter=dead_letter,
        lease_owner=lease_owner,
    )
    if failed_row is None:
        result["outcome"] = "completion_ownership_lost"
        return result
    result["outbox"] = failed_row
    result["outcome"] = "dead_lettered" if dead_letter else "retry"
    result["dead_lettered" if dead_letter else "retried"] = 1
    return result


def _run_scheduler_morning_memo_delivery(
    container,
    log: logging.Logger,
    *,
    now_utc: datetime | None = None,
) -> dict[str, object]:  # type: ignore[no-untyped-def]
    from app.product.service import build_product_service
    from app.services.google_oauth import GOOGLE_CONNECTOR_NAME
    from app.services.registration_email import email_delivery_enabled
    from app.services.telegram_onboarding_service import TELEGRAM_IDENTITY_CONNECTOR

    observed_at = now_utc or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    service = build_product_service(container)
    google_bindings = [
        binding
        for binding in container.tool_runtime.list_connector_bindings_for_connector(GOOGLE_CONNECTOR_NAME, limit=1000)
        if str(binding.status or "").strip().lower() == "enabled" and str(binding.principal_id or "").strip()
    ]
    telegram_bindings = [
        binding
        for binding in container.tool_runtime.list_connector_bindings_for_connector(TELEGRAM_IDENTITY_CONNECTOR, limit=1000)
        if str(binding.status or "").strip().lower() == "enabled" and str(binding.principal_id or "").strip()
    ]
    google_bindings_by_principal: dict[str, list[object]] = {}
    for binding in google_bindings:
        principal_id = str(binding.principal_id or "").strip()
        if principal_id:
            google_bindings_by_principal.setdefault(principal_id, []).append(binding)
    principals = {
        str(binding.principal_id or "").strip()
        for binding in [*google_bindings, *telegram_bindings]
        if str(binding.principal_id or "").strip()
    }

    configured = 0
    due = 0
    sent = 0
    blocked = 0
    failed = 0
    skipped = 0
    error_count = 0
    queued = 0
    claimed = 0
    claim_conflicts = 0
    retried = 0
    dead_lettered = 0

    for principal_id in sorted(principals):
        try:
            principal_bindings = list(google_bindings_by_principal.get(principal_id, []))
            preferences = container.memory_runtime.list_delivery_preferences(
                principal_id=principal_id,
                limit=50,
                status="active",
            )
            preference = next(
                (
                    row
                    for row in preferences
                    if str(dict(row.format_json or {}).get("schedule_kind") or "").strip().lower() in {"morning_memo", "assistant_nudge"}
                ),
                None,
            )
            if preference is None:
                continue
            configured += 1
            quiet_hours = dict(preference.quiet_hours_json or {})
            format_json = dict(preference.format_json or {})
            local_now = observed_at.astimezone(_schedule_timezone(str(quiet_hours.get("timezone") or "UTC")))
            if not _morning_memo_cadence_allows_now(str(preference.cadence or "daily_morning"), local_now=local_now):
                skipped += 1
                continue
            delivery_time = _normalize_scheduler_time(
                str(quiet_hours.get("delivery_time_local") or ""),
                default="08:00",
            )
            delivery_start = local_now.replace(
                hour=delivery_time[0],
                minute=delivery_time[1],
                second=0,
                microsecond=0,
            )
            delivery_window_minutes = max(
                int(quiet_hours.get("delivery_window_minutes") or _SCHEDULER_MORNING_MEMO_DELIVERY_WINDOW_MINUTES),
                15,
            )
            delivery_end = delivery_start + timedelta(minutes=delivery_window_minutes)
            if local_now < delivery_start or local_now >= delivery_end:
                skipped += 1
                continue
            due += 1
            quiet_start = _normalize_scheduler_time(
                str(quiet_hours.get("quiet_hours_start") or ""),
                default="20:00",
            )
            quiet_end = _normalize_scheduler_time(
                str(quiet_hours.get("quiet_hours_end") or ""),
                default="07:00",
            )
            local_day = local_now.date().isoformat()
            schedule_key = str(preference.preference_id or f"morning-memo:{principal_id}").strip()
            sent_dedupe = f"{principal_id}|scheduled-morning-memo|{schedule_key}|{local_day}|sent"
            if _is_local_time_within_quiet_hours(local_now, quiet_start=quiet_start, quiet_end=quiet_end):
                blocked += 1
                container.channel_runtime.ingest_observation(
                    principal_id=principal_id,
                    channel="product",
                    event_type="scheduled_morning_memo_delivery_blocked",
                    payload={
                        "schedule_key": schedule_key,
                        "local_day": local_day,
                        "reason": "quiet_hours",
                        "delivery_time_local": f"{delivery_time[0]:02d}:{delivery_time[1]:02d}",
                    },
                    source_id=schedule_key,
                    dedupe_key=f"{principal_id}|scheduled-morning-memo|{schedule_key}|{local_day}|quiet-hours",
                )
                continue
            retry_after_minutes = max(
                int(format_json.get("retry_after_minutes") or _SCHEDULER_MORNING_MEMO_RETRY_AFTER_MINUTES),
                5,
            )
            delivery_channel = str(format_json.get("delivery_channel") or preference.channel or "email").strip().lower() or "email"
            if delivery_channel not in {"email", "telegram"}:
                blocked += 1
                container.channel_runtime.ingest_observation(
                    principal_id=principal_id,
                    channel="product",
                    event_type="scheduled_morning_memo_delivery_blocked",
                    payload={
                        "schedule_key": schedule_key,
                        "local_day": local_day,
                        "reason": "unsupported_delivery_channel",
                        "delivery_channel": delivery_channel,
                    },
                    source_id=schedule_key,
                    dedupe_key=f"{principal_id}|scheduled-morning-memo|{schedule_key}|{local_day}|unsupported-channel",
                )
                continue
            explicit_email = str(format_json.get("recipient_email") or "").strip().lower()
            google_email = next(
                (
                    str(
                        dict(getattr(binding, "auth_metadata_json", {}) or {}).get("google_email")
                        or getattr(binding, "external_account_ref", "")
                        or ""
                    ).strip().lower()
                    for binding in principal_bindings
                    if str(
                        dict(getattr(binding, "auth_metadata_json", {}) or {}).get("google_email")
                        or getattr(binding, "external_account_ref", "")
                        or ""
                    ).strip()
                ),
                "",
            )
            recipient_email = explicit_email or google_email or principal_id
            if not recipient_email:
                blocked += 1
                container.channel_runtime.ingest_observation(
                    principal_id=principal_id,
                    channel="product",
                    event_type="scheduled_morning_memo_delivery_blocked",
                    payload={
                        "schedule_key": schedule_key,
                        "local_day": local_day,
                        "reason": "recipient_missing",
                    },
                    source_id=schedule_key,
                    dedupe_key=f"{principal_id}|scheduled-morning-memo|{schedule_key}|{local_day}|recipient-missing",
                )
                continue
            digest_key = str(format_json.get("digest_key") or "memo").strip().lower() or "memo"
            digest = service.channel_digest_pack(
                principal_id=principal_id,
                digest_key=digest_key,
                operator_id="",
            )
            if digest is None or (digest_key == "assistant_nudge" and not list(digest.get("items") or [])):
                skipped += 1
                continue
            if delivery_channel == "email" and not email_delivery_enabled():
                blocked += 1
                container.channel_runtime.ingest_observation(
                    principal_id=principal_id,
                    channel="product",
                    event_type="scheduled_morning_memo_delivery_blocked",
                    payload={
                        "schedule_key": schedule_key,
                        "local_day": local_day,
                        "reason": "email_delivery_not_configured",
                        "recipient_email": recipient_email,
                    },
                    source_id=schedule_key,
                    dedupe_key=f"{principal_id}|scheduled-morning-memo|{schedule_key}|{local_day}|email-not-configured",
                )
                continue
            dispatch = _dispatch_scheduled_morning_memo(
                container=container,
                service=service,
                observed_at=observed_at,
                principal_id=principal_id,
                schedule_key=schedule_key,
                local_day=local_day,
                digest_key=digest_key,
                recipient_email=recipient_email,
                role=str(format_json.get("role") or "principal").strip().lower() or "principal",
                display_name=str(format_json.get("display_name") or recipient_email or "Workspace Principal").strip(),
                delivery_channel=delivery_channel,
                retry_after_minutes=retry_after_minutes,
                max_attempts=_scheduler_morning_memo_max_attempts(
                    format_json.get("max_delivery_attempts")
                ),
            )
            queued += int(dispatch.get("queued") or 0)
            claimed += int(dispatch.get("claimed") or 0)
            claim_conflicts += int(dispatch.get("claim_conflicts") or 0)
            retried += int(dispatch.get("retried") or 0)
            dead_lettered += int(dispatch.get("dead_lettered") or 0)
            outcome = str(dispatch.get("outcome") or "failed").strip().lower()
            delivery_channel = str(
                dispatch.get("delivery_channel") or delivery_channel
            ).strip().lower() or delivery_channel
            payload = dict(dispatch.get("payload") or {})
            outbox_row = dispatch.get("outbox")
            outbox_id = str(getattr(outbox_row, "delivery_id", "") or "").strip()
            attempt_count = max(0, int(getattr(outbox_row, "attempt_count", 0) or 0))
            if outcome == "sent":
                sent += 1
                container.channel_runtime.ingest_observation(
                    principal_id=principal_id,
                    channel="product",
                    event_type="scheduled_morning_memo_delivery_sent",
                    payload={
                        "schedule_key": schedule_key,
                        "local_day": local_day,
                        "delivery_id": str(payload.get("delivery_id") or "").strip(),
                        "outbox_delivery_id": outbox_id,
                        "recipient_email": recipient_email,
                        "digest_key": str(payload.get("digest_key") or "memo").strip(),
                        "delivery_channel": delivery_channel,
                        "attempt_count": attempt_count,
                    },
                    source_id=outbox_id or str(payload.get("delivery_id") or schedule_key).strip() or schedule_key,
                    dedupe_key=sent_dedupe,
                )
                continue
            if outcome in {"already_sent", "claim_conflict"}:
                skipped += 1
                continue
            if outcome == "retry_deferred":
                blocked += 1
                continue
            failed += 1
            failure_bucket = int(observed_at.timestamp()) // max(retry_after_minutes * 60, 60)
            container.channel_runtime.ingest_observation(
                principal_id=principal_id,
                channel="product",
                event_type="scheduled_morning_memo_delivery_failed",
                payload={
                    "schedule_key": schedule_key,
                    "local_day": local_day,
                    "delivery_id": str(payload.get("delivery_id") or "").strip(),
                    "outbox_delivery_id": outbox_id,
                    "recipient_email": recipient_email,
                    "digest_key": str(payload.get("digest_key") or digest_key).strip(),
                    "delivery_channel": delivery_channel,
                    "outcome": outcome,
                    "attempt_count": attempt_count,
                    "email_delivery_status": str(payload.get("email_delivery_status") or "").strip(),
                    "telegram_delivery_status": str(payload.get("telegram_delivery_status") or "").strip(),
                },
                source_id=outbox_id or str(payload.get("delivery_id") or schedule_key).strip() or schedule_key,
                dedupe_key=f"{principal_id}|scheduled-morning-memo|{schedule_key}|{local_day}|failed|{failure_bucket}|{attempt_count}",
            )
        except Exception:
            error_count += 1
            log.exception(
                "scheduler morning memo delivery failed principal_ref=%s",
                _scheduler_log_ref(principal_id),
            )
    summary = {
        "ran": True,
        "configured": configured,
        "due": due,
        "sent": sent,
        "blocked": blocked,
        "failed": failed,
        "skipped": skipped,
        "errors": error_count,
        "queued": queued,
        "claimed": claimed,
        "claim_conflicts": claim_conflicts,
        "retried": retried,
        "dead_lettered": dead_lettered,
    }
    _record_scheduler_delivery_metrics(summary)
    return summary


def _parse_runner_isoish_datetime(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _derive_telegram_async_message_id(dedupe_key: str, fallback_external_id: str) -> str:
    normalized = str(dedupe_key or "").strip()
    if normalized:
        parts = [part for part in normalized.split(":") if part]
        if len(parts) >= 3:
            return str(parts[-1]).strip()
    return str(fallback_external_id or "").strip()


def _run_scheduler_telegram_async_recovery(container, log: logging.Logger) -> dict[str, object]:  # type: ignore[no-untyped-def]
    drained = 0
    pending = 0
    skipped = 0
    errors = 0
    observed_at = datetime.now(timezone.utc)
    min_age_seconds = max(_scheduler_telegram_async_recovery_min_age_seconds(), 5.0)
    for row in container.channel_runtime.list_recent_observations(limit=400):
        if str(getattr(row, "channel", "") or "").strip() != "telegram":
            continue
        if str(getattr(row, "event_type", "") or "").strip() != "telegram.reply_async_started":
            continue
        principal_id = str(getattr(row, "principal_id", "") or "").strip()
        payload = dict(getattr(row, "payload", {}) or {})
        chat_id = str(payload.get("chat_id") or "").strip()
        prompt_text = str(payload.get("prompt_text") or "").strip()
        dedupe_key = str(payload.get("dedupe_key") or getattr(row, "external_id", "") or "").strip()
        current_message_id = _derive_telegram_async_message_id(
            dedupe_key,
            str(getattr(row, "external_id", "") or "").strip(),
        )
        created_at = _parse_runner_isoish_datetime(getattr(row, "created_at", "") or "")
        if not principal_id or not chat_id or not prompt_text or not current_message_id or not created_at:
            skipped += 1
            continue
        if max((observed_at - created_at).total_seconds(), 0.0) < min_age_seconds:
            pending += 1
            continue
        sent_dedupe = f"{current_message_id}:assistant_async_sent"
        failed_dedupe = f"{current_message_id}:assistant_async_failed"
        if container.channel_runtime.find_observation_by_dedupe(sent_dedupe, principal_id=principal_id) is not None:
            skipped += 1
            continue
        if container.channel_runtime.find_observation_by_dedupe(failed_dedupe, principal_id=principal_id) is not None:
            skipped += 1
            continue
        bot_key = str(payload.get("bot_key") or "").strip()
        bot_handle = str(payload.get("bot_handle") or "").strip()
        try:
            bot_config = _resolve_telegram_bot_config(bot_key=bot_key)
            if not bot_config and bot_handle:
                bot_config = _resolve_telegram_bot_config()
                if str(bot_config.get("handle") or "").strip() != bot_handle:
                    bot_config = {}
            if not bot_config:
                skipped += 1
                continue
            _telegram_async_assistant_reply_worker(
                container=container,
                principal_id=principal_id,
                bot_config=dict(bot_config),
                chat_id=chat_id,
                text=prompt_text,
                current_message_id=current_message_id,
            )
            drained += 1
        except Exception:
            errors += 1
            log.exception(
                "scheduler telegram async outbox drain failed principal_ref=%s chat_ref=%s message_ref=%s",
                _scheduler_log_ref(principal_id),
                _scheduler_log_ref(chat_id),
                _scheduler_log_ref(current_message_id),
            )
    return {
        "ran": True,
        "drained": drained,
        "pending": pending,
        "skipped": skipped,
        "errors": errors,
    }


def _run_role_heartbeat_loop(*, role: str, stop_event: threading.Event) -> None:
    while not stop_event.wait(30.0):
        _write_scheduler_heartbeat(role=role, status="serving")


def _run_api() -> None:
    s = get_settings()
    stop_event = threading.Event()
    _write_scheduler_heartbeat(role="api", status="startup")
    heartbeat_thread = threading.Thread(
        target=_run_role_heartbeat_loop,
        kwargs={"role": "api", "stop_event": stop_event},
        name="property-search-api-writer-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        uvicorn.run(
            "app.main:app",
            host=s.host,
            port=s.port,
            log_level=s.log_level.lower(),
            log_config=None,
            ws_ping_interval=None,
            ws_ping_timeout=None,
        )
    finally:
        stop_event.set()
        _write_scheduler_heartbeat(role="api", status="stopped")


def _run_openvoice() -> None:
    host = str(os.environ.get("OPENVOICE_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    raw_port = str(os.environ.get("OPENVOICE_PORT") or "8093").strip()
    try:
        port = int(raw_port)
    except ValueError:
        port = 8093
    log_level = str(os.environ.get("OPENVOICE_LOG_LEVEL") or get_settings().log_level).strip().lower() or "info"
    uvicorn.run(
        "app.openvoice_app:app",
        host=host,
        port=port,
        log_level=log_level,
        log_config=None,
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )


def _run_property_search_work_once(container, *, role: str, log: logging.Logger) -> dict[str, object]:  # type: ignore[no-untyped-def]
    _record_property_search_queue_metrics(None)
    database_url = str(os.environ.get("DATABASE_URL") or "").strip()
    if not database_url:
        _write_scheduler_heartbeat(role=role, status="loop")
        return {"claimed": False, "reason": "database_url_missing"}

    from app.product.property_search_work_queue import (
        PostgresPropertySearchWorkQueue,
        property_search_work_heartbeat_seconds,
        property_search_work_lease_seconds,
    )
    from app.product.service import build_product_service
    from app.telemetry import (
        TraceContextError,
        persisted_trace_parent_from_payload,
        start_span,
    )

    try:
        repository = PostgresPropertySearchWorkQueue(database_url)
    except Exception:
        _write_scheduler_heartbeat(role=role, status="loop")
        raise
    _refresh_property_search_queue_metrics(repository)
    _write_scheduler_heartbeat(role=role, status="loop")
    lease_seconds = property_search_work_lease_seconds()
    instance_host = str(os.environ.get("HOSTNAME") or "local").strip() or "local"
    lease_owner = (
        f"{str(role or 'worker').strip() or 'worker'}:{instance_host}:{os.getpid()}:{_EXECUTION_INSTANCE_ID}"
    )
    job = repository.claim(lease_owner=lease_owner, lease_seconds=lease_seconds)
    if job is None:
        return {"claimed": False}
    try:
        telemetry_parent = persisted_trace_parent_from_payload(job.payload_json)
    except TraceContextError:
        failed = repository.fail(
            job_id=job.job_id,
            lease_owner=lease_owner,
            error="property_search_trace_context_invalid",
        )
        _refresh_property_search_work_queue_metrics(repository, log=log)
        _write_scheduler_heartbeat(role=role, status="loop")
        log.error(
            "role=%s property search work rejected invalid trace context job=%s run=%s",
            role,
            job.job_id,
            job.run_id,
        )
        return {
            "claimed": True,
            "completed": False,
            "job_id": job.job_id,
            "run_id": job.run_id,
            "status": str(failed.status if failed is not None else "lease_lost"),
            "attempt_count": job.attempt_count,
        }

    parent_trace = runtime_trace_context_from_mapping(job.payload_json.get("trace_context"))
    worker_trace = child_trace_context(parent_trace) if parent_trace is not None else None
    trace_fields = (
        {
            "trace_id": worker_trace.trace_id,
            "span_id": worker_trace.span_id,
            "parent_span_id": worker_trace.parent_span_id,
            "trace_flags": worker_trace.trace_flags,
            "trace_source": worker_trace.source,
        }
        if worker_trace is not None
        else {}
    )
    build_identity = runtime_build_identity()
    trace_fields.update(build_identity)
    correlation_id = str(
        dict(job.payload_json.get("trace_context") or {}).get("correlation_id")
        if isinstance(job.payload_json.get("trace_context"), dict)
        else ""
    ).strip()
    log_event(
        log,
        logging.INFO,
        "property_search_work_started",
        correlation_id=correlation_id,
        component="property_search_worker",
        operation="execute",
        outcome="started",
        job_id=job.job_id,
        run_id=job.run_id,
        **trace_fields,
    )

    stop_heartbeat = threading.Event()
    lease_lost = threading.Event()

    def _heartbeat() -> None:
        interval = float(property_search_work_heartbeat_seconds())
        while not stop_heartbeat.wait(interval):
            try:
                if not repository.heartbeat(
                    job_id=job.job_id,
                    lease_owner=lease_owner,
                    lease_seconds=lease_seconds,
                ):
                    lease_lost.set()
                    return
                _refresh_property_search_queue_metrics(repository)
                _write_scheduler_heartbeat(role=role, status="loop")
            except Exception:
                _record_property_search_queue_metrics(None)
                _write_scheduler_heartbeat(role=role, status="loop")
                log.exception("role=%s property search work heartbeat failed job=%s", role, job.job_id)

    heartbeat_thread = threading.Thread(
        target=_heartbeat,
        name=f"property-search-heartbeat-{job.job_id[:8]}",
        daemon=False,
    )
    heartbeat_thread.start()
    try:
        service = build_product_service(container)
        with bind_runtime_trace_context(
            worker_trace,
            correlation_id=correlation_id,
        ):
            result = service.execute_property_search_work_job(job)
    except Exception as exc:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=max(1.0, float(property_search_work_heartbeat_seconds()) + 1.0))
        failed = repository.fail(
            job_id=job.job_id,
            lease_owner=lease_owner,
            error=str(exc or "property search work failed"),
        )
        _refresh_property_search_queue_metrics(repository)
        _write_scheduler_heartbeat(role=role, status="loop")
        log.exception(
            "role=%s property search work failed job=%s run=%s attempt=%s terminal=%s",
            role,
            job.job_id,
            job.run_id,
            job.attempt_count,
            bool(failed and failed.status == "failed"),
        )
        log_event(
            log,
            logging.ERROR,
            "property_search_work_failed",
            correlation_id=correlation_id,
            component="property_search_worker",
            operation="execute",
            outcome="failed",
            job_id=job.job_id,
            run_id=job.run_id,
            error_type=exc.__class__.__name__,
            **trace_fields,
        )
        return {
            "claimed": True,
            "completed": True,
            "job_id": job.job_id,
            "run_id": job.run_id,
            "status": completed.status,
            "attempt_count": completed.attempt_count,
            "result_status": str(result.get("status") or "completed"),
        }
    stop_heartbeat.set()
    heartbeat_thread.join(timeout=max(1.0, float(property_search_work_heartbeat_seconds()) + 1.0))
    completed = None if lease_lost.is_set() else repository.complete(job_id=job.job_id, lease_owner=lease_owner)
    _refresh_property_search_queue_metrics(repository)
    _write_scheduler_heartbeat(role=role, status="loop")
    if completed is None:
        log.error("role=%s property search work completion lost lease job=%s run=%s", role, job.job_id, job.run_id)
        log_event(
            log,
            logging.ERROR,
            "property_search_work_lease_lost",
            correlation_id=correlation_id,
            component="property_search_worker",
            operation="execute",
            outcome="lease_lost",
            job_id=job.job_id,
            run_id=job.run_id,
            **trace_fields,
        )
        return {
            "claimed": True,
            "completed": False,
            "job_id": job.job_id,
            "run_id": job.run_id,
            "status": "lease_lost",
            "attempt_count": job.attempt_count,
        }
    log_event(
        log,
        logging.INFO,
        "property_search_work_completed",
        correlation_id=correlation_id,
        component="property_search_worker",
        operation="execute",
        outcome="completed",
        job_id=job.job_id,
        run_id=job.run_id,
        **trace_fields,
    )
    return {
        "claimed": True,
        "completed": True,
        "job_id": job.job_id,
        "run_id": job.run_id,
        "status": completed.status,
        "attempt_count": completed.attempt_count,
        "result_status": str(result.get("status") or "completed"),
    }


def _require_property_search_writer_readiness(container: object, *, role: str) -> None:
    normalized_role = str(role or "").strip().lower()
    if normalized_role not in {"worker", "scheduler"}:
        return
    readiness = getattr(container, "readiness", None)
    probe = getattr(readiness, "_probe_database", None)
    if not callable(probe):
        raise RuntimeError("property_search_writer_not_ready:readiness_probe_missing")
    ready, reason = probe()
    if ready:
        return
    bounded_reason = re.sub(
        r"[^a-zA-Z0-9_.:-]+",
        "_",
        str(reason or "not_ready").strip(),
    )[:240]
    raise RuntimeError(
        f"property_search_writer_not_ready:{bounded_reason or 'not_ready'}"
    )


def _run_execution_worker(role: str) -> None:
    stop_event = threading.Event()

    def _handle_stop(signum, frame):  # type: ignore[no-untyped-def]
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    log = logging.getLogger("ea.runner")
    container = build_container()
    _require_property_search_writer_readiness(container, role=role)
    idle_backoff_seconds = _IDLE_BACKOFF_START_SECONDS
    last_horizon_scan_at = 0.0
    last_onemin_refresh_at = 0.0
    last_google_signal_sync_at = 0.0
    now_at_startup = time.time()
    last_property_scout_at = 0.0
    last_property_search_recovery_at = now_at_startup
    last_property_results_finalize_at = now_at_startup
    last_pocket_signal_sync_at = 0.0
    last_morning_memo_at = 0.0
    last_telegram_async_recovery_at = 0.0
    property_only_scheduler = role == "scheduler" and _scheduler_property_only_profile_enabled()
    property_only_worker = role == "worker" and _worker_property_only_profile_enabled()
    log.info("role=%s started worker loop", role)
    _write_scheduler_heartbeat(role=role, status="started")
    while not stop_event.is_set():
        _write_scheduler_heartbeat(role=role, status="loop")
        if role == "scheduler":
            now = time.time()
            if now - last_property_search_recovery_at >= _scheduler_property_search_recovery_interval_seconds():
                try:
                    recovery_summary = _run_scheduler_step_with_heartbeat(
                        role=role,
                        step_name="property_search_recovery",
                        timeout_seconds=_scheduler_property_search_recovery_timeout_seconds(),
                        timeout_result={
                            "ran": True,
                            "scanned": 0,
                            "stale_total": 0,
                            "repaired": 0,
                            "replacement_started": 0,
                            "errors": 1,
                        },
                        log=log,
                        fn=lambda: _run_scheduler_property_search_recovery(container, log),
                        stop_event=stop_event,
                    )
                    if stop_event.is_set():
                        break
                    last_property_search_recovery_at = now
                    if (
                        int(recovery_summary.get("stale_total") or 0) > 0
                        or int(recovery_summary.get("errors") or 0) > 0
                        or bool(recovery_summary.get("timeout"))
                    ):
                        log.info(
                            "role=%s scheduler property search recovery scanned=%s stale=%s repaired=%s replacements=%s errors=%s timeout=%s",
                            role,
                            recovery_summary.get("scanned"),
                            recovery_summary.get("stale_total"),
                            recovery_summary.get("repaired"),
                            recovery_summary.get("replacement_started"),
                            recovery_summary.get("errors"),
                            recovery_summary.get("timeout"),
                        )
                except Exception:
                    log.exception("role=%s scheduler property search recovery failed", role)
                    last_property_search_recovery_at = now
            if not property_only_scheduler and now - last_horizon_scan_at >= _SCHEDULER_SCAN_INTERVAL_SECONDS:
                observed_at = datetime.now(timezone.utc)
                try:
                    candidates = container.proactive_horizon.scan(now=observed_at)
                    refreshed_principals = {
                        str(row.principal_id or "").strip()
                        for row in candidates
                        if str(row.principal_id or "").strip()
                    }
                    for principal_id in sorted(refreshed_principals):
                        container.cognitive_load.refresh_for_principal(
                            principal_id,
                            now=observed_at,
                        )
                    launched = container.proactive_horizon.run_once(now=observed_at)
                    if launched:
                        log.info("role=%s proactive horizon launched=%s", role, len(launched))
                    if refreshed_principals:
                        log.debug("role=%s cognitive-load refreshed principals=%s", role, len(refreshed_principals))
                except Exception:
                    log.exception("role=%s proactive horizon scan failed", role)
                last_horizon_scan_at = now
            if not property_only_scheduler and now - last_onemin_refresh_at >= _scheduler_onemin_refresh_interval_seconds():
                try:
                    refresh_summary = _run_scheduler_onemin_billing_refresh(container, log)
                    if bool(refresh_summary.get("ran")) and not bool(refresh_summary.get("throttled")):
                        last_onemin_refresh_at = now
                        log.info(
                            "role=%s scheduler onemin refresh browseract=%s/%s browseract_failed=%s members=%s api_attempted=%s api_recovered=%s api_rate_limited=%s errors=%s",
                            role,
                            refresh_summary.get("browseract_refreshed"),
                            refresh_summary.get("browseract_attempted"),
                            refresh_summary.get("browseract_failed"),
                            refresh_summary.get("member_reconciled"),
                            refresh_summary.get("api_attempted"),
                            refresh_summary.get("api_recovered"),
                            refresh_summary.get("api_rate_limited"),
                            refresh_summary.get("errors"),
                        )
                    elif bool(refresh_summary.get("throttled")):
                        throttle_seconds_remaining = max(
                            float(refresh_summary.get("throttle_seconds_remaining") or 0.0),
                            1.0,
                        )
                        last_onemin_refresh_at = now - _scheduler_onemin_refresh_interval_seconds() + throttle_seconds_remaining
                        log.info(
                            "role=%s scheduler onemin refresh throttled reason=%s retry_in=%.1fs",
                            role,
                            refresh_summary.get("throttle_reason"),
                            throttle_seconds_remaining,
                        )
                except Exception:
                    log.exception("role=%s scheduler onemin refresh failed", role)
            if not property_only_scheduler and _scheduler_google_signal_sync_enabled() and (
                now - last_google_signal_sync_at >= _scheduler_google_signal_sync_interval_seconds()
            ):
                try:
                    sync_summary = _run_scheduler_google_signal_sync(container, log)
                    last_google_signal_sync_at = now
                    log.info(
                        "role=%s scheduler google signal sync attempted=%s synced=%s errors=%s",
                        role,
                        sync_summary.get("attempted"),
                        sync_summary.get("synced"),
                        sync_summary.get("errors"),
                    )
                except Exception:
                    log.exception("role=%s scheduler google signal sync failed", role)
                    last_google_signal_sync_at = now
            if _scheduler_property_scout_enabled() and (
                now - last_property_scout_at >= _scheduler_property_scout_interval_seconds()
            ):
                try:
                    scout_summary = _run_scheduler_step_with_heartbeat(
                        role=role,
                        step_name="property_scout",
                        timeout_seconds=_scheduler_property_scout_timeout_seconds(),
                        timeout_result={
                            "ran": True,
                            "attempted": 0,
                            "synced": 0,
                            "errors": 1,
                            "principal_count": 0,
                        },
                        log=log,
                        fn=lambda: _run_scheduler_property_scout(container, log),
                        stop_event=stop_event,
                    )
                    if stop_event.is_set():
                        break
                    last_property_scout_at = now
                    log.info(
                        "role=%s scheduler property scout attempted=%s synced=%s launched=%s due=%s skipped_active=%s skipped_not_due=%s errors=%s principal_count=%s timeout=%s",
                        role,
                        scout_summary.get("attempted"),
                        scout_summary.get("synced"),
                        scout_summary.get("launched"),
                        scout_summary.get("due"),
                        scout_summary.get("skipped_active"),
                        scout_summary.get("skipped_not_due"),
                        scout_summary.get("errors"),
                        scout_summary.get("principal_count"),
                        scout_summary.get("timeout"),
                    )
                except Exception:
                    log.exception("role=%s scheduler property scout failed", role)
                    last_property_scout_at = now
            if now - last_property_results_finalize_at >= _scheduler_property_results_finalize_interval_seconds():
                try:
                    finalize_summary = _run_scheduler_step_with_heartbeat(
                        role=role,
                        step_name="property_results_finalize",
                        timeout_seconds=_scheduler_property_results_finalize_timeout_seconds(),
                        timeout_result={
                            "ran": True,
                            "attempted": 0,
                            "finalized": 0,
                            "emailed": 0,
                            "pending": 0,
                            "repair_resolved_total": 0,
                            "repair_deferred_total": 0,
                            "errors": 1,
                        },
                        log=log,
                        fn=lambda: _run_scheduler_property_results_finalize(container, log),
                        stop_event=stop_event,
                    )
                    if stop_event.is_set():
                        break
                    last_property_results_finalize_at = now
                    if (
                        int(finalize_summary.get("attempted") or 0) > 0
                        or int(finalize_summary.get("emailed") or 0) > 0
                        or int(finalize_summary.get("pending") or 0) > 0
                        or bool(finalize_summary.get("timeout"))
                    ):
                        log.info(
                            "role=%s scheduler property results finalize attempted=%s finalized=%s emailed=%s pending=%s errors=%s timeout=%s",
                            role,
                            finalize_summary.get("attempted"),
                            finalize_summary.get("finalized"),
                            finalize_summary.get("emailed"),
                            finalize_summary.get("pending"),
                            finalize_summary.get("errors"),
                            finalize_summary.get("timeout"),
                        )
                except Exception:
                    log.exception("role=%s scheduler property results finalize failed", role)
                    last_property_results_finalize_at = now
            if not property_only_scheduler and _scheduler_pocket_signal_sync_enabled() and (
                now - last_pocket_signal_sync_at >= _scheduler_pocket_signal_sync_interval_seconds()
            ):
                try:
                    sync_summary = _run_scheduler_pocket_signal_sync(container, log)
                    last_pocket_signal_sync_at = now
                    log.info(
                        "role=%s scheduler pocket signal sync attempted=%s synced=%s errors=%s principal_ref=%s",
                        role,
                        sync_summary.get("attempted"),
                        sync_summary.get("synced"),
                        sync_summary.get("errors"),
                        sync_summary.get("principal_ref", "none"),
                    )
                except Exception:
                    log.exception("role=%s scheduler pocket signal sync failed", role)
                    last_pocket_signal_sync_at = now
            if not property_only_scheduler and _scheduler_morning_memo_enabled() and (
                now - last_morning_memo_at >= _scheduler_morning_memo_interval_seconds()
            ):
                try:
                    memo_summary = _run_scheduler_morning_memo_delivery(container, log)
                    last_morning_memo_at = now
                    log.info(
                        "role=%s scheduler morning memo configured=%s due=%s queued=%s claimed=%s sent=%s retried=%s dead_lettered=%s claim_conflicts=%s blocked=%s failed=%s skipped=%s errors=%s",
                        role,
                        memo_summary.get("configured"),
                        memo_summary.get("due"),
                        memo_summary.get("queued"),
                        memo_summary.get("claimed"),
                        memo_summary.get("sent"),
                        memo_summary.get("retried"),
                        memo_summary.get("dead_lettered"),
                        memo_summary.get("claim_conflicts"),
                        memo_summary.get("blocked"),
                        memo_summary.get("failed"),
                        memo_summary.get("skipped"),
                        memo_summary.get("errors"),
                    )
                except Exception:
                    log.exception("role=%s scheduler morning memo delivery failed", role)
                    last_morning_memo_at = now
            if not property_only_scheduler and _scheduler_telegram_async_recovery_enabled() and (
                now - last_telegram_async_recovery_at >= _scheduler_telegram_async_recovery_interval_seconds()
            ):
                try:
                    recovery_summary = _run_scheduler_telegram_async_recovery(container, log)
                    last_telegram_async_recovery_at = now
                    log.info(
                        "role=%s scheduler telegram async outbox drained=%s pending=%s skipped=%s errors=%s",
                        role,
                        recovery_summary.get("drained"),
                        recovery_summary.get("pending"),
                        recovery_summary.get("skipped"),
                        recovery_summary.get("errors"),
                    )
                except Exception:
                    log.exception("role=%s scheduler telegram async outbox failed", role)
                    last_telegram_async_recovery_at = now
            if property_only_scheduler:
                log.debug("role=%s property-only scheduler idle; sleeping %.1fs", role, idle_backoff_seconds)
                _write_scheduler_heartbeat(role=role, status="idle")
                stop_event.wait(idle_backoff_seconds)
                idle_backoff_seconds = min(idle_backoff_seconds * 2.0, _IDLE_BACKOFF_MAX_SECONDS)
                continue
        if role == "worker":
            try:
                property_work = _run_property_search_work_once(container, role=role, log=log)
            except Exception:
                _record_property_search_queue_metrics(None)
                _write_scheduler_heartbeat(role=role, status="loop")
                log.exception("role=%s property search work queue failed; retrying in %.1fs", role, _ERROR_BACKOFF_SECONDS)
                stop_event.wait(_ERROR_BACKOFF_SECONDS)
                continue
            if bool(property_work.get("claimed")):
                idle_backoff_seconds = _IDLE_BACKOFF_START_SECONDS
                log.info(
                    "role=%s property search work job=%s run=%s status=%s attempt=%s",
                    role,
                    property_work.get("job_id"),
                    property_work.get("run_id"),
                    property_work.get("status"),
                    property_work.get("attempt_count"),
                )
                continue
        if property_only_worker:
            log.debug("role=%s property-only worker skips inherited generic queue; sleeping %.1fs", role, idle_backoff_seconds)
            stop_event.wait(idle_backoff_seconds)
            idle_backoff_seconds = min(idle_backoff_seconds * 2.0, _IDLE_BACKOFF_MAX_SECONDS)
            continue
        try:
            artifact = container.orchestrator.run_next_queue_item(lease_owner=role)
        except Exception:
            log.exception("role=%s queue execution failed; retrying in %.1fs", role, _ERROR_BACKOFF_SECONDS)
            stop_event.wait(_ERROR_BACKOFF_SECONDS)
            continue
        if artifact is None:
            log.debug("role=%s idle; sleeping %.1fs before next lease attempt", role, idle_backoff_seconds)
            stop_event.wait(idle_backoff_seconds)
            idle_backoff_seconds = min(idle_backoff_seconds * 2.0, _IDLE_BACKOFF_MAX_SECONDS)
            continue
        idle_backoff_seconds = _IDLE_BACKOFF_START_SECONDS
        log.info(
            "role=%s completed queued item session=%s artifact=%s; idle backoff reset",
            role,
            artifact.execution_session_id,
            artifact.artifact_id,
        )
    log.info("role=%s stopped worker loop", role)


def main() -> None:
    from app.telemetry import configure_span_exporter_from_environment

    s = get_settings()
    configure_logging(s.log_level)
    configure_span_exporter_from_environment()
    if s.role == "api":
        _run_api()
        return
    if s.role == "openvoice":
        _run_openvoice()
        return
    _run_execution_worker(s.role)


if __name__ == "__main__":
    main()
