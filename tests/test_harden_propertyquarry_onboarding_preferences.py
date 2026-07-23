from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "harden_propertyquarry_onboarding_preferences.py"


def _module():
    spec = importlib.util.spec_from_file_location("hardened_onboarding_compactor", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _preferences(*, deep: bool = True):
    raw = {
        "saved_shortlist_candidates": [{"legacy": True}],
        "search_agents": [{"legacy": True}],
        "old_intent": "quiet",
        "raw_preferences": {"old_intent": "older", "inner_value": 9},
    }
    if not deep:
        raw.pop("raw_preferences")
    return {
        "saved_shortlist_candidates": [{"id": "top"}],
        "search_agents": [{"id": "agent"}],
        "must_keep": {"nested": [1, 2]},
        "raw_preferences": raw,
    }


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.connection.executed.append((" ".join(str(query).split()), params))
        normalized = " ".join(str(query).split())
        if normalized.startswith("SELECT principal_id") and "WHERE jsonb_typeof" in normalized:
            self.rows = [(self.connection.principal, 999_999)]
        elif normalized.startswith("SELECT principal_id") and "FOR UPDATE" in normalized:
            self.rows = [self.connection.row()]
        elif normalized.startswith("UPDATE onboarding_states"):
            if self.connection.cas_fails:
                self.rows = []
                return
            replacement = json.loads(params[0])
            expected = json.loads(params[2])
            if self.connection.preferences != expected:
                self.rows = []
                return
            self.connection.preferences = replacement
            self.rows = [(replacement, len(json.dumps(replacement)))] if "pg_column_size" in normalized else [(replacement,)]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    def __init__(self):
        self.principal = "principal-secret-value"
        self.preferences = _preferences()
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.cas_fails = False
        self.autocommit = True

    def row(self):
        payload = {"principal_id": self.principal, "property_search_preferences_json": self.preferences, "other": "recovery"}
        return (self.principal, self.preferences, 999_999, payload)

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _scope(module, connection):
    return dict(expected_count=1, expected_principal_digests=[module.principal_digest(connection.principal)])


def test_scope_requires_exact_count_and_full_digest_set():
    module = _module()
    connection = Connection()
    candidate = module._candidate_from_row(connection.row())
    with pytest.raises(module.CompactorError, match="candidate_count_mismatch"):
        module.validate_scope([candidate], expected_count=3, expected_principal_digests=[candidate.principal_sha256])
    with pytest.raises(module.CompactorError, match="candidate_digest_set_mismatch"):
        module.validate_scope([candidate], expected_count=1, expected_principal_digests=["0" * 64])


def test_compaction_bounds_depth_and_preserves_all_top_level_semantics():
    module = _module()
    source = _preferences()
    compacted = module.compact_preferences(source)
    assert compacted["must_keep"] == source["must_keep"]
    assert compacted["saved_shortlist_candidates"] == source["saved_shortlist_candidates"]
    assert compacted["search_agents"] == source["search_agents"]
    assert compacted["raw_preferences"] == {"old_intent": "quiet", "inner_value": 9}
    assert module.raw_preferences_depth(compacted) == 1
    too_deep = {"value": True}
    for _ in range(module.RAW_PREFERENCES_MAX_DEPTH + 1):
        too_deep = {"raw_preferences": too_deep}
    with pytest.raises(module.CompactorError, match="depth_limit"):
        module.compact_preferences(too_deep)
    no_op = _preferences(deep=False)
    assert module.compact_preferences(no_op) == no_op
    candidate = module.Candidate("principal", module.principal_digest("principal"), no_op, 999)
    assert candidate.changed is False


def test_dry_run_has_no_locks_updates_backups_or_commit(tmp_path):
    module = _module()
    connection = Connection()
    receipt = module.dry_run(connection, minimum_bytes=1, **_scope(module, connection))
    assert receipt["mode"] == "dry_run"
    assert connection.commits == 0
    assert not any("FOR UPDATE" in query or query.startswith("UPDATE") for query, _ in connection.executed)
    assert list(tmp_path.iterdir()) == []


def test_apply_is_private_transactional_and_noop_rows_are_not_written(tmp_path):
    module = _module()
    connection = Connection()
    backup_dir = tmp_path / "private"
    backup_dir.mkdir(mode=0o700)
    receipt = module.apply(connection, backup_dir=backup_dir, minimum_bytes=1, **_scope(module, connection))
    assert receipt["mode"] == "apply"
    assert connection.commits == 1
    assert connection.autocommit is True
    assert any("FOR UPDATE" in query for query, _ in connection.executed)
    assert any("lock_timeout" in query for query, _ in connection.executed)
    backup = next(backup_dir.iterdir())
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    text = backup.read_text()
    assert connection.principal in text  # full private recovery row
    assert connection.principal not in json.dumps(receipt)
    assert "must_keep" not in json.dumps(receipt)
    with pytest.raises(module.CompactorError, match="candidate_changed_before_lock"):
        module.apply(connection, backup_dir=backup_dir, minimum_bytes=1, **_scope(module, connection))


def test_apply_rejects_backup_policy_and_compare_and_swap(tmp_path):
    module = _module()
    connection = Connection()
    with pytest.raises(module.CompactorError, match="backup_directory_missing"):
        module.apply(connection, backup_dir=tmp_path / "missing", minimum_bytes=1, **_scope(module, connection))
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    os.chmod(unsafe, 0o755)
    with pytest.raises(module.CompactorError, match="mode_must_be_0700"):
        module.apply(connection, backup_dir=unsafe, minimum_bytes=1, **_scope(module, connection))
    link = tmp_path / "link"
    link.symlink_to(unsafe, target_is_directory=True)
    with pytest.raises(module.CompactorError, match="backup_directory_missing"):
        module.apply(connection, backup_dir=link, minimum_bytes=1, **_scope(module, connection))
    safe = tmp_path / "safe"
    safe.mkdir(mode=0o700)
    connection.cas_fails = True
    with pytest.raises(module.CompactorError, match="update_compare_and_swap_failed"):
        module.apply(connection, backup_dir=safe, minimum_bytes=1, **_scope(module, connection))
    assert connection.rollbacks == 1


def test_restore_validates_backup_hash_and_refuses_later_edit(tmp_path):
    module = _module()
    connection = Connection()
    backup_dir = tmp_path / "safe"
    backup_dir.mkdir(mode=0o700)
    module.apply(connection, backup_dir=backup_dir, minimum_bytes=1, **_scope(module, connection))
    backup = next(backup_dir.iterdir())
    receipt = module.restore(connection, backup_path=backup)
    assert receipt["mode"] == "restore"
    assert connection.preferences == _preferences()
    second_backup_dir = tmp_path / "safe-second"
    second_backup_dir.mkdir(mode=0o700)
    module.apply(connection, backup_dir=second_backup_dir, minimum_bytes=1, **_scope(module, connection))
    second_backup = next(second_backup_dir.iterdir())
    connection.preferences["later_edit"] = True
    with pytest.raises(module.CompactorError, match="restore_compare_and_swap_mismatch"):
        module.restore(connection, backup_path=second_backup)
    damaged = tmp_path / "damaged.json"
    damaged.write_bytes(backup.read_bytes().replace(b"backup.v1", b"backup.x1"))
    os.chmod(damaged, 0o600)
    with pytest.raises(module.CompactorError, match="backup_"):
        module.restore(connection, backup_path=damaged)
