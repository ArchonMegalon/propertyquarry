from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "compact_propertyquarry_onboarding_preferences.py"


def _module():
    spec = importlib.util.spec_from_file_location("compact_propertyquarry_onboarding_preferences", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _nested_preferences() -> dict[str, object]:
    oldest = {
        "keywords": "quiet balcony",
        "investment_strategy": "cash_flow",
        "include_shared_housing_sources": True,
        "saved_shortlist_candidates": [{"title": "old"}],
        "search_agents": [{"agent_id": "old-agent"}],
    }
    middle = {
        "keywords": "quiet balcony lift",
        "alert_frequency": "daily",
        "raw_preferences": oldest,
        "saved_shortlist_candidates": [{"title": "middle"}],
        "search_agents": [{"agent_id": "middle-agent"}],
    }
    return {
        "country_code": "AT",
        "keywords": "quiet balcony lift",
        "property_commercial": {"active_plan_key": "plus"},
        "saved_shortlist_candidates": [
            {"title": "current", "property_url": "https://sensitive.example/listing"}
        ],
        "search_agents": [{"agent_id": "current-agent", "name": "Current"}],
        "raw_preferences": middle,
    }


def test_compaction_preserves_current_shortlist_agents_and_full_raw_chain_fields() -> None:
    module = _module()
    original = _nested_preferences()

    plan = module.build_compaction_plan(original)

    assert plan.changed is True
    assert plan.raw_nesting_depth == 2
    assert plan.after_json_bytes < plan.before_json_bytes
    assert plan.compacted["saved_shortlist_candidates"] == original["saved_shortlist_candidates"]
    assert plan.compacted["search_agents"] == original["search_agents"]
    assert plan.compacted["property_commercial"] == original["property_commercial"]
    assert plan.compacted["raw_preferences"] == {
        "keywords": "quiet balcony lift",
        "alert_frequency": "daily",
        "investment_strategy": "cash_flow",
        "include_shared_housing_sources": True,
    }
    assert module._raw_nesting_depth(plan.compacted) == 1


def test_compaction_rejects_a_chain_beyond_its_verified_depth_bound() -> None:
    module = _module()
    nested: dict[str, object] = {"deep_value": "must-not-be-silently-lost"}
    for _ in range(module.RAW_PREFERENCES_MAX_DEPTH + 2):
        nested = {"raw_preferences": nested}

    with pytest.raises(ValueError, match="raw_preferences_depth_limit_exceeded"):
        module.build_compaction_plan(nested)


def test_public_report_does_not_include_principal_or_preference_values() -> None:
    module = _module()
    principal_id = "cf-email:private.person@example.test"
    preferences = _nested_preferences()
    plan = module.build_compaction_plan(preferences)

    report = module._public_report(
        plan=plan,
        principal_digest=module._principal_digest(principal_id),
        stored_before_bytes=123,
        stored_after_bytes=None,
        mode="dry_run",
        search_runs={
            "row_count": 10,
            "payload_stored_bytes": 1000,
            "compact_stored_bytes": 100,
            "full_payload_row_count": 9,
        },
    )
    rendered = json.dumps(report, sort_keys=True)

    assert report["status"] == "dry_run"
    assert report["search_run_retention_action"] == "diagnostic_only"
    assert principal_id not in rendered
    assert "private.person" not in rendered
    assert "sensitive.example" not in rendered
    assert "quiet balcony" not in rendered


def test_backup_is_exclusive_private_and_contains_restore_row(tmp_path: Path) -> None:
    module = _module()
    path = tmp_path / "onboarding-backup.json"
    payload = {
        "schema": module.BACKUP_SCHEMA,
        "row": {
            "principal_id": "private-principal",
            "property_search_preferences_json": _nested_preferences(),
        },
    }

    digest, size = module.write_backup_file(path, payload)

    assert len(digest) == 64
    assert size == path.stat().st_size
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["row"]["principal_id"] == "private-principal"
    with pytest.raises(FileExistsError, match="backup_path_exists"):
        module.write_backup_file(path, payload)


def test_parse_args_is_dry_run_by_default_and_apply_requires_backup() -> None:
    module = _module()

    args = module.parse_args([])
    assert args.apply is False
    assert args.backup_path is None
    with pytest.raises(SystemExit):
        module.parse_args(["--apply"])


class _FakeCursor:
    def __init__(self, row: tuple[object, ...]) -> None:
        self.row = row
        self.results: list[tuple[object, ...]] = []
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query, params) -> None:
        self.queries.append(str(query))
        if "FROM onboarding_states" in str(query):
            self.results.append(self.row)
        elif "FROM property_search_runs" in str(query):
            self.results.append((12, 1000, 200, 10))
        else:
            raise AssertionError(f"unexpected mutation query in dry run: {query}")

    def fetchone(self):
        return self.results.pop(0)


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        self.committed = True


