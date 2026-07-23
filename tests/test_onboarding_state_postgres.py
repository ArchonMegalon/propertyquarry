from __future__ import annotations

import os

import pytest

from app.domain.models import OnboardingState
from app.repositories.onboarding_state_postgres import PostgresOnboardingStateRepository


class _FakeCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, tuple[object, ...] | None]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> None:
        self.executions.append((query, params))
        if "INSERT INTO onboarding_states" in query:
            assert params is not None
            assert query.count("%s") == len(params)

    def fetchone(self):
        return (
            "onb-1",
            "principal-1",
            "Workspace",
            "personal",
            "AT",
            "en",
            "Europe/Vienna",
            ["google"],
            {"selected_platforms": ["willhaben"], "preference_person_id": "elisabeth", "max_results_per_source": 3},
            {},
            {},
            {},
            "started",
            "2026-06-02T00:00:00+00:00",
            "2026-06-02T00:00:00+00:00",
        )

    def fetchall(self):
        return [
            self.fetchone(),
            (
                "onb-2",
                "principal-2",
                "Workspace 2",
                "personal",
                "DE",
                "de",
                "Europe/Berlin",
                [],
                {"selected_platforms": ["immowelt_de"]},
                {},
                {},
                {},
                "completed",
                "2026-06-01T00:00:00+00:00",
                "2026-06-03T00:00:00+00:00",
            ),
        ]


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> _FakeConnection:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor


def test_postgres_onboarding_upsert_matches_placeholder_count(monkeypatch) -> None:
    schema_cursor = _FakeCursor()
    write_cursor = _FakeCursor()
    connections = [_FakeConnection(schema_cursor), _FakeConnection(write_cursor)]

    def _fake_connect(self):
        return connections.pop(0)

    monkeypatch.setattr(PostgresOnboardingStateRepository, "_connect", _fake_connect)
    repo = PostgresOnboardingStateRepository("postgresql://example")
    monkeypatch.setattr(repo, "get_for_principal", lambda principal_id: None)
    monkeypatch.setattr(repo, "_json_value", lambda value: value)

    row = repo.upsert_state(
        principal_id="principal-1",
        workspace_name="Workspace",
        workspace_mode="personal",
        region="AT",
        language="en",
        timezone="Europe/Vienna",
        selected_channels=("google",),
        property_search_preferences_json={
            "selected_platforms": ["willhaben"],
            "preference_person_id": "elisabeth",
            "max_results_per_source": 3,
        },
        status="started",
    )

    assert isinstance(row, OnboardingState)
    insert_queries = [query for query, _params in write_cursor.executions if "INSERT INTO onboarding_states" in query]
    assert insert_queries


def test_postgres_onboarding_list_states_reads_recent_rows(monkeypatch) -> None:
    schema_cursor = _FakeCursor()
    read_cursor = _FakeCursor()
    connections = [_FakeConnection(schema_cursor), _FakeConnection(read_cursor)]

    def _fake_connect(self):
        return connections.pop(0)

    monkeypatch.setattr(PostgresOnboardingStateRepository, "_connect", _fake_connect)
    repo = PostgresOnboardingStateRepository("postgresql://example")

    rows = repo.list_states(limit=2)

    assert [row.principal_id for row in rows] == ["principal-1", "principal-2"]
    select_queries = [query for query, _params in read_cursor.executions if "FROM onboarding_states" in query]
    assert select_queries


def test_enabled_search_agent_principal_projection_never_hydrates_onboarding_state(
    monkeypatch,
) -> None:
    class _PrincipalProjectionCursor(_FakeCursor):
        def fetchall(self):
            return [
                ("principal-enabled",),
                ("principal-legacy",),
                ("",),
            ]

    schema_cursor = _FakeCursor()
    read_cursor = _PrincipalProjectionCursor()
    connections = [_FakeConnection(schema_cursor), _FakeConnection(read_cursor)]

    def _fake_connect(self):
        return connections.pop(0)

    monkeypatch.setattr(PostgresOnboardingStateRepository, "_connect", _fake_connect)
    repo = PostgresOnboardingStateRepository("postgresql://example")
    monkeypatch.setattr(
        repo,
        "_from_row",
        lambda _row: (_ for _ in ()).throw(
            AssertionError("principal projection must not materialize OnboardingState")
        ),
    )

    principals = repo.list_enabled_property_search_agent_principals(limit=25)

    assert principals == ("principal-enabled", "principal-legacy")
    query, params = next(
        (query, params)
        for query, params in read_cursor.executions
        if "FROM onboarding_states" in query
    )
    normalized_query = " ".join(query.lower().split())
    select_list = normalized_query.split(" from onboarding_states", 1)[0]
    assert select_list == "select principal_id"
    assert "property_search_preferences_json" not in select_list
    assert "jsonb_array_elements" in normalized_query
    assert "search_agent_enabled" in normalized_query
    assert normalized_query.index(" where ") < normalized_query.index(" limit ")
    assert params == (25,)


def test_postgres_enabled_search_agent_projection_semantics() -> None:
    database_url = str(
        os.environ.get("EA_TEST_PROPERTY_DATABASE_URL")
        or os.environ.get("EA_TEST_DATABASE_URL")
        or ""
    ).strip()
    if not database_url:
        pytest.skip(
            "EA_TEST_PROPERTY_DATABASE_URL or EA_TEST_DATABASE_URL is not set"
        )

    import psycopg
    from psycopg.types.json import Jsonb

    cases = (
        (
            "explicit-enabled",
            {"search_agents": [{"enabled": " YES "}]},
            7,
        ),
        (
            "explicit-disabled",
            {
                "search_agent_enabled": True,
                "search_agents": [{"enabled": False}],
            },
            6,
        ),
        (
            "explicit-inherits-legacy",
            {
                "search_agent_enabled": "active",
                "search_agents": [{"name": "Legacy inherited flag"}],
            },
            5,
        ),
        (
            "explicit-empty",
            {"search_agent_enabled": True, "search_agents": []},
            4,
        ),
        ("legacy-enabled", {"search_agent_enabled": 1}, 3),
        (
            "legacy-top-enabled",
            {"enabled": "on", "search_agent_enabled": False},
            2,
        ),
        ("legacy-paused", {"search_agent_enabled": False}, 1),
    )

    class _BorrowedConnection:
        def __init__(self, connection) -> None:
            self.connection = connection

        def __enter__(self):
            return self.connection

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    with psycopg.connect(database_url, autocommit=False) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TEMP TABLE onboarding_states (
                    principal_id TEXT PRIMARY KEY,
                    property_search_preferences_json JSONB NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                ) ON COMMIT DROP
                """
            )
            cursor.executemany(
                """
                INSERT INTO onboarding_states (
                    principal_id,
                    property_search_preferences_json,
                    updated_at
                )
                VALUES (
                    %s,
                    %s::jsonb,
                    TIMESTAMPTZ '2026-07-23T00:00:00Z'
                        + (%s * INTERVAL '1 minute')
                )
                """,
                tuple(
                    (principal_id, Jsonb(preferences), order)
                    for principal_id, preferences, order in cases
                ),
            )
        repo = object.__new__(PostgresOnboardingStateRepository)
        repo._database_url = database_url
        repo._connect = lambda: _BorrowedConnection(connection)

        principals = repo.list_enabled_property_search_agent_principals(limit=20)

        assert principals == (
            "explicit-enabled",
            "explicit-inherits-legacy",
            "legacy-enabled",
            "legacy-top-enabled",
        )
        connection.rollback()
