from __future__ import annotations

import builtins
import os
import sys
import types
from types import SimpleNamespace
import warnings
import pytest

from app.container import ReadinessService
from app.settings import get_settings


def _clear_env() -> None:
    for key in (
        "EA_APP_NAME",
        "EA_APP_VERSION",
        "EA_ROLE",
        "EA_HOST",
        "EA_PORT",
        "EA_LOG_LEVEL",
        "EA_TENANT_ID",
        "EA_RUNTIME_MODE",
        "EA_STORAGE_FALLBACK_ALLOWED",
        "EA_STORAGE_BACKEND",
        "EA_LEDGER_BACKEND",
        "DATABASE_URL",
        "EA_ARTIFACTS_DIR",
        "EA_API_TOKEN",
        "EA_DEFAULT_PRINCIPAL_ID",
        "EA_MAX_REWRITE_CHARS",
        "EA_APPROVAL_THRESHOLD_CHARS",
        "EA_APPROVAL_TTL_MINUTES",
        "EA_CHANNEL_DEFAULT_LIMIT",
        "EA_ENABLE_PUBLIC_SIDE_SURFACES",
        "EA_ENABLE_PUBLIC_RESULTS",
        "EA_ENABLE_PUBLIC_TOURS",
        "EA_ENABLE_PUBLIC_MEMORIALS",
        "PROPERTYQUARRY_ENABLE_PUBLIC_SIDE_SURFACES",
        "PROPERTYQUARRY_ENABLE_PUBLIC_RESULTS",
        "PROPERTYQUARRY_ENABLE_PUBLIC_TOURS",
        "PROPERTYQUARRY_ENABLE_PUBLIC_MEMORIALS",
        "PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES",
        "EA_ENABLE_LEGACY_RUNTIME_SURFACES",
    ):
        os.environ.pop(key, None)


def test_settings_defaults() -> None:
    _clear_env()
    s = get_settings()
    assert s.core.app_name == "PropertyQuarry"
    assert s.core.role == "api"
    assert s.runtime.mode == "dev"
    assert s.storage.backend == "auto"
    assert s.storage.database_url == ""
    assert s.auth.enabled is False
    assert s.auth.default_principal_id == "local-user"
    assert s.policy.max_rewrite_chars == 20000
    assert s.policy.approval_required_chars == 5000
    assert s.policy.approval_ttl_minutes == 120
    assert s.channels.default_list_limit == 50


def test_settings_legacy_backend_fallback() -> None:
    _clear_env()
    os.environ["EA_LEDGER_BACKEND"] = "postgres"
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s = get_settings()
    assert s.storage.backend == "postgres"
    assert s.ledger_backend == "postgres"
    assert s.database_url == "postgresql://example.invalid/ea"
    assert any("EA_LEDGER_BACKEND is deprecated" in str(w.message) for w in caught)


def test_settings_explicit_storage_backend_wins() -> None:
    _clear_env()
    os.environ["EA_LEDGER_BACKEND"] = "memory"
    os.environ["EA_STORAGE_BACKEND"] = "postgres"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        s = get_settings()
    assert s.storage.backend == "postgres"
    assert any("ignored when EA_STORAGE_BACKEND is set" in str(w.message) for w in caught)


def test_policy_threshold_overrides() -> None:
    _clear_env()
    os.environ["EA_APPROVAL_THRESHOLD_CHARS"] = "42"
    os.environ["EA_APPROVAL_TTL_MINUTES"] = "15"
    s = get_settings()
    assert s.policy.approval_required_chars == 42
    assert s.policy.approval_ttl_minutes == 15


def test_runtime_mode_prod_disables_storage_fallback() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "super-secret"
    s = get_settings()
    assert s.runtime.mode == "prod"
    assert s.storage_fallback_allowed is False


def test_non_prod_storage_fallback_can_be_disabled_explicitly() -> None:
    _clear_env()
    os.environ["EA_STORAGE_FALLBACK_ALLOWED"] = "0"
    s = get_settings()
    assert s.runtime.mode == "dev"
    assert s.storage_fallback_allowed is False


def test_non_prod_storage_fallback_can_be_enabled_explicitly() -> None:
    _clear_env()
    os.environ["EA_STORAGE_FALLBACK_ALLOWED"] = "1"
    s = get_settings()
    assert s.runtime.mode == "dev"
    assert s.storage_fallback_allowed is True


