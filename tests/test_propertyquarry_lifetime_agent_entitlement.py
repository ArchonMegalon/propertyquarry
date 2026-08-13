from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from app.services.property_billing import merge_property_commercial, normalize_property_commercial
from app.services.onboarding import strip_client_property_commercial_authority
from tests.product_test_helpers import build_property_client, start_workspace


ROOT = Path(__file__).resolve().parents[1]


def _module():
    path = ROOT / "scripts" / "propertyquarry_lifetime_agent_entitlement.py"
    spec = importlib.util.spec_from_file_location("propertyquarry_lifetime_agent_entitlement", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_non_lifetime_commercial_shape_does_not_gain_empty_entitlement_fields() -> None:
    normalized = normalize_property_commercial({})

    assert normalized["active_plan_key"] == "free"
    assert "entitlement_kind" not in normalized


def test_lifetime_agent_marker_survives_normalization_and_cannot_expire() -> None:
    normalized = normalize_property_commercial(
        {
            "active_plan_key": "free",
            "status": "expired",
            "active_until": "2020-01-01T00:00:00+00:00",
            "entitlement_kind": "lifetime",
            "entitlement_plan_key": "agent",
            "entitlement_source": "operator_grant",
            "entitlement_grant_id": "pq-lifetime-agent-example",
            "entitlement_granted_at": "2026-07-27T10:00:00+00:00",
            "entitlement_reason_digest": "a" * 64,
        },
        now=datetime(2998, 12, 31, tzinfo=timezone.utc),
    )

    assert normalized["active_plan_key"] == "agent"
    assert normalized["status"] == "active"
    assert normalized["active_until"] == "2999-01-01T00:00:00+00:00"
    assert normalized["entitlement_kind"] == "lifetime"
    assert normalized["entitlement_grant_id"] == "pq-lifetime-agent-example"

    after_projection_sentinel = normalize_property_commercial(
        normalized,
        now=datetime(3500, 1, 1, tzinfo=timezone.utc),
    )
    assert after_projection_sentinel["active_plan_key"] == "agent"
    assert after_projection_sentinel["status"] == "active"
    assert after_projection_sentinel["entitlement_kind"] == "lifetime"


def test_lifetime_agent_marker_prevents_payment_event_downgrade() -> None:
    merged = merge_property_commercial(
        {
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
                "entitlement_kind": "lifetime",
                "entitlement_plan_key": "agent",
                "entitlement_grant_id": "pq-lifetime-agent-example",
            }
        },
        updates={"active_plan_key": "free", "status": "refunded", "active_until": ""},
    )

    commercial = merged["property_commercial"]
    assert commercial["active_plan_key"] == "agent"
    assert commercial["status"] == "active"
    assert commercial["entitlement_kind"] == "lifetime"


def test_grant_builder_is_idempotent_and_preserves_billing_history() -> None:
    module = _module()
    before = {
        "active_plan_key": "plus",
        "active_until": "2026-08-01T00:00:00+00:00",
        "last_order_id": "existing-order",
        "billing_events_json": [{"event_id": "existing-event"}],
    }
    first = module.build_lifetime_agent_commercial(
        before,
        target_email_digest="b" * 64,
        granted_at="2026-07-27T10:00:00+00:00",
        reason="authorized",
    )
    second = module.build_lifetime_agent_commercial(
        first,
        target_email_digest="b" * 64,
        granted_at="2026-07-28T10:00:00+00:00",
        reason="authorized",
    )

    assert first == second
    assert first["last_order_id"] == "existing-order"
    assert first["billing_events_json"][0]["event_id"] == "existing-event"
    assert first["entitlement_kind"] == "lifetime"
    assert first["entitlement_granted_at"] == "2026-07-27T10:00:00+00:00"


def test_grant_builder_keeps_richer_top_level_billing_history_when_raw_is_empty() -> None:
    module = _module()
    preferences = {
        "property_commercial": {
            "active_plan_key": "plus",
            "active_until": "2026-08-01T00:00:00+00:00",
            "last_order_id": "top-level-order",
        },
        "raw_preferences": {"property_commercial": {}},
    }

    updated = module._updated_preferences(
        preferences,
        email_digest="c" * 64,
        granted_at="2026-07-27T10:00:00+00:00",
        reason="authorized",
    )

    assert updated["property_commercial"]["last_order_id"] == "top-level-order"
    assert (
        updated["raw_preferences"]["property_commercial"]["last_order_id"]
        == "top-level-order"
    )


