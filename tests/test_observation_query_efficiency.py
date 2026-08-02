from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

import app.product.service as product_service
from app.api.routes.landing import _property_access_link_snapshot
from app.repositories.observation import InMemoryObservationEventRepository
from app.repositories.observation_postgres import PostgresObservationEventRepository
from tests.product_test_helpers import build_property_client


def test_in_memory_recent_queries_preserve_window_tenant_and_event_filters() -> None:
    repository = InMemoryObservationEventRepository()
    repository.append(
        "tenant-a",
        "product",
        "workspace_access_session_issued",
        {"session_id": "session-a"},
        source_id="session-a",
        dedupe_key="tenant-a|session-a",
    )
    repository.append("tenant-a", "product", "unrelated_event", {"large": "x" * 10_000})
    repository.append(
        "tenant-b",
        "product",
        "workspace_access_session_issued",
        {"session_id": "session-b"},
        source_id="session-b",
        dedupe_key="tenant-b|session-b",
    )

    assert not repository.exists_recent(
        principal_id="tenant-a",
        channel="product",
        event_type="workspace_access_session_issued",
        source_id="session-a",
        limit=2,
    )
    assert repository.exists_recent(
        principal_id="tenant-a",
        channel="product",
        event_type="workspace_access_session_issued",
        source_id="session-a",
        limit=3,
    )
    assert not repository.exists_recent(
        principal_id="tenant-a",
        channel="product",
        event_type="workspace_access_session_issued",
        source_id="session-b",
        limit=3,
    )

    rows = repository.list_recent_matching(
        limit=3,
        principal_id="tenant-a",
        channel="product",
        event_types=("workspace_access_session_issued", "workspace_access_session_revoked"),
    )
    assert [row.source_id for row in rows] == ["session-a"]


def test_recent_product_event_exists_uses_payload_free_runtime_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_property_client(principal_id="event-exists-query")
    service = product_service.build_product_service(client.app.state.container)
    runtime = client.app.state.container.channel_runtime
    captured: dict[str, object] = {}

    def _recent_exists(**kwargs) -> bool:
        captured.update(kwargs)
        return True

    monkeypatch.setattr(runtime, "recent_observation_exists", _recent_exists)
    monkeypatch.setattr(
        runtime,
        "list_recent_observations",
        lambda *args, **kwargs: pytest.fail("full observation payload scan is forbidden"),
    )

    assert service._recent_product_event_exists(
        principal_id="event-exists-query",
        event_type="property_search_results_ready_email_sent",
        source_id="run-1",
        dedupe_key="event-exists-query|run-1|results-ready",
    )
    assert captured == {
        "channel": "product",
        "event_type": "property_search_results_ready_email_sent",
        "source_id": "run-1",
        "dedupe_key": "event-exists-query|run-1|results-ready",
        "external_id": "",
        "limit": 1000,
        "principal_id": "event-exists-query",
    }


def test_workspace_access_legacy_replay_fetches_only_access_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal_id = f"workspace-access-query-{uuid4().hex}"
    client = build_property_client(principal_id=principal_id)
    service = product_service.build_product_service(client.app.state.container)
    runtime = client.app.state.container.channel_runtime
    captured: dict[str, object] = {}
    issued = SimpleNamespace(
        observation_id="event-issued",
        created_at="2026-07-13T12:00:00+00:00",
        event_type="workspace_access_session_issued",
        source_id="session-1",
        payload={
            "session_id": "session-1",
            "principal_id": principal_id,
            "email": "operator@example.test",
            "role": "operator",
        },
    )
    revoked = SimpleNamespace(
        observation_id="event-revoked",
        created_at="2026-07-13T12:01:00+00:00",
        event_type="workspace_access_session_revoked",
        source_id="session-1",
        payload={"session_id": "session-1", "revoked_by": principal_id},
    )

    def _matching(**kwargs):
        captured.update(kwargs)
        return [revoked, issued]

    monkeypatch.setattr(product_service, "list_workspace_access_session_records", lambda **kwargs: ())
    monkeypatch.setattr(runtime, "list_recent_observations_matching", _matching)
    monkeypatch.setattr(
        runtime,
        "list_recent_observations",
        lambda *args, **kwargs: pytest.fail("full observation payload scan is forbidden"),
    )

    sessions = service.list_workspace_access_sessions(principal_id=principal_id)

    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "session-1"
    assert sessions[0]["status"] == "revoked"
    assert captured == {
        "limit": 1000,
        "principal_id": principal_id,
        "event_types": ("workspace_access_session_issued", "workspace_access_session_revoked"),
    }


