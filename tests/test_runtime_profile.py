from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.dependencies import RequestContext, get_request_context, resolve_principal_id
from app.domain.models import TaskContract, now_utc_iso
from app.repositories.task_contracts import InMemoryTaskContractRepository
from app.services.provider_registry import ProviderRegistryService
from app.services.skills import SkillCatalogService
from app.settings import (
    get_settings,
    resolve_signing_secret,
    resolve_runtime_profile,
    validate_startup_settings,
)
from app.services.task_contracts import TaskContractService


def _clear_env() -> None:
    for key in (
        "EA_RUNTIME_MODE",
        "EA_STORAGE_FALLBACK_ALLOWED",
        "EA_STORAGE_BACKEND",
        "EA_LEDGER_BACKEND",
        "DATABASE_URL",
        "EA_API_TOKEN",
        "EA_SIGNING_SECRET",
        "EA_DEFAULT_PRINCIPAL_ID",
        "EA_ALLOW_LOOPBACK_NO_AUTH",
        "EA_REGISTRATION_EMAIL_FROM",
        "EA_REGISTRATION_EMAIL_FROM_FALLBACK",
        "EA_EMAIL_DEFAULT_FROM",
        "EA_ALLOW_NON_PROPERTYQUARRY_EMAIL_SENDER",
        "EA_ALLOWED_EMAIL_SENDER_DOMAINS",
        "EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER",
        "EA_CF_ACCESS_TEAM_DOMAIN",
        "EA_CF_ACCESS_AUD",
        "EA_CF_ACCESS_CERTS_URL",
    ):
        os.environ.pop(key, None)


@pytest.fixture(autouse=True)
def _isolated_env() -> None:
    tracked = {
        "EA_RUNTIME_MODE": os.environ.get("EA_RUNTIME_MODE"),
        "EA_STORAGE_FALLBACK_ALLOWED": os.environ.get("EA_STORAGE_FALLBACK_ALLOWED"),
        "EA_STORAGE_BACKEND": os.environ.get("EA_STORAGE_BACKEND"),
        "EA_LEDGER_BACKEND": os.environ.get("EA_LEDGER_BACKEND"),
        "DATABASE_URL": os.environ.get("DATABASE_URL"),
        "EA_API_TOKEN": os.environ.get("EA_API_TOKEN"),
        "EA_SIGNING_SECRET": os.environ.get("EA_SIGNING_SECRET"),
        "EA_DEFAULT_PRINCIPAL_ID": os.environ.get("EA_DEFAULT_PRINCIPAL_ID"),
        "EA_ALLOW_LOOPBACK_NO_AUTH": os.environ.get("EA_ALLOW_LOOPBACK_NO_AUTH"),
        "EA_REGISTRATION_EMAIL_FROM": os.environ.get("EA_REGISTRATION_EMAIL_FROM"),
        "EA_REGISTRATION_EMAIL_FROM_FALLBACK": os.environ.get("EA_REGISTRATION_EMAIL_FROM_FALLBACK"),
        "EA_EMAIL_DEFAULT_FROM": os.environ.get("EA_EMAIL_DEFAULT_FROM"),
        "EA_ALLOW_NON_PROPERTYQUARRY_EMAIL_SENDER": os.environ.get("EA_ALLOW_NON_PROPERTYQUARRY_EMAIL_SENDER"),
        "EA_ALLOWED_EMAIL_SENDER_DOMAINS": os.environ.get("EA_ALLOWED_EMAIL_SENDER_DOMAINS"),
        "EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER": os.environ.get("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER"),
        "EA_CF_ACCESS_TEAM_DOMAIN": os.environ.get("EA_CF_ACCESS_TEAM_DOMAIN"),
        "EA_CF_ACCESS_AUD": os.environ.get("EA_CF_ACCESS_AUD"),
        "EA_CF_ACCESS_CERTS_URL": os.environ.get("EA_CF_ACCESS_CERTS_URL"),
    }
    _clear_env()
    try:
        yield
    finally:
        for key, value in tracked.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _request(headers: dict[str, str] | None = None) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/context",
            "headers": raw_headers,
            "client": ("127.0.0.1", 49152),
        }
    )


def _container_for_current_settings():
    settings = get_settings()
    profile = resolve_runtime_profile(settings)
    return SimpleNamespace(settings=settings, runtime_profile=profile), profile


def test_runtime_profile_auto_without_database_prefers_memory() -> None:
    _clear_env()
    settings = get_settings()
    profile = resolve_runtime_profile(settings)
    assert profile.storage_backend == "memory"
    assert profile.durability == "ephemeral"
    assert profile.auth_mode == "anonymous_dev"
    assert profile.principal_source == "caller_header_or_default"
    assert profile.caller_principal_header_allowed is True


