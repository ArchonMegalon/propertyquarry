"""Spend-guard unit tests: contract, rolling cap, kill switch, duplicates, fail-closed."""

from __future__ import annotations

import json
import time
import os

import pytest

from app.services.phygital import spend as spend_module


def _ledger(tmp_path, limit=3, window=86400):
    return spend_module.SpendLedger(str(tmp_path / 'state'), limit=limit, window_seconds=window)


def _read(tmp_path):
    return json.loads((tmp_path / 'state' / 'spend-ledger.json').read_text(encoding='utf-8'))


class TestSpendLedgerContract:
    def test_unset_limit_is_inactive(self, monkeypatch):
        names = ['PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT', 'PROPERTYQUARRY_PHYGITAL_SPEND_WINDOW_SECONDS', 'PROPERTYQUARRY_PHYGITAL_STATE_DIR']
        for name in names:
            monkeypatch.delenv(name, raising=False)
        led = spend_module.SpendLedger.from_env()
        assert led.active is False
        assert led.limit == -1

    def test_zero_limit_is_kill_switch(self, monkeypatch):
        monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT', '0')
        led = spend_module.SpendLedger.from_env()
        assert led.active is True
        assert led.limit == 0

    def test_positive_limit_arms_guard(self, monkeypatch):
        monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT', '7')
        led = spend_module.SpendLedger.from_env()
        assert led.active is True
        assert led.limit == 7

    def test_garbage_limit_is_kill_switch(self, monkeypatch):
        monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT', 'not-a-number')
        led = spend_module.SpendLedger.from_env()
        assert led.active is True
        assert led.limit == 0

    def test_negative_limit_is_kill_switch(self, monkeypatch):
        monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT', '-3')
        led = spend_module.SpendLedger.from_env()
        assert led.active is True
        assert led.limit == 0

    def test_window_default_and_override(self, monkeypatch):
        monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_SPEND_LIMIT', '5')
        monkeypatch.delenv('PROPERTYQUARRY_PHYGITAL_SPEND_WINDOW_SECONDS', raising=False)
        led = spend_module.SpendLedger.from_env()
        assert led.window_seconds == 86400
        monkeypatch.setenv('PROPERTYQUARRY_PHYGITAL_SPEND_WINDOW_SECONDS', '3600')
        led = spend_module.SpendLedger.from_env()
        assert led.window_seconds == 3600


class TestSpendLedgerGates:
    def test_preflight_missing_file_allows(self, tmp_path):
        led = _ledger(tmp_path)
        allowed, reason, _ = led.preflight('a1')
        assert allowed is True
        assert reason == 'ok'

    def test_preflight_corrupt_json_fails_closed(self, tmp_path):
        state = tmp_path / 'state'
        state.mkdir(parents=True, exist_ok=True)
        (state / 'spend-ledger.json').write_text('{broken', encoding='utf-8')
        led = _ledger(tmp_path)
        allowed, reason, _ = led.preflight('a1')
        assert allowed is False
        assert reason == spend_module.REASON_STATE

    def test_reserve_persists_hold(self, tmp_path):
        led = _ledger(tmp_path, limit=3)
        ok, reason, _ = led.reserve('a1')
        assert ok is True
        assert reason == 'ok'
        data = _read(tmp_path)
        assert data['tasks']['a1']['phase'] == 'reserved'

    def test_reserve_duplicate_refused(self, tmp_path):
        led = _ledger(tmp_path, limit=3)
        assert led.reserve('a1')[0] is True
        ok, reason, record = led.reserve('a1')
        assert ok is False
        assert reason == spend_module.REASON_IN_FLIGHT

    def test_refund_frees_cap(self, tmp_path):
        led = _ledger(tmp_path, limit=1)
        led.reserve('a1')
        led.set_outcome('a1', 'refunded')
        ok, reason, _ = led.reserve('a2')
        assert ok is True

    def test_window_expiry_frees_cap(self, tmp_path):
        led = _ledger(tmp_path, limit=1, window=60)
        led.reserve('a1')
        ok, reason, _ = led.reserve('a2', now=time.time() + 61)
        assert ok is True

    def test_kill_switch_blocks_preflight_and_reserve(self, tmp_path):
        led = _ledger(tmp_path, limit=0)
        ok, reason, _ = led.preflight('a1')
        assert ok is False
        assert reason == spend_module.REASON_DISABLED
        ok, reason, _ = led.reserve('a1')
        assert ok is False
        assert reason == spend_module.REASON_DISABLED

    def test_cap_enforced_across_keys(self, tmp_path):
        led = _ledger(tmp_path, limit=2)
        assert led.reserve('a1')[0] is True
        assert led.reserve('a2')[0] is True
        ok, reason, _ = led.reserve('a3')
        assert ok is False
        assert reason == spend_module.REASON_LIMIT

    def test_completed_counts_until_window_expiry(self, tmp_path):
        led = _ledger(tmp_path, limit=1, window=60)
        led.reserve('a1')
        led.set_outcome('a1', 'completed')
        ok, reason, _ = led.reserve('a2')
        assert ok is False
        assert reason == spend_module.REASON_LIMIT
        ok, reason, _ = led.reserve('a2', now=time.time() + 61)
        assert ok is True

    def test_snapshot_shape(self, tmp_path):
        led = _ledger(tmp_path, limit=5)
        led.reserve('a1')
        snap = led.snapshot()
        assert snap['active'] is True
        assert snap['count'] == 1

    def test_attach_task_persists_id(self, tmp_path):
        led = _ledger(tmp_path)
        led.reserve('a1')
        led.attach_task('a1', 'task-xyz')
        data = _read(tmp_path)
        assert data['tasks']['a1']['task_id'] == 'task-xyz'
        assert data['tasks']['a1']['phase'] == 'in_flight'

    def test_set_outcome_invalid_raises(self, tmp_path):
        led = _ledger(tmp_path)
        led.reserve('a1')
        with pytest.raises(ValueError):
            led.set_outcome('a1', 'nonsense')

    def test_atomic_save_leaves_no_tmp(self, tmp_path):
        led = _ledger(tmp_path)
        led.reserve('a1')
        assert not (tmp_path / 'state' / 'spend-ledger.json.tmp').exists()