def test_property_access_snapshot_fetches_sessions_once_and_preserves_caps() -> None:
    class Product:
        calls: list[dict[str, object]] = []

        def list_workspace_access_sessions(self, **kwargs):
            self.calls.append(dict(kwargs))
            return tuple(
                [
                    {
                        "session_id": f"active-{index}",
                        "email": f"active-{index}@example.test",
                        "role": "operator",
                        "status": "active",
                    }
                    for index in range(55)
                ]
                + [
                    {
                        "session_id": f"revoked-{index}",
                        "email": f"revoked-{index}@example.test",
                        "role": "principal",
                        "status": "revoked",
                    }
                    for index in range(25)
                ]
            )

    product = Product()
    snapshot = _property_access_link_snapshot(product=product, principal_id="tenant-a")

    assert product.calls == [{"principal_id": "tenant-a", "status": "", "limit": 1000}]
    assert snapshot["active_total"] == 50
    assert snapshot["revoked_total"] == 20
    assert [row["session_id"] for row in snapshot["rows"]] == ["active-0", "active-1", "active-2"]


def test_workspace_access_principal_legacy_fallback_uses_targeted_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = build_property_client(principal_id="workspace-access-resolver")
    service = product_service.build_product_service(client.app.state.container)
    runtime = client.app.state.container.channel_runtime
    captured: dict[str, object] = {}

    def _matching(**kwargs):
        captured.update(kwargs)
        return [
            SimpleNamespace(
                event_type="workspace_access_session_issued",
                source_id="session-1",
                principal_id="tenant-a",
                payload={"session_id": "session-1", "principal_id": "tenant-a"},
            )
        ]

    monkeypatch.setattr(product_service, "get_workspace_access_session_record_by_session_id", lambda **kwargs: None)
    monkeypatch.setattr(runtime, "list_recent_observations_matching", _matching)
    monkeypatch.setattr(
        runtime,
        "list_recent_observations",
        lambda *args, **kwargs: pytest.fail("full observation payload scan is forbidden"),
    )

    assert service._resolve_workspace_access_session_principal(session_id="session-1") == "tenant-a"
    assert captured == {
        "limit": 5000,
        "event_types": ("workspace_access_session_issued",),
    }


def _postgres_database_url() -> str:
    database_url = str(os.environ.get("EA_TEST_PROPERTY_DATABASE_URL") or "").strip()
    if not database_url:
        pytest.skip("EA_TEST_PROPERTY_DATABASE_URL is not set")
    return database_url


def test_postgres_recent_queries_avoid_unrelated_payloads_and_preserve_window() -> None:
    database_url = _postgres_database_url()
    import psycopg
    from psycopg import sql

    namespace = f"observation_query_{uuid4().hex}"

    class IsolatedRepository(PostgresObservationEventRepository):
        def _connect(self):  # type: ignore[no-untyped-def]
            connection = psycopg.connect(database_url, autocommit=True, connect_timeout=5)
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(namespace)))
            return connection

    with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as admin:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(namespace)))
    try:
        repository = IsolatedRepository(database_url)
        repository.append(
            "tenant-a",
            "product",
            "workspace_access_session_issued",
            {"session_id": "session-a"},
            source_id="session-a",
        )
        repository.append(
            "tenant-a",
            "product",
            "unrelated_event",
            {"large": "x" * 2_000_000},
            raw_payload_uri="s3://propertyquarry-observations/query-efficiency.json",
        )

        assert not repository.exists_recent(
            principal_id="tenant-a",
            channel="product",
            event_type="workspace_access_session_issued",
            source_id="session-a",
            limit=1,
        )
        assert repository.exists_recent(
            principal_id="tenant-a",
            channel="product",
            event_type="workspace_access_session_issued",
            source_id="session-a",
            limit=2,
        )
        rows = repository.list_recent_matching(
            limit=2,
            principal_id="tenant-a",
            channel="product",
            event_types=("workspace_access_session_issued",),
        )
        assert [row.source_id for row in rows] == ["session-a"]
        assert rows[0].payload == {"session_id": "session-a"}
    finally:
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as admin:
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(namespace)))
