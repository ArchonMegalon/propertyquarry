from __future__ import annotations

from dataclasses import replace
import sys
from types import SimpleNamespace

import pytest

import app.container as container_module
from app.container import ReadinessService
from app.services.admission_control import AdmissionBackendUnavailable
from app.settings import Settings, get_settings


class _Cursor:
    def __init__(self, lane: str) -> None:
        self.lane = lane
        self._row: tuple[object, ...] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: object = ()) -> None:
        normalized = " ".join(sql.split())
        if normalized == "SELECT 1":
            self._row = (1,)
        elif "FROM property_search_erasure_key_state" in normalized:
            self._row = ("expected-key-id",)
        else:
            self._row = None

    def fetchone(self) -> tuple[object, ...] | None:
        return self._row


class _Connection:
    def __init__(self, lane: str) -> None:
        self.lane = lane

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.lane)


def _settings(primary_url: str) -> SimpleNamespace:
    return SimpleNamespace(
        runtime_mode="prod",
        role="api",
        database_url=primary_url,
    )


def _nested_settings(
    primary_url: str,
    *,
    runtime_mode: str,
    role: str,
    nested_runtime_mode: str = "prod",
    nested_role: str = "api",
) -> SimpleNamespace:
    return SimpleNamespace(
        runtime=SimpleNamespace(mode=nested_runtime_mode),
        core=SimpleNamespace(role=nested_role),
        runtime_mode=runtime_mode,
        role=role,
        database_url=primary_url,
    )


def test_prod_api_readiness_probes_admission_with_the_dedicated_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "postgresql://api-runtime/property"
    admission_url = "postgresql://api-admission/property"
    connections: list[tuple[str, dict[str, object]]] = []

    def connect(database_url: str, **kwargs: object) -> _Connection:
        connections.append((database_url, dict(kwargs)))
        lane = "admission" if database_url == admission_url else "primary"
        return _Connection(lane)

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    monkeypatch.setenv(
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
        admission_url,
    )
    from app.product import property_search_schema
    from app.product import property_search_storage

    monkeypatch.setattr(
        property_search_schema,
        "property_search_schema_readiness_required",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        property_search_schema,
        "inspect_property_search_schema_cursor",
        lambda _cursor, **_kwargs: SimpleNamespace(
            ready=True,
            reason="ready",
            current_version=17,
        ),
    )
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_erasure_key_id",
        lambda: "expected-key-id",
    )

    def probe(cursor: _Cursor) -> None:
        assert cursor.lane == "admission"

    monkeypatch.setattr(container_module, "probe_admission_cursor", probe)

    assert ReadinessService(_settings(primary_url))._probe_database() == (
        True,
        "postgres_ready:property_search_schema_v17",
    )
    assert connections == [
        (primary_url, {"autocommit": True, "connect_timeout": 3}),
        (
            admission_url,
            {
                "autocommit": True,
                "connect_timeout": 5,
                "application_name": "propertyquarry-api-admission-readiness",
            },
        ),
    ]


def test_prod_api_readiness_fails_before_database_io_without_admission_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("missing dedicated admission configuration reached database I/O")

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=forbidden_connect),
    )
    monkeypatch.delenv(
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
        raising=False,
    )

    assert ReadinessService(
        _settings("postgresql://api-runtime/property")
    )._probe_database() == (
        False,
        "propertyquarry_admission_not_ready:"
        "propertyquarry_api_admission_database_url_required",
    )


def test_prod_api_readiness_uses_nested_authority_when_flattened_aliases_are_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("blank compatibility aliases skipped nested production authority")

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=forbidden_connect),
    )
    monkeypatch.delenv(
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
        raising=False,
    )

    assert ReadinessService(
        _nested_settings(
            "postgresql://api-runtime/property",
            runtime_mode=" \t ",
            role="\n",
        )
    )._probe_database() == (
        False,
        "propertyquarry_admission_not_ready:"
        "propertyquarry_api_admission_database_url_required",
    )


def test_prod_worker_readiness_uses_nested_authority_for_schema_when_aliases_are_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "postgresql://worker-runtime/property"
    connections: list[str] = []

    def connect(database_url: str, **_kwargs: object) -> _Connection:
        connections.append(database_url)
        return _Connection("primary")

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=connect),
    )
    from app.product import property_search_schema
    from app.product import property_search_storage

    readiness_authority: list[tuple[str, str]] = []

    def readiness_required(*, runtime_mode: str, role: str, **_kwargs: object) -> bool:
        readiness_authority.append((runtime_mode, role))
        return True

    monkeypatch.setattr(
        property_search_schema,
        "property_search_schema_readiness_required",
        readiness_required,
    )
    monkeypatch.setattr(
        property_search_schema,
        "inspect_property_search_schema_cursor",
        lambda _cursor, **_kwargs: SimpleNamespace(
            ready=True,
            reason="ready",
            current_version=17,
        ),
    )
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_erasure_key_id",
        lambda: "expected-key-id",
    )

    assert ReadinessService(
        _nested_settings(
            primary_url,
            runtime_mode="",
            role="  ",
            nested_role="worker",
        )
    )._probe_database() == (
        True,
        "postgres_ready:property_search_schema_v17",
    )
    assert readiness_authority == [("prod", "worker")]
    assert connections == [primary_url]