def test_prod_ignores_storage_fallback_override() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "super-secret"
    os.environ["EA_STORAGE_FALLBACK_ALLOWED"] = "1"
    s = get_settings()
    assert s.runtime.mode == "prod"
    assert s.storage_fallback_allowed is False


def test_runtime_mode_case_variants_disables_storage_fallback() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "PrOd"
    os.environ["EA_API_TOKEN"] = "super-secret"
    s = get_settings()
    assert s.runtime.mode == "prod"
    assert s.storage_fallback_allowed is False


def test_runtime_mode_prod_rejects_empty_api_token() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    with pytest.raises(RuntimeError, match="EA_RUNTIME_MODE=prod requires EA_API_TOKEN or Cloudflare Access auth to be set"):
        _ = get_settings()


def test_runtime_mode_prod_rejects_whitespace_api_token() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "   \t\n"
    with pytest.raises(RuntimeError, match="EA_RUNTIME_MODE=prod requires EA_API_TOKEN or Cloudflare Access auth to be set"):
        _ = get_settings()


@pytest.mark.parametrize(
    "runtime_mode",
    ("unknown-mode", "production", "prdo"),
)
def test_runtime_mode_unknown_fails_closed(runtime_mode: str) -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = runtime_mode
    with pytest.raises(
        RuntimeError,
        match="EA_RUNTIME_MODE must be one of: dev, test, prod",
    ):
        _ = get_settings()


def test_runtime_mode_whitespace_defaults_to_dev() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = " \t\n"
    assert get_settings().runtime.mode == "dev"


def test_default_principal_override() -> None:
    _clear_env()
    os.environ["EA_DEFAULT_PRINCIPAL_ID"] = "exec-1"
    s = get_settings()
    assert s.auth.default_principal_id == "exec-1"


def test_propertyquarry_public_surface_aliases_override_ea_defaults() -> None:
    _clear_env()
    os.environ["EA_ENABLE_PUBLIC_SIDE_SURFACES"] = "1"
    os.environ["EA_ENABLE_PUBLIC_RESULTS"] = "1"
    os.environ["EA_ENABLE_PUBLIC_TOURS"] = "1"
    os.environ["EA_ENABLE_PUBLIC_MEMORIALS"] = "1"
    os.environ["PROPERTYQUARRY_ENABLE_PUBLIC_SIDE_SURFACES"] = "0"
    os.environ["PROPERTYQUARRY_ENABLE_PUBLIC_RESULTS"] = "0"
    os.environ["PROPERTYQUARRY_ENABLE_PUBLIC_TOURS"] = "0"
    os.environ["PROPERTYQUARRY_ENABLE_PUBLIC_MEMORIALS"] = "0"
    s = get_settings()
    assert s.public_side_surfaces_enabled is False
    assert s.public_results_enabled is False
    assert s.public_tours_enabled is False
    assert s.public_memorials_enabled is False


def test_propertyquarry_public_surface_aliases_can_enable_tours_without_memorials() -> None:
    _clear_env()
    os.environ["PROPERTYQUARRY_ENABLE_PUBLIC_TOURS"] = "1"
    s = get_settings()
    assert s.public_side_surfaces_enabled is True
    assert s.public_tours_enabled is True
    assert s.public_results_enabled is False
    assert s.public_memorials_enabled is False


def test_propertyquarry_legacy_runtime_surface_alias_defaults_off_and_can_enable() -> None:
    _clear_env()
    s = get_settings()
    assert s.legacy_runtime_surfaces_enabled is False

    os.environ["PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES"] = "1"
    s = get_settings()
    assert s.legacy_runtime_surfaces_enabled is True


def test_readiness_service_rejects_prod_without_api_token() -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="prod"),
        storage=SimpleNamespace(backend="postgres", database_url="postgresql://example/ea"),
        auth=SimpleNamespace(api_token=""),
    )
    ready, reason = ReadinessService(settings).check()
    assert ready is False
    assert reason == "prod_api_token_missing"


def test_readiness_service_rejects_prod_with_whitespace_api_token() -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="prod"),
        storage=SimpleNamespace(backend="postgres", database_url="postgresql://example/ea"),
        auth=SimpleNamespace(api_token="  \t"),
    )
    ready, reason = ReadinessService(settings).check()
    assert ready is False
    assert reason == "prod_api_token_missing"