def test_runtime_profile_non_prod_can_disable_storage_fallback() -> None:
    _clear_env()
    os.environ["EA_STORAGE_FALLBACK_ALLOWED"] = "false"
    settings = get_settings()
    profile = resolve_runtime_profile(settings)
    assert profile.storage_backend == "memory"
    assert settings.storage_fallback_allowed is False


def test_runtime_profile_auto_with_database_prefers_postgres() -> None:
    _clear_env()
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    settings = get_settings()
    profile = resolve_runtime_profile(settings)
    assert profile.storage_backend == "postgres"
    assert profile.durability == "durable"
    assert profile.principal_source == "caller_header_or_default"
    assert profile.caller_principal_header_allowed is True


def test_runtime_profile_non_prod_token_auth_still_allows_caller_header_or_default_principal() -> None:
    _clear_env()
    os.environ["EA_API_TOKEN"] = "secret-token"
    settings = get_settings()
    profile = resolve_runtime_profile(settings)
    assert profile.auth_mode == "token"
    assert profile.principal_source == "authenticated_header_or_default"
    assert profile.caller_principal_header_allowed is True


def test_prod_requires_database_url() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["EA_SIGNING_SECRET"] = "signing-secret"
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        validate_startup_settings(get_settings())


def test_prod_requires_explicit_signing_secret() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    with pytest.raises(RuntimeError, match="EA_SIGNING_SECRET"):
        validate_startup_settings(get_settings())


def test_prod_forbids_loopback_no_auth() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["EA_SIGNING_SECRET"] = "signing-secret"
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    os.environ["EA_ALLOW_LOOPBACK_NO_AUTH"] = "1"
    with pytest.raises(RuntimeError, match="EA_ALLOW_LOOPBACK_NO_AUTH"):
        validate_startup_settings(get_settings())


def test_prod_rejects_inherited_registration_sender_domains() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["EA_SIGNING_SECRET"] = "signing-secret"
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    os.environ["EA_REGISTRATION_EMAIL_FROM"] = "concierge@chummer.run"
    with pytest.raises(RuntimeError, match="PropertyQuarry email sender"):
        validate_startup_settings(get_settings())


def test_prod_rejects_registration_sender_domain_override() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["EA_SIGNING_SECRET"] = "signing-secret"
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    os.environ["EA_REGISTRATION_EMAIL_FROM"] = "concierge@chummer.run"
    os.environ["EA_ALLOW_NON_PROPERTYQUARRY_EMAIL_SENDER"] = "1"
    with pytest.raises(RuntimeError, match="PropertyQuarry email sender"):
        validate_startup_settings(get_settings())


def test_prod_rejects_registration_sender_domain_allowlist() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["EA_SIGNING_SECRET"] = "signing-secret"
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    os.environ["EA_REGISTRATION_EMAIL_FROM"] = "office@girschele.com"
    os.environ["EA_ALLOWED_EMAIL_SENDER_DOMAINS"] = "girschele.com"
    with pytest.raises(RuntimeError, match="PropertyQuarry email sender"):
        validate_startup_settings(get_settings())


def test_prod_runtime_profile_requires_verified_identity_principal() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["EA_SIGNING_SECRET"] = "signing-secret"
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    settings = get_settings()
    profile = resolve_runtime_profile(settings)
    assert profile.auth_mode == "token"
    assert profile.principal_source == "verified_identity"
    assert profile.caller_principal_header_allowed is False
    assert profile.default_principal_fallback_allowed is False


def test_runtime_profile_non_prod_token_auth_matches_request_context_contract() -> None:
    _clear_env()
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["EA_DEFAULT_PRINCIPAL_ID"] = "ops-fallback"
    container, profile = _container_for_current_settings()

    fallback_context = get_request_context(
        _request(headers={"Authorization": "Bearer secret-token"}),
        container=container,
    )
    assert profile.principal_source == "authenticated_header_or_default"
    assert fallback_context.principal_id == "ops-fallback"
    assert fallback_context.authenticated is True

    header_context = get_request_context(
        _request(headers={"Authorization": "Bearer secret-token", "X-EA-Principal-ID": "caller-1"}),
        container=container,
    )
    assert header_context.principal_id == "ops-fallback"

    os.environ["EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER"] = "1"
    container, _ = _container_for_current_settings()
    header_context = get_request_context(
        _request(headers={"Authorization": "Bearer secret-token", "X-EA-Principal-ID": "caller-1"}),
        container=container,
    )
    assert header_context.principal_id == "caller-1"