class TestCrossProcessLocking:
    def test_lock_file_created_and_released(self, tmp_path):
        led = _ledger(tmp_path, limit=2)
        allowed, reason, _ = led.reserve('k1')
        assert allowed is True and reason == 'ok'
        lockfile = tmp_path / 'state' / 'ledger.lock'
        assert not lockfile.exists()

    def test_lock_timeout_fails_closed(self, tmp_path):
        led = _ledger(tmp_path, limit=2)
        allowed, _, _ = led.reserve('k1')
        assert allowed is True
        led._acquire_file_lock()
        try:
            allowed, reason, _ = led.reserve('k2')
            assert allowed is False
            assert reason == spend_module.REASON_STATE
        finally:
            os.unlink(led._lock_path())

    def test_stale_lock_takeover(self, tmp_path):
        led = _ledger(tmp_path, limit=2)
        lockfile = tmp_path / 'state' / 'ledger.lock'
        lockfile.parent.mkdir(parents=True, exist_ok=True)
        lockfile.write_text('{"pid": 999999}')
        old = time.time() - (led._stale_seconds + 5)
        os.utime(lockfile, (old, old))
        allowed, reason, _ = led.reserve('k3')
        assert allowed is True and reason == 'ok'
        assert not lockfile.exists()

class TestSnapshotHealthAndPrune:
    def test_state_ok_true_when_absent(self, tmp_path):
        led = _ledger(tmp_path, limit=1)
        assert led.snapshot()['state_ok'] is True

    def test_state_ok_true_when_healthy(self, tmp_path):
        led = _ledger(tmp_path, limit=1)
        led.reserve('k1')
        led.set_outcome('k1', 'refunded')
        assert led.snapshot()['state_ok'] is True

    def test_state_ok_false_when_corrupt(self, tmp_path):
        led = _ledger(tmp_path, limit=1)
        p = tmp_path / 'state' / 'spend-ledger.json'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{not json')
        assert led.snapshot()['state_ok'] is False

    def test_malformed_reserved_at_counts_then_expires(self, tmp_path):
        led = _ledger(tmp_path, limit=1, window=5)
        p = tmp_path / 'state' / 'spend-ledger.json'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"version": 1, "tasks": {"bad": {"artifact_key": "bad", "task_id": "", "phase": "in_flight", "outcome": "", "reserved_at": "not-a-number"}}}')
        allowed, reason, _ = led.reserve('new')
        assert allowed is False
        assert reason == spend_module.REASON_LIMIT
        after = led.snapshot(now=time.time() + 10)
        assert after['count'] == 0