def test_readiness_service_checks_token_before_dependencies_in_prod() -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="PROD"),
        storage=SimpleNamespace(backend="auto", database_url=""),
        auth=SimpleNamespace(api_token="  \n\t"),
    )
    ready, reason = ReadinessService(settings).check()
    assert ready is False
    assert reason == "prod_api_token_missing"


def test_readiness_service_rejects_case_variant_prod_mode_without_api_token() -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="PrOd"),
        storage=SimpleNamespace(backend="postgres", database_url="postgresql://example/ea"),
        auth=SimpleNamespace(api_token="  \t"),
    )
    ready, reason = ReadinessService(settings).check()
    assert ready is False
    assert reason == "prod_api_token_missing"


def test_readiness_service_rejects_prod_postgres_without_database_url() -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="prod"),
        storage=SimpleNamespace(backend="postgres", database_url=""),
        auth=SimpleNamespace(api_token="secret-token", signing_secret="signing-secret"),
    )
    ready, reason = ReadinessService(settings).check()
    assert ready is False
    assert reason == "database_url_missing"


def test_readiness_service_blocks_registered_pending_startup_gate() -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="dev"),
        storage=SimpleNamespace(backend="memory", database_url=""),
        auth=SimpleNamespace(api_token="", signing_secret=""),
    )
    readiness = ReadinessService(settings)
    readiness.register_startup_gate("property_search_shell_prewarm")

    ready, reason = readiness.check()

    assert ready is False
    assert reason == "property_search_shell_prewarm:pending"


def test_readiness_service_reports_failed_startup_gate_reason() -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="dev"),
        storage=SimpleNamespace(backend="memory", database_url=""),
        auth=SimpleNamespace(api_token="", signing_secret=""),
    )
    readiness = ReadinessService(settings)
    readiness.mark_startup_gate_failed("property_search_shell_prewarm", "failed")

    ready, reason = readiness.check()

    assert ready is False
    assert reason == "property_search_shell_prewarm:failed"


def test_readiness_service_allows_ready_startup_gate() -> None:
    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="dev"),
        storage=SimpleNamespace(backend="memory", database_url=""),
        auth=SimpleNamespace(api_token="", signing_secret=""),
    )
    readiness = ReadinessService(settings)
    readiness.register_startup_gate("property_search_shell_prewarm")
    readiness.mark_startup_gate_ready("property_search_shell_prewarm")

    ready, reason = readiness.check()

    assert ready is True
    assert reason == "memory_ready"


def test_readiness_service_rejects_missing_psycopg_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    original_import = builtins.__import__

    def _raise_for_psycopg(name: str, globals: dict, locals: dict, fromlist: tuple, level: int = 0):  # type: ignore[override]
        if name == "psycopg":
            raise ImportError("psycopg intentionally unavailable")
        return original_import(name, globals, locals, fromlist, level)

    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="prod"),
        storage=SimpleNamespace(backend="postgres", database_url="postgresql://example/ea"),
        auth=SimpleNamespace(api_token="secret-token", signing_secret="signing-secret"),
    )
    try:
        monkeypatch.setattr(builtins, "__import__", _raise_for_psycopg)
        ready, reason = ReadinessService(settings).check()
    finally:
        monkeypatch.setattr(builtins, "__import__", original_import)
    assert ready is False
    assert reason == "psycopg_missing"


def test_readiness_service_rejects_unavailable_postgres_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadPsycopg:
        @staticmethod
        def connect(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("network unreachable")

    settings = SimpleNamespace(
        runtime=SimpleNamespace(mode="prod"),
        storage=SimpleNamespace(backend="postgres", database_url="postgresql://example/ea"),
        auth=SimpleNamespace(api_token="secret-token", signing_secret="signing-secret"),
    )
    fake_psycopg = types.SimpleNamespace(connect=_BadPsycopg.connect)
    try:
        old_psycopg = sys.modules.get("psycopg")
        sys.modules["psycopg"] = fake_psycopg
        ready, reason = ReadinessService(settings).check()
    finally:
        if old_psycopg is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = old_psycopg
    assert ready is False
    assert reason == "postgres_unavailable:RuntimeError"