class _QueryCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, _params: object = None) -> None:
        self.queries.append(query)

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self.rows)


class _QueryConnection:
    def __init__(self, cursor: _QueryCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _QueryCursor:
        return self._cursor


def test_workspace_access_email_alone_is_not_authoritative_identity_evidence() -> None:
    module = _module()
    email_cursor = _QueryCursor([])
    with pytest.raises(module.EntitlementGrantError, match="target_account_not_found"):
        module._email_for_md5(
            _QueryConnection(email_cursor),
            hashlib.md5(b"recipient@example.test").hexdigest(),
        )
    assert "workspace_access_sessions" not in email_cursor.queries[0]
    assert "FROM identity_accounts" not in email_cursor.queries[0]
    assert "propertyquarry_google_identity_accounts" not in email_cursor.queries[1]

    resolution_cursor = _QueryCursor([])
    with pytest.raises(module.EntitlementGrantError, match="target_account_not_found"):
        module._resolved_accounts(
            _QueryConnection(resolution_cursor),
            "recipient@example.test",
            lock=False,
        )
    assert "workspace_access_sessions" not in resolution_cursor.queries[0]
    assert "FROM identity_accounts" not in resolution_cursor.queries[0]
    assert "propertyquarry_google_identity_accounts" not in resolution_cursor.queries[1]


def _rollback_fixture(module, *, before: dict[str, object], after: dict[str, object]):
    return module._rollback_payload(
        generated_at="2026-07-27T10:00:00+00:00",
        target_email_digest="d" * 64,
        rows=[
            module._rollback_row(
                principal_id="principal-one",
                before=before,
                after=after,
            )
        ],
    )


def test_rollback_snapshot_is_atomic_no_clobber_and_scoped_to_commercial_subtrees(
    tmp_path: Path,
) -> None:
    module = _module()
    before = {
        "location_query": "Vienna",
        "property_commercial": {"active_plan_key": "free", "status": "free"},
        "raw_preferences": {
            "location_query": "Vienna",
            "property_commercial": {"active_plan_key": "free", "status": "free"},
        },
    }
    after = module._updated_preferences(
        before,
        email_digest="d" * 64,
        granted_at="2026-07-27T10:00:00+00:00",
        reason="authorized",
    )
    payload = _rollback_fixture(module, before=before, after=after)
    path = tmp_path / "rollback.private.json"

    first = module._prepare_rollback_snapshot(
        path,
        payload=payload,
        current_by_principal={"principal-one": before},
    )
    original_bytes = path.read_bytes()
    current_with_unrelated_change = {
        **after,
        "location_query": "Graz",
        "raw_preferences": {
            **dict(after["raw_preferences"]),
            "location_query": "Graz",
        },
    }
    second = module._prepare_rollback_snapshot(
        path,
        payload=_rollback_fixture(
            module,
            before=current_with_unrelated_change,
            after=current_with_unrelated_change,
        ),
        current_by_principal={
            "principal-one": current_with_unrelated_change
        },
    )

    assert first["created"] is True
    assert second["preserved_existing"] is True
    assert path.read_bytes() == original_bytes
    stored = json.loads(original_bytes)
    row = stored["rows"][0]
    assert "before_preferences" not in row
    assert row["before_top_controlled"] == {
        "active_plan_key": "free",
        "status": "free",
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_rollback_restore_changes_only_grant_controlled_commercial_fields() -> None:
    module = _module()
    before = {
        "location_query": "Vienna",
        "property_commercial": {
            "active_plan_key": "plus",
            "status": "active",
            "active_until": "2026-08-01T00:00:00+00:00",
            "last_order_id": "before-order",
        },
        "raw_preferences": {
            "location_query": "Vienna",
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "active",
                "active_until": "2026-08-01T00:00:00+00:00",
                "last_order_id": "before-order",
            },
        },
    }
    after = module._updated_preferences(
        before,
        email_digest="d" * 64,
        granted_at="2026-07-27T10:00:00+00:00",
        reason="authorized",
    )
    current = {
        **after,
        "location_query": "Graz",
        "property_commercial": {
            **dict(after["property_commercial"]),
            "last_order_id": "post-grant-order",
        },
        "raw_preferences": {
            **dict(after["raw_preferences"]),
            "location_query": "Graz",
            "property_commercial": {
                **dict(after["raw_preferences"]["property_commercial"]),
                "last_order_id": "post-grant-order",
            },
        },
    }
    row = module._rollback_row(
        principal_id="principal-one",
        before=before,
        after=after,
    )

    restored = module._restored_preferences(
        current,
        rollback_row=row,
        target_email_digest="d" * 64,
    )

    assert restored["location_query"] == "Graz"
    assert restored["raw_preferences"]["location_query"] == "Graz"
    assert restored["property_commercial"]["active_plan_key"] == "plus"
    assert (
        restored["property_commercial"]["last_order_id"]
        == "post-grant-order"
    )
    assert (
        restored["raw_preferences"]["property_commercial"]["last_order_id"]
        == "post-grant-order"
    )
    assert "entitlement_kind" not in restored["property_commercial"]


def test_rollback_restore_recreates_absent_commercial_subtrees() -> None:
    module = _module()
    before = {
        "location_query": "Vienna",
        "raw_preferences": {"location_query": "Vienna"},
    }
    after = module._updated_preferences(
        before,
        email_digest="d" * 64,
        granted_at="2026-07-27T10:00:00+00:00",
        reason="authorized",
    )
    row = module._rollback_row(
        principal_id="principal-one",
        before=before,
        after=after,
    )

    restored = module._restored_preferences(
        after,
        rollback_row=row,
        target_email_digest="d" * 64,
    )

    assert "property_commercial" not in restored
    assert "property_commercial" not in restored["raw_preferences"]
    assert restored["raw_preferences"]["location_query"] == "Vienna"


def test_rollback_restore_preserves_post_grant_billing_fields_when_subtree_was_absent() -> None:
    module = _module()
    before = {"location_query": "Vienna"}
    after = module._updated_preferences(
        before,
        email_digest="d" * 64,
        granted_at="2026-07-27T10:00:00+00:00",
        reason="authorized",
    )
    current = {
        **after,
        "property_commercial": {
            **dict(after["property_commercial"]),
            "last_order_id": "post-grant-order",
        },
        "raw_preferences": {
            **dict(after["raw_preferences"]),
            "property_commercial": {
                **dict(after["raw_preferences"]["property_commercial"]),
                "billing_events_json": [{"event_id": "post-grant-event"}],
            },
        },
    }
    row = module._rollback_row(
        principal_id="principal-one",
        before=before,
        after=after,
    )

    restored = module._restored_preferences(
        current,
        rollback_row=row,
        target_email_digest="d" * 64,
    )

    assert restored["property_commercial"] == {
        "last_order_id": "post-grant-order"
    }
    assert restored["raw_preferences"]["property_commercial"] == {
        "billing_events_json": [{"event_id": "post-grant-event"}]
    }


class _RestoreCursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.rowcount = 0
        self._rows: list[tuple[object, ...]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query: str, params: object = None) -> None:
        if "SELECT principal_id, property_search_preferences_json" in query:
            self._rows = [
                (principal_id, preferences)
                for principal_id, preferences in sorted(
                    self.connection.rows.items()
                )
            ]
            return
        if "UPDATE onboarding_states" in query:
            assert isinstance(params, tuple)
            restored_json, principal_id, current_json = params
            assert self.connection.rows[principal_id] == json.loads(
                current_json
            )
            self.connection.rows[principal_id] = json.loads(restored_json)
            self.connection.update_count += 1
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected query: {query}")

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows)