def test_loopback_no_auth_preserves_token_auth_principal_contract() -> None:
    _clear_env()
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["EA_ALLOW_LOOPBACK_NO_AUTH"] = "1"
    os.environ["EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER"] = "1"
    container, _ = _container_for_current_settings()

    token_context = get_request_context(
        _request(headers={"Authorization": "Bearer secret-token", "X-EA-Principal-ID": "caller-1"}),
        container=container,
    )
    assert token_context.principal_id == "caller-1"
    assert token_context.auth_source == "api_token"
    assert token_context.authenticated is True

    loopback_context = get_request_context(
        _request(headers={"X-EA-Principal-ID": "caller-2"}),
        container=container,
    )
    assert loopback_context.principal_id == "caller-2"
    assert loopback_context.auth_source == "loopback_no_auth"
    assert loopback_context.authenticated is True


def test_runtime_profile_prod_rejects_authenticated_header_principal_contract() -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["EA_API_TOKEN"] = "secret-token"
    os.environ["EA_SIGNING_SECRET"] = "signing-secret"
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    container, profile = _container_for_current_settings()

    with pytest.raises(HTTPException, match="principal_required"):
        get_request_context(
            _request(headers={"Authorization": "Bearer secret-token"}),
            container=container,
        )

    assert profile.principal_source == "verified_identity"
    with pytest.raises(HTTPException, match="principal_required"):
        get_request_context(
            _request(headers={"Authorization": "Bearer secret-token", "X-EA-Principal-ID": "ops-1"}),
            container=container,
        )

    os.environ["EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER"] = "1"
    container, profile = _container_for_current_settings()
    assert profile.principal_source == "verified_identity"
    with pytest.raises(HTTPException, match="principal_required"):
        get_request_context(
            _request(headers={"Authorization": "Bearer secret-token", "X-EA-Principal-ID": "ops-1"}),
            container=container,
        )


def test_signing_secret_does_not_fallback_to_api_token() -> None:
    _clear_env()
    os.environ["EA_API_TOKEN"] = "secret-token"
    settings = get_settings()
    resolved = resolve_signing_secret(settings, purpose="workspace-access")
    assert resolved != "secret-token:workspace-access"
    assert "secret-token" not in resolved

    os.environ["EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER"] = "1"
    container, _ = _container_for_current_settings()
    header_context = get_request_context(
        _request(headers={"Authorization": "Bearer secret-token", "X-EA-Principal-ID": "ops-1"}),
        container=container,
    )
    assert header_context.principal_id == "ops-1"
    assert header_context.authenticated is True


def test_prod_runtime_profile_allows_cloudflare_access_without_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env()
    os.environ["EA_RUNTIME_MODE"] = "prod"
    os.environ["DATABASE_URL"] = "postgresql://example.invalid/ea"
    os.environ["EA_CF_ACCESS_TEAM_DOMAIN"] = "girschele.cloudflareaccess.com"
    os.environ["EA_CF_ACCESS_AUD"] = "aud-123"
    settings = get_settings()
    profile = resolve_runtime_profile(settings)
    assert profile.auth_mode == "access"

    from app.api import dependencies as deps
    from app.services.cloudflare_access import CloudflareAccessIdentity

    monkeypatch.setattr(
        deps,
        "resolve_access_identity",
        lambda **kwargs: CloudflareAccessIdentity(
            principal_id="cf-email:user@gmail.com",
            email="user@gmail.com",
            subject="subject-123",
            display_name="User Gmail",
            issuer="https://girschele.cloudflareaccess.com",
            idp_name="google",
            audiences=("aud-123",),
            claims={"email": "user@gmail.com", "sub": "subject-123"},
        ),
    )
    container, _ = _container_for_current_settings()
    container.orchestrator = SimpleNamespace(
        fetch_operator_profile=lambda operator_id, principal_id: None,
        upsert_operator_profile=lambda **kwargs: kwargs,
    )

    context = get_request_context(_request(headers={}), container=container)
    assert context.principal_id == "cf-email:user@gmail.com"
    assert context.authenticated is True
    assert context.auth_source == "cloudflare_access"
    assert context.access_email == "user@gmail.com"


def test_resolve_principal_id_rejects_foreign_requested_principal() -> None:
    context = RequestContext(principal_id="exec-1", authenticated=False)
    with pytest.raises(Exception):
        resolve_principal_id("exec-2", context)


def test_provider_registry_exposes_executable_browseract_binding() -> None:
    registry = ProviderRegistryService()
    contract = TaskContract(
        task_key="inventory",
        deliverable_type="inventory",
        default_risk_class="low",
        default_approval_class="none",
        allowed_tools=("browseract.extract_account_inventory",),
        evidence_requirements=(),
        memory_write_policy="none",
        budget_policy_json={"class": "low"},
        updated_at=now_utc_iso(),
    )
    bindings = registry.bindings_for_skill(
        SkillCatalogService(TaskContractService(InMemoryTaskContractRepository())).contract_to_skill(contract)
    )
    assert any(binding.provider_key == "browseract" and binding.executable for binding in bindings)
