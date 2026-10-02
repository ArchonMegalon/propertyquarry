"""Paid-generation spend guard for the Phygital+ adapter.

Fail-closed by design: when armed and the persistent ledger cannot be read or
written, paid Kling tasks are refused (`phygital_state_unavailable`) rather
than started unchecked. Search ranking and cache reuse never touch this module.

Contract (env):
- PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT: max paid Kling tasks per rolling window.
  UNSET/blank  -> guard INACTIVE (no ledger I/O; unit tests and existing
                  callers keep today's behaviour).
  "0"          -> hard kill switch (`phygital_spend_disabled`).
  "N" (N > 0)  -> at most N paid tasks per window; production compose passes
                  `"${PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT:-10}"` so the guard
                  is armed by default with a 10/24h cap.
- PROPERTYQUARRY_PHYGITAL_SPEND_WINDOW_SECONDS: window length. Default 86400.
- PROPERTYQUARRY_PHYGITAL_STATE_DIR: ledger directory.
  Default /data/artifacts/propertyquarry-phygital-cache (persistent volume in
  the production stack).

The ledger is a JSON document {version, tasks: {artifact_key: record}} replaced
atomically (tmp file + os.replace) so a crash never truncates it. Records are
keyed by artifact_key: a second generate call for the same artifact while a
record is open is a duplicate and is refused with `phygital_task_in_flight`.

Counting is conservative: records count against the cap unless explicitly
refunded (`set_outcome(..., "refunded")`) or older than the window.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


DEFAULT_STATE_DIR = "/data/artifacts/propertyquarry-phygital-cache"
DEFAULT_WINDOW_SECONDS = 86400
INACTIVE_LIMIT = -1

REASON_DISABLED = "phygital_spend_disabled"
REASON_LIMIT = "phygital_spend_limit_reached"
REASON_IN_FLIGHT = "phygital_task_in_flight"
REASON_STATE = "phygital_state_unavailable"

_OUTCOMES = {"completed", "refunded"}


def _parse_limit(name: str) -> int:
    """Parse the limit env: unset -> inactive; "0" -> kill switch; bad -> kill switch."""
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return INACTIVE_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return 0
    if value < 0:
        return 0
    return value


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


class SpendLedger:
    """Persistent, atomic, conservative ledger of paid Phygital/Kling tasks.

    Active only when PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT is set explicitly.
    """

    def __init__(self, state_dir: str, *, limit: int, window_seconds: int) -> None:
        self._state_dir = state_dir
        self._limit = int(limit)
        self._window = max(1, int(window_seconds))

    @classmethod
    def from_env(cls) -> "SpendLedger":
        state_dir = (
            str(os.environ.get("PROPERTYQUARRY_PHYGITAL_STATE_DIR") or "").strip()
            or DEFAULT_STATE_DIR
        )
        return cls(
            state_dir,
            limit=_parse_limit("PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT"),
            window_seconds=_env_int(
                "PROPERTYQUARRY_PHYGITAL_SPEND_WINDOW_SECONDS", DEFAULT_WINDOW_SECONDS
            ),
        )

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> bool:
        return self._limit >= 0

    @property
    def window_seconds(self) -> int:
        return self._window

    # ------------------------------------------------------------------ io

    def _ledger_path(self) -> str:
        return os.path.join(self._state_dir, "spend-ledger.json")

    def _load(self) -> dict[str, Any]:
        """Read the ledger; corrupt or unreadable state raises OSError/ValueError."""
        path = self._ledger_path()
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or not isinstance(data.get("tasks") or {}, dict):
            raise ValueError("phygital spend ledger malformed")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        os.makedirs(self._state_dir, exist_ok=True)
        path = self._ledger_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    # ------------------------------------------------------------- helpers

    def _pruned(self, data: dict[str, Any], now: float) -> dict[str, Any]:
        tasks = data.get("tasks") or {}
        kept: dict[str, Any] = {}
        for key, record in tasks.items():
            if not isinstance(record, dict):
                continue
            try:
                reserved_at = float(record.get("reserved_at") or 0.0)
            except (TypeError, ValueError):
                continue
            if now - reserved_at >= self._window:
                continue
            kept[key] = record
        return {"version": data.get("version") or 1, "tasks": kept}

    def _window_count(self, tasks: dict[str, Any], now: float) -> int:
        return sum(
            1
            for record in tasks.values()
            if str(record.get("outcome") or "") != "refunded"
        )

    def _blocked(
        self, tasks: dict[str, Any], key: str, now: float
    ) -> tuple[bool, str, dict[str, Any]]:
        """Shared gate: kill switch, cap, and duplicate-in-flight detection."""
        if not self.active:
            return True, "ok", {}
        if self._limit == 0:
            return False, REASON_DISABLED, {}
        record = tasks.get(key)
        if isinstance(record, dict) and str(record.get("outcome") or "") != "refunded":
            return False, REASON_IN_FLIGHT, record
        if self._window_count(tasks, now) >= self._limit:
            return False, REASON_LIMIT, {}
        return True, "ok", {}

    # --------------------------------------------------------------- public

    def preflight(
        self, artifact_key: str, *, now: float | None = None
    ) -> tuple[bool, str, dict[str, Any]]:
        """Advisory check before any upload/login work. Persists nothing."""
        moment = time.time() if now is None else float(now)
        if not self.active:
            return True, "ok", {}
        if self._limit == 0:
            return False, REASON_DISABLED, {}
        try:
            raw = self._load()
        except FileNotFoundError:
            raw = {"version": 1, "tasks": {}}
        except (OSError, ValueError):
            return False, REASON_STATE, {}
        tasks = self._pruned(raw, moment)["tasks"]
        allowed, reason, record = self._blocked(tasks, artifact_key, moment)
        return allowed, reason, dict(record or {})

    def reserve(
        self, artifact_key: str, *, now: float | None = None
    ) -> tuple[bool, str, dict[str, Any]]:
        """Enforcing gate immediately before start_kling_task. Persists a hold."""
        moment = time.time() if now is None else float(now)
        if not self.active:
            return True, "ok", {}
        try:
            raw = self._load()
        except FileNotFoundError:
            raw = {"version": 1, "tasks": {}}
        except (OSError, ValueError):
            return False, REASON_STATE, {}
        tasks = self._pruned(raw, moment)["tasks"]
        allowed, reason, record = self._blocked(tasks, artifact_key, moment)
        if not allowed:
            return False, reason, dict(record or {})
        tasks[artifact_key] = {
            "artifact_key": artifact_key,
            "reserved_at": moment,
            "task_id": "",
            "phase": "reserved",
            "outcome": "",
        }
        try:
            self._save({"version": 1, "tasks": tasks})
        except OSError:
            return False, REASON_STATE, {}
        return True, "ok", dict(tasks[artifact_key])

    def attach_task(
        self, artifact_key: str, task_id: str, *, now: float | None = None
    ) -> None:
        """Best-effort: record the Kling task id on a reserved slot."""
        if not self.active:
            return
        moment = time.time() if now is None else float(now)
        tasks = self._pruned(self._load(), moment)["tasks"]
        record = tasks.get(artifact_key)
        if isinstance(record, dict):
            record["task_id"] = str(task_id or "").strip()
            record["phase"] = "in_flight"
            tasks[artifact_key] = record
            self._save({"version": 1, "tasks": tasks})

    def set_outcome(
        self, artifact_key: str, outcome: str, *, now: float | None = None
    ) -> None:
        """Mark a slot completed (counts) or refunded (frees the cap)."""
        if outcome not in _OUTCOMES:
            raise ValueError(f"unknown outcome: {outcome}")
        if not self.active:
            return
        moment = time.time() if now is None else float(now)
        tasks = self._pruned(self._load(), moment)["tasks"]
        record = tasks.get(artifact_key)
        if isinstance(record, dict):
            record["outcome"] = outcome
            tasks[artifact_key] = record
            self._save({"version": 1, "tasks": tasks})

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        """Observability helper: active window count and records."""
        moment = time.time() if now is None else float(now)
        try:
            tasks = self._pruned(self._load(), moment)["tasks"]
        except FileNotFoundError:
            tasks = {}
        except (OSError, ValueError):
            tasks = {}
        return {
            "limit": self._limit,
            "active": self.active,
            "window_seconds": self._window,
            "count": self._window_count(tasks, moment) if self.active else 0,
            "tasks": tasks,
        }