class _RestoreConnection:
    def __init__(self, rows: dict[str, dict[str, object]]) -> None:
        self.rows = {
            principal_id: json.loads(json.dumps(preferences))
            for principal_id, preferences in rows.items()
        }
        self.update_count = 0
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self) -> _RestoreCursor:
        return _RestoreCursor(self)

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _write_private_payload(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


def test_restore_plan_and_apply_preserve_unrelated_post_grant_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    before = {
        "location_query": "Vienna",
        "property_commercial": {
            "active_plan_key": "plus",
            "status": "active",
            "active_until": "2026-08-01T00:00:00+00:00",
            "last_order_id": "before-order",
        },
        "raw_preferences": {
            "location_query": "Vienna",
            "property_commercial": {
                "active_plan_key": "plus",
                "status": "active",
                "active_until": "2026-08-01T00:00:00+00:00",
                "last_order_id": "before-order",
            },
        },
    }
    after = module._updated_preferences(
        before,
        email_digest="d" * 64,
        granted_at="2026-07-27T10:00:00+00:00",
        reason="authorized",
    )
    current = {
        **after,
        "location_query": "Graz",
        "property_commercial": {
            **dict(after["property_commercial"]),
            "last_order_id": "post-grant-order",
        },
        "raw_preferences": {
            **dict(after["raw_preferences"]),
            "location_query": "Graz",
            "property_commercial": {
                **dict(after["raw_preferences"]["property_commercial"]),
                "last_order_id": "post-grant-order",
            },
        },
    }
    snapshot = _rollback_fixture(module, before=before, after=after)
    path = tmp_path / "rollback.private.json"
    _write_private_payload(path, snapshot)

    plan_connection = _RestoreConnection({"principal-one": current})
    monkeypatch.setattr(
        module,
        "_connect",
        lambda _url: plan_connection,
    )
    plan = module.restore(
        database_url="postgresql://not-used",
        snapshot_path=path,
        apply=False,
        receipt_path=None,
    )
    assert plan["mode"] == "plan"
    assert plan_connection.update_count == 0
    assert plan_connection.rolled_back is True

    apply_connection = _RestoreConnection({"principal-one": current})
    monkeypatch.setattr(
        module,
        "_connect",
        lambda _url: apply_connection,
    )
    receipt = module.restore(
        database_url="postgresql://not-used",
        snapshot_path=path,
        apply=True,
        receipt_path=None,
    )
    restored = apply_connection.rows["principal-one"]
    assert receipt["status"] == "restored"
    assert apply_connection.committed is True
    assert apply_connection.update_count == 1
    assert restored["location_query"] == "Graz"
    assert restored["raw_preferences"]["location_query"] == "Graz"
    assert (
        restored["property_commercial"]["last_order_id"]
        == "post-grant-order"
    )
    assert restored["property_commercial"]["active_plan_key"] == "plus"


def test_restore_rejects_altered_entitlement_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    before = {"property_commercial": {"active_plan_key": "free"}}
    after = module._updated_preferences(
        before,
        email_digest="d" * 64,
        granted_at="2026-07-27T10:00:00+00:00",
        reason="authorized",
    )
    altered = json.loads(json.dumps(after))
    altered["property_commercial"]["active_until"] = (
        "2099-01-01T00:00:00+00:00"
    )
    path = tmp_path / "rollback.private.json"
    _write_private_payload(
        path,
        _rollback_fixture(module, before=before, after=after),
    )
    connection = _RestoreConnection({"principal-one": altered})
    monkeypatch.setattr(module, "_connect", lambda _url: connection)

    with pytest.raises(
        module.EntitlementGrantError,
        match="rollback_current_entitlement_conflict",
    ):
        module.restore(
            database_url="postgresql://not-used",
            snapshot_path=path,
            apply=True,
            receipt_path=None,
        )
    assert connection.update_count == 0
    assert connection.committed is False


def test_legacy_snapshot_is_accepted_only_for_matching_lifetime_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    before = {"property_commercial": {"active_plan_key": "free"}}
    after = module._updated_preferences(
        before,
        email_digest="d" * 64,
        granted_at="2026-07-27T10:00:00+00:00",
        reason="authorized",
    )
    legacy_payload = {
        "schema": f"{module.SCHEMA}.rollback",
        "created_at": "2026-07-27T10:00:00+00:00",
        "target_email_sha256": "d" * 64,
        "rows": [
            {
                "principal_id": "principal-one",
                "before_preferences": before,
                "expected_after_sha256": "ignored-for-scoped-restore",
            }
        ],
    }
    path = tmp_path / "legacy.private.json"
    _write_private_payload(path, legacy_payload)
    connection = _RestoreConnection({"principal-one": after})
    monkeypatch.setattr(module, "_connect", lambda _url: connection)

    receipt = module.restore(
        database_url="postgresql://not-used",
        snapshot_path=path,
        apply=False,
        receipt_path=None,
    )
    assert receipt["status"] == "planned"
    assert receipt["commercial_subtree_only"] is True


def test_conflicting_rollback_snapshot_fails_before_database_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    before = {"property_commercial": {"active_plan_key": "free"}}
    path = tmp_path / "rollback.private.json"
    path.write_text(
        json.dumps(
            {
                "schema": module.ROLLBACK_SCHEMA,
                "target_email_sha256": "e" * 64,
                "rows": [],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    class _FakeConnection:
        cursor_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            self.cursor_calls += 1
            raise AssertionError("database mutation must not be reached")

        def rollback(self) -> None:
            pass

    connection = _FakeConnection()
    monkeypatch.setattr(module, "_connect", lambda _url: connection)
    monkeypatch.setattr(
        module,
        "_email_for_md5",
        lambda _conn, _digest: "owner@example.test",
    )
    monkeypatch.setattr(
        module,
        "_resolved_accounts",
        lambda _conn, _email, lock: [("principal-one", before)],
    )

    with pytest.raises(module.EntitlementGrantError, match="rollback_snapshot_conflict"):
        module.grant(
            database_url="postgresql://not-used",
            email_md5="f" * 32,
            apply=True,
            reason="authorized",
            receipt_path=None,
            rollback_path=path,
        )
    assert connection.cursor_calls == 0


def test_apply_requires_rollback_snapshot_before_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(
        module,
        "_connect",
        lambda _url: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    with pytest.raises(module.EntitlementGrantError, match="rollback_snapshot_required"):
        module.grant(
            database_url="postgresql://not-used",
            email_md5="a" * 32,
            apply=True,
            reason="authorized",
            receipt_path=None,
            rollback_path=None,
        )


def test_receipt_cannot_overwrite_rollback_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = _module()
    shared_path = tmp_path / "shared.json"
    monkeypatch.setattr(
        module,
        "_connect",
        lambda _url: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    with pytest.raises(
        module.EntitlementGrantError,
        match="receipt_and_rollback_paths_must_differ",
    ):
        module.grant(
            database_url="postgresql://not-used",
            email_md5="a" * 32,
            apply=True,
            reason="authorized",
            receipt_path=shared_path,
            rollback_path=shared_path,
        )


def test_client_commercial_fields_are_stripped_even_when_nested() -> None:
    sanitized = strip_client_property_commercial_authority(
        {
            "location_query": "Vienna",
            "property_commercial": {"active_plan_key": "agent_lifetime"},
            "raw_preferences": {
                "property_commercial": {"entitlement_kind": "lifetime"},
                "min_rooms": 3,
            },
        }
    )

    assert "property_commercial" not in sanitized
    assert sanitized["location_query"] == "Vienna"
    assert sanitized["min_rooms"] == 3


def test_authenticated_preferences_write_cannot_self_upgrade_or_replace_lifetime() -> None:
    principal_id = "entitlement-boundary-user"
    client = build_property_client(principal_id=principal_id)
    start_workspace(client, mode="personal", workspace_name="Boundary Office")

    attempted_upgrade = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "location_query": "Vienna",
            "property_commercial": {
                "active_plan_key": "agent_lifetime",
                "status": "lifetime",
                "entitlement_kind": "lifetime",
            },
            "raw_preferences": {
                "property_commercial": {
                    "active_plan_key": "agent_tier_lifetime",
                }
            },
        },
    )
    assert attempted_upgrade.status_code == 200
    commercial = attempted_upgrade.json()["property_search_preferences"][
        "property_commercial"
    ]
    assert commercial["active_plan_key"] == "free"
    assert commercial.get("entitlement_kind") != "lifetime"

    onboarding = client.app.state.container.onboarding
    onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json={
            "location_query": "Vienna",
            "property_commercial": {
                "active_plan_key": "agent",
                "status": "active",
                "active_until": "2999-01-01T00:00:00+00:00",
                "entitlement_kind": "lifetime",
                "entitlement_plan_key": "agent",
                "entitlement_source": "operator_grant",
                "entitlement_grant_id": "pq-lifetime-agent-boundary",
            },
        },
        trusted_commercial_update=True,
    )
    attempted_downgrade = client.post(
        "/v1/onboarding/property-search/preferences",
        json={
            "location_query": "Graz",
            "property_commercial": {
                "active_plan_key": "free",
                "status": "free",
            },
        },
    )
    assert attempted_downgrade.status_code == 200
    preferences = attempted_downgrade.json()["property_search_preferences"]
    assert preferences["location_query"] == "Graz"
    assert preferences["property_commercial"]["active_plan_key"] == "agent"
    assert preferences["property_commercial"]["entitlement_kind"] == "lifetime"