@pytest.mark.parametrize(
    ("runtime_mode", "role", "expected_reason"),
    (
        ("dev", "api", "readiness_settings_conflict:runtime_mode"),
        ("prod", "worker", "readiness_settings_conflict:role"),
    ),
)
def test_prod_api_readiness_fails_closed_on_conflicting_flattened_authority(
    monkeypatch: pytest.MonkeyPatch,
    runtime_mode: str,
    role: str,
    expected_reason: str,
) -> None:
    def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("conflicting readiness authority reached database I/O")

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=forbidden_connect),
    )

    readiness = ReadinessService(
        _nested_settings(
            "postgresql://api-runtime/property",
            runtime_mode=runtime_mode,
            role=role,
        )
    )

    assert readiness.check() == (False, expected_reason)
    assert readiness._probe_database() == (False, expected_reason)


def _default_memory_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("EA_RUNTIME_MODE", "dev")
    monkeypatch.setenv("EA_ROLE", "api")
    monkeypatch.setenv("EA_STORAGE_BACKEND", "memory")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return get_settings()


def test_readiness_accepts_valid_default_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _default_memory_settings(monkeypatch)

    assert ReadinessService(settings).check() == (True, "memory_ready")


@pytest.mark.parametrize(
    ("missing_field", "expected_reason"),
    (
        ("runtime_mode", "readiness_settings_missing:runtime_mode"),
        ("role", "readiness_settings_missing:role"),
    ),
)
def test_readiness_fails_closed_when_real_settings_authority_is_blank(
    monkeypatch: pytest.MonkeyPatch,
    missing_field: str,
    expected_reason: str,
) -> None:
    settings = _default_memory_settings(monkeypatch)
    if missing_field == "runtime_mode":
        settings = replace(
            settings,
            runtime=replace(settings.runtime, mode=" \t "),
        )
    else:
        settings = replace(
            settings,
            core=replace(settings.core, role="\n"),
        )

    def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("missing readiness authority reached database I/O")

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=forbidden_connect),
    )
    readiness = ReadinessService(settings)

    assert readiness.check() == (False, expected_reason)
    assert readiness._probe_database() == (False, expected_reason)


@pytest.mark.parametrize(
    ("invalid_field", "expected_reason"),
    (
        ("runtime_mode", "readiness_settings_invalid:runtime_mode"),
        ("role", "readiness_settings_invalid:role"),
    ),
)
def test_readiness_fails_closed_when_real_settings_authority_is_invalid(
    monkeypatch: pytest.MonkeyPatch,
    invalid_field: str,
    expected_reason: str,
) -> None:
    settings = _default_memory_settings(monkeypatch)
    if invalid_field == "runtime_mode":
        settings = replace(
            settings,
            runtime=replace(settings.runtime, mode="production"),
        )
    else:
        settings = replace(
            settings,
            core=replace(settings.core, role="appi"),
        )

    def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid readiness authority reached database I/O")

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=forbidden_connect),
    )
    readiness = ReadinessService(settings)

    assert readiness.check() == (False, expected_reason)
    assert readiness._probe_database() == (False, expected_reason)


def test_operator_tools_readiness_is_nonproduction_and_fails_closed_in_prod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _default_memory_settings(monkeypatch)
    settings = replace(
        settings,
        core=replace(settings.core, role="operator-tools"),
    )
    assert ReadinessService(settings).check() == (True, "memory_ready")

    production_settings = replace(
        settings,
        runtime=replace(settings.runtime, mode="prod"),
    )

    def forbidden_connect(*_args: object, **_kwargs: object) -> None:
        pytest.fail("production operator tools authority reached database I/O")

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=forbidden_connect),
    )
    readiness = ReadinessService(production_settings)
    expected = (False, "readiness_settings_invalid:operator_tools_prod")

    assert readiness.check() == expected
    assert readiness._probe_database() == expected


def test_prod_api_readiness_preserves_bounded_admission_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_url = "postgresql://api-runtime/property"
    admission_url = "postgresql://api-admission/property"

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(
            connect=lambda database_url, **_kwargs: _Connection(
                "admission" if database_url == admission_url else "primary"
            )
        ),
    )
    monkeypatch.setenv(
        "PROPERTYQUARRY_API_ADMISSION_DATABASE_URL",
        admission_url,
    )
    from app.product import property_search_schema
    from app.product import property_search_storage

    monkeypatch.setattr(
        property_search_schema,
        "property_search_schema_readiness_required",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        property_search_schema,
        "inspect_property_search_schema_cursor",
        lambda _cursor, **_kwargs: SimpleNamespace(
            ready=True,
            reason="ready",
            current_version=17,
        ),
    )
    monkeypatch.setattr(
        property_search_storage,
        "_property_search_erasure_key_id",
        lambda: "expected-key-id",
    )

    def fail_probe(_cursor: _Cursor) -> None:
        raise AdmissionBackendUnavailable("admission_backend_role_elevated")

    monkeypatch.setattr(
        container_module,
        "probe_admission_cursor",
        fail_probe,
    )

    assert ReadinessService(_settings(primary_url))._probe_database() == (
        False,
        "propertyquarry_admission_not_ready:admission_backend_role_elevated",
    )