def test_dry_run_performs_no_lock_update_backup_or_commit(tmp_path: Path) -> None:
    module = _module()
    preferences = _nested_preferences()
    values: list[object] = [
        "onboarding-1",
        "private-principal",
        "Workspace",
        "personal",
        "AT",
        "en",
        "Europe/Vienna",
        [],
        preferences,
        {},
        {},
        {},
        "started",
        "2026-07-17T00:00:00Z",
        "2026-07-17T00:00:00Z",
        4096,
    ]
    cursor = _FakeCursor(tuple(values))
    connection = _FakeConnection(cursor)
    backup_path = tmp_path / "must-not-exist.json"

    report = module.run_compaction(
        connection,
        principal_id="private-principal",
        apply=False,
        backup_path=backup_path,
    )

    assert report["status"] == "dry_run"
    assert connection.committed is False
    assert backup_path.exists() is False
    assert all("FOR UPDATE" not in query for query in cursor.queries)
    assert all(not query.lstrip().upper().startswith("UPDATE") for query in cursor.queries)


def test_apply_writes_private_backup_before_update_and_verifies_result(tmp_path: Path) -> None:
    module = _module()
    preferences = _nested_preferences()
    values: list[object] = [
        "onboarding-1",
        "private-principal",
        "Workspace",
        "personal",
        "AT",
        "en",
        "Europe/Vienna",
        [],
        preferences,
        {},
        {},
        {},
        "started",
        "2026-07-17T00:00:00Z",
        "2026-07-17T00:00:00Z",
        4096,
    ]
    backup_path = tmp_path / "onboarding-backup.json"

    class ApplyCursor(_FakeCursor):
        def execute(self, query, params) -> None:
            self.queries.append(str(query))
            if "FROM onboarding_states" in str(query):
                self.results.append(self.row)
            elif "FROM property_search_runs" in str(query):
                self.results.append((12, 1000, 200, 10))
            elif str(query).lstrip().upper().startswith("UPDATE"):
                assert backup_path.exists()
                assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
                updated = json.loads(params[0])
                self.results.append((updated, len(params[0].encode("utf-8"))))
            else:
                raise AssertionError(f"unexpected query: {query}")

    cursor = ApplyCursor(tuple(values))
    connection = _FakeConnection(cursor)

    report = module.run_compaction(
        connection,
        principal_id="private-principal",
        apply=True,
        backup_path=backup_path,
    )

    assert report["status"] == "applied"
    assert report["backup_permissions"] == "0600"
    assert report["canonical_sha256_after"] != report["canonical_sha256_before"]
    assert connection.committed is True
    assert any("FOR UPDATE" in query for query in cursor.queries)
    assert any(query.lstrip().upper().startswith("UPDATE") for query in cursor.queries)
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    assert backup["schema"] == module.BACKUP_SCHEMA
    assert backup["row"]["property_search_preferences_json"] == preferences
