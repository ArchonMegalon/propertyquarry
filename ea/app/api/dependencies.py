from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request
from fastapi.params import Depends as DependsMarker

from app.container import AppContainer
from app.api.principal_identity import (
    VERIFIED_PRINCIPAL_ASSERTION_STATE_KEY,
    VerifiedPrincipalAssertion,
)
from app.product.workspace_access_storage import (
    get_workspace_access_session_record,
    update_workspace_access_session_record,
)
from app.services.cloudflare_access import (
    CloudflareAccessIdentity,
    build_operator_id,
    build_operator_notes,
    resolve_access_identity,
)
from app.services import propertyquarry_google_identity
from app.settings import RuntimeProfile, resolve_runtime_profile, resolve_signing_secret


_LOG = logging.getLogger(__name__)


def get_container(request: Request) -> AppContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise RuntimeError("application container is not initialized")
    return container


def _extract_token(request: Request) -> str:
    ea_api_token = str(request.headers.get("x-ea-api-token") or "").strip()
    if ea_api_token:
        return ea_api_token
    api_token = str(request.headers.get("x-api-token") or "").strip()
    if api_token:
        return api_token
    header = str(request.headers.get("authorization") or "").strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _request_supplies_auth_material(request: Request) -> bool:
    return bool(
        str(request.headers.get("authorization") or "").strip()
        or str(request.headers.get("x-ea-api-token") or "").strip()
        or str(request.headers.get("x-api-token") or "").strip()
        or str(request.headers.get("x-ea-principal-id") or "").strip()
        or str(request.headers.get("x-principal-id") or "").strip()
        or str(request.headers.get("x-ea-operator-id") or "").strip()
        or _extract_propertyquarry_identity_token(request)
        or _extract_workspace_session_token(request)
    )


def _telegram_webhook_secret_candidates(*, bot_key: str = "") -> tuple[str, ...]:
    candidates: list[str] = []
    normalized_bot_key = str(bot_key or "").strip()
    raw_registry = str(os.environ.get("EA_TELEGRAM_BOT_REGISTRY_JSON") or "").strip()
    if raw_registry:
        try:
            parsed = json.loads(raw_registry)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            for raw_key, raw_value in parsed.items():
                if normalized_bot_key and str(raw_key or "").strip() != normalized_bot_key:
                    continue
                if not isinstance(raw_value, dict):
                    continue
                secret = str(raw_value.get("secret") or "").strip()
                if secret:
                    candidates.append(secret)
    if not normalized_bot_key or normalized_bot_key == "default":
        fallback = str(os.environ.get("EA_TELEGRAM_INGEST_SECRET") or "").strip()
        if fallback:
            candidates.append(fallback)
    return tuple(dict.fromkeys(candidates))


def _telegram_webhook_request_authenticated(request: Request) -> bool:
    if request.method.upper() != "POST":
        return False
    path = str(request.url.path or "").strip()
    prefix = "/v1/channels/telegram/ingest"
    if path != prefix and not path.startswith(f"{prefix}/"):
        return False
    provided = str(request.headers.get("x-telegram-bot-api-secret-token") or "").strip()
    if not provided:
        return False
    bot_key = ""
    if path.startswith(f"{prefix}/"):
        bot_key = path.removeprefix(f"{prefix}/").strip("/")
        if "/" in bot_key:
            return False
    for expected in _telegram_webhook_secret_candidates(bot_key=bot_key):
        if hmac.compare_digest(provided, expected):
            return True
    return False


def _log_auth_failure(
    request: Request,
    *,
    detail: str,
    profile: RuntimeProfile,
    expected_token_configured: bool,
) -> None:
    client_host = ""
    client_port = ""
    if request.client is not None:
        client_host = str(getattr(request.client, "host", "") or "")
        client_port = str(getattr(request.client, "port", "") or "")
    authorization = str(request.headers.get("authorization") or "")
    x_ea_api_token = str(request.headers.get("x-ea-api-token") or "")
    x_api_token = str(request.headers.get("x-api-token") or "")
    principal_header = str(
        request.headers.get("x-ea-principal-id")
        or request.headers.get("x-principal-id")
        or request.headers.get("x-ea-operator-id")
        or ""
    ).strip()
    user_agent = str(request.headers.get("user-agent") or "").strip()
    _LOG.warning(
        "ea_auth_failure detail=%s method=%s path=%s client_host=%s client_port=%s auth_mode=%s has_bearer=%s has_x_ea_api_token=%s has_x_api_token=%s has_principal=%s expected_token_configured=%s user_agent=%r",
        detail,
        request.method,
        str(request.url.path or ""),
        client_host,
        client_port,
        str(profile.auth_mode or ""),
        bool(authorization.strip().lower().startswith("bearer ")),
        bool(x_ea_api_token.strip()),
        bool(x_api_token.strip()),
        bool(principal_header),
        bool(expected_token_configured),
        user_agent[:160],
    )


def _configured_api_token(container: AppContainer) -> str:
    return str(container.settings.auth.api_token or "").strip()


def _workspace_access_secret(container: AppContainer) -> str:
    return resolve_signing_secret(container.settings, purpose="workspace-access")


def _extract_workspace_session_token(request: Request) -> str:
    return (
        str(request.headers.get("x-ea-workspace-session") or "").strip()
        or str(request.cookies.get("ea_workspace_session") or "").strip()
    )


def _verify_signed_payload(*, secret: str, token: str) -> dict[str, object] | None:
    normalized = str(token or "").strip()
    if not normalized or "." not in normalized:
        return None
    payload_b64, signature = normalized.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    padding = "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(f"{payload_b64}{padding}".encode("ascii"))
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    expires_raw = str(payload.get("expires_at") or "").strip()
    if expires_raw:
        try:
            expires_at = datetime.fromisoformat(expires_raw)
        except ValueError:
            return None
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return None
    return payload


def _workspace_session_payload(request: Request, container: AppContainer) -> dict[str, object] | None:
    cached = getattr(request.state, "workspace_access_session_payload", None)
    if isinstance(cached, dict):
        return cached
    if cached is False:
        return None
    token = _extract_workspace_session_token(request)
    if not token:
        setattr(request.state, "workspace_access_session_payload", False)
        return None
    payload = _verify_signed_payload(secret=_workspace_access_secret(container), token=token)
    if payload is None or not hmac.compare_digest(
        str(payload.get("token_kind") or "").strip(),
        "workspace_access_session",
    ):
        setattr(request.state, "workspace_access_session_payload", False)
        return None
    principal_id = str(payload.get("principal_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if not principal_id or not session_id:
        setattr(request.state, "workspace_access_session_payload", False)
        return None
    database_url = str(getattr(container.settings, "database_url", "") or "").strip()
    stored_session = get_workspace_access_session_record(
        principal_id=principal_id,
        session_id=session_id,
        database_url=database_url,
    )
    if stored_session:
        if str(stored_session.get("status") or "").strip().lower() == "revoked":
            setattr(request.state, "workspace_access_session_payload", False)
            return None
        expires_raw = str(stored_session.get("expires_at") or "").strip()
        if expires_raw:
            try:
                expires_at = datetime.fromisoformat(expires_raw)
            except ValueError:
                setattr(request.state, "workspace_access_session_payload", False)
                return None
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                setattr(request.state, "workspace_access_session_payload", False)
                return None
        try:
            update_workspace_access_session_record(
                principal_id=principal_id,
                session_id=session_id,
                updates={"last_seen_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()},
                database_url=database_url,
            )
        except Exception:
            pass
        setattr(request.state, "workspace_access_session_payload", payload)
        return payload
    rows = list(container.channel_runtime.list_recent_observations(limit=1000, principal_id=principal_id))
    rows.sort(key=lambda row: (str(row.created_at or ""), str(row.observation_id or "")))
    revoked = False
    for row in rows:
        event_type = str(row.event_type or "").strip().lower()
        payload_row = dict(row.payload or {})
        current_session_id = str(payload_row.get("session_id") or row.source_id or "").strip()
        if current_session_id != session_id:
            continue
        if event_type == "workspace_access_session_revoked":
            revoked = True
        elif event_type == "workspace_access_session_issued":
            revoked = False
    if revoked:
        setattr(request.state, "workspace_access_session_payload", False)
        return None
    setattr(request.state, "workspace_access_session_payload", payload)
    return payload


def _extract_propertyquarry_identity_token(request: Request) -> str:
    return str(
        request.cookies.get(propertyquarry_google_identity.GOOGLE_IDENTITY_COOKIE_NAME) or ""
    ).strip()


def _propertyquarry_identity_session_payload(
    request: Request,
    container: AppContainer,
) -> dict[str, object] | None:
    cached = getattr(request.state, "propertyquarry_identity_session_payload", None)
    if isinstance(cached, dict):
        return cached
    if cached is False:
        return None
    if not propertyquarry_google_identity.propertyquarry_identity_host_allowed(request.url.hostname):
        setattr(request.state, "propertyquarry_identity_session_payload", False)
        return None
    token = _extract_propertyquarry_identity_token(request)
    if not token:
        setattr(request.state, "propertyquarry_identity_session_payload", False)
        return None
    database_url = str(getattr(container.settings, "database_url", "") or "").strip()
    try:
        payload = propertyquarry_google_identity.resolve_propertyquarry_identity_session(
            token=token,
            database_url=database_url,
        )
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        setattr(request.state, "propertyquarry_identity_session_payload", False)
        return None
    setattr(request.state, "propertyquarry_identity_session_payload", payload)
    return payload


def _requested_operator_id(request: Request) -> str:
    return str(request.headers.get("x-ea-operator-id") or "").strip()


def _client_host(request: Request) -> str:
    client = getattr(request, "client", None)
    return str(getattr(client, "host", "") or "").strip()


def _is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _loopback_no_auth_allowed(request: Request, container: AppContainer) -> bool:
    if not bool(getattr(container.settings.auth, "allow_loopback_no_auth", False)):
        return False
    return _is_loopback_host(_client_host(request))


def _provision_access_identity(container: AppContainer, identity: CloudflareAccessIdentity) -> None:
    operator_id = build_operator_id(identity)
    current = container.orchestrator.fetch_operator_profile(operator_id, principal_id=identity.principal_id)
    notes = build_operator_notes(identity)
    if (
        current is not None
        and current.display_name == identity.display_name
        and current.status == "active"
        and current.notes == notes
    ):
        return
    container.orchestrator.upsert_operator_profile(
        principal_id=identity.principal_id,
        operator_id=operator_id,
        display_name=identity.display_name,
        roles=("cloudflare_access",),
        trust_tier="standard",
        status="active",
        notes=notes,
    )


def get_cloudflare_access_identity(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> CloudflareAccessIdentity | None:
    cached = getattr(request.state, "cloudflare_access_identity", None)
    if isinstance(cached, CloudflareAccessIdentity):
        return cached
    if cached is False:
        return None
    try:
        identity = resolve_access_identity(headers=request.headers, settings=container.settings.auth)
    except Exception as exc:
        setattr(request.state, "cloudflare_access_error", str(exc))
        raise HTTPException(status_code=401, detail="cloudflare_access_invalid") from exc
    if identity is None:
        setattr(request.state, "cloudflare_access_identity", False)
        return None
    _provision_access_identity(container, identity)
    setattr(request.state, "cloudflare_access_identity", identity)
    return identity


def _runtime_profile(container: AppContainer):
    profile = getattr(container, "runtime_profile", None)
    if profile is not None:
        return profile
    settings = container.settings
    if hasattr(settings, "storage"):
        return resolve_runtime_profile(settings)
    mode = str(getattr(getattr(settings, "runtime", None), "mode", "dev") or "dev").strip().lower() or "dev"
    api_token = str(getattr(getattr(settings, "auth", None), "api_token", "") or "").strip()
    auth_mode = "token" if mode == "prod" or api_token else "anonymous_dev"
    principal_source = "verified_identity" if mode == "prod" else (
        "authenticated_header_or_default" if auth_mode == "token" else "caller_header_or_default"
    )
    return RuntimeProfile(
        mode=mode,
        storage_backend="postgres" if mode == "prod" else "memory",
        durability="durable" if mode == "prod" else "ephemeral",
        auth_mode=auth_mode,
        principal_source=principal_source,
        database_required=mode == "prod",
        database_configured=False,
        source_backend="memory",
    )


def _resolved_principal_id(
    request: Request,
    *,
    container: AppContainer,
    authenticated: bool,
    access_identity: CloudflareAccessIdentity | None = None,
) -> str:
    profile = _runtime_profile(container)
    if access_identity is not None:
        return access_identity.principal_id
    if str(profile.mode or "").strip().lower() == "prod":
        # Production caller-controlled headers are never an identity source.
        # Verified Access/session/edge assertions are handled before this path.
        return ""
    principal_id = str(request.headers.get("x-ea-principal-id") or "").strip()
    fallback_principal = str(container.settings.auth.default_principal_id or "").strip()
    if principal_id:
        if profile.caller_principal_header_requires_authentication and not authenticated:
            return ""
        if _loopback_no_auth_allowed(request, container):
            return principal_id
        if authenticated and not authenticated_principal_override_allowed():
            principal_id = ""
        else:
            return principal_id
    if fallback_principal and authenticated and profile.default_principal_fallback_allowed:
        return fallback_principal
    if profile.default_principal_fallback_allowed:
        return fallback_principal or "local-user"
    return ""


def require_request_auth(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> None:
    if _telegram_webhook_request_authenticated(request):
        return None
    get_request_context(request, container, access_identity)
    return None


@dataclass(frozen=True)
class RequestContext:
    principal_id: str
    authenticated: bool
    auth_source: str = "anonymous"
    access_email: str = ""
    operator_id: str = ""


def _operator_principal_allowlist() -> set[str]:
    values: set[str] = set()
    for env_name in ("EA_OPERATOR_PRINCIPAL_IDS", "EA_OPERATOR_PRINCIPALS"):
        raw = str(os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            normalized = str(item or "").strip()
            if normalized:
                values.add(normalized)
    return values


def _operator_email_allowlist() -> set[str]:
    values: set[str] = set()
    for env_name in ("EA_OPERATOR_EMAILS", "EA_OPERATOR_ACCESS_EMAILS"):
        raw = str(os.environ.get(env_name) or "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            normalized = str(item or "").strip().lower()
            if normalized:
                values.add(normalized)
    return values


def authenticated_principal_override_allowed() -> bool:
    for env_name in (
        "EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER",
        "EA_ALLOW_AUTHENTICATED_PRINCIPAL_HEADER",
        "EA_TRUST_API_TOKEN_PRINCIPAL_HEADER",
    ):
        if str(os.environ.get(env_name) or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def browser_principal_override_allowed() -> bool:
    for env_name in (
        "EA_TRUST_BROWSER_PRINCIPAL_OVERRIDE",
        "EA_ALLOW_BROWSER_PRINCIPAL_OVERRIDE",
    ):
        if str(os.environ.get(env_name) or "").strip().lower() in {"1", "true", "yes", "on"}:
            return True
    return False


def is_operator_context(context: RequestContext) -> bool:
    principal_id = str(context.principal_id or "").strip()
    if not principal_id:
        return False
    if context.auth_source == "workspace_access_session":
        return bool(str(context.operator_id or "").strip())
    if context.auth_source == "loopback_no_auth":
        return True
    if not bool(context.authenticated):
        return False
    if principal_id in _operator_principal_allowlist():
        return True
    access_email = str(context.access_email or "").strip().lower()
    if access_email and access_email in _operator_email_allowlist():
        return True
    if context.auth_source != "cloudflare_access":
        return False
    lowered = principal_id.lower()
    return lowered.startswith(("system", "operator", "admin", "automation", "scheduler", "cron", "daemon", "health"))


def get_request_context(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RequestContext:
    cached_context = getattr(request.state, "ea_request_context", None)
    if isinstance(cached_context, RequestContext):
        return cached_context
    if isinstance(access_identity, DependsMarker):
        access_identity = get_cloudflare_access_identity(request, container)
    profile = _runtime_profile(container)
    if access_identity is not None:
        principal_id = _resolved_principal_id(
            request,
            container=container,
            authenticated=True,
            access_identity=access_identity,
        )
        context = RequestContext(
            principal_id=principal_id,
            authenticated=True,
            auth_source="cloudflare_access",
            access_email=access_identity.email,
            operator_id=build_operator_id(access_identity),
        )
        setattr(request.state, "ea_request_context", context)
        return context
    propertyquarry_identity_session = _propertyquarry_identity_session_payload(request, container)
    if propertyquarry_identity_session is not None:
        principal_id = str(propertyquarry_identity_session.get("principal_id") or "").strip()
        if not principal_id:
            _log_auth_failure(
                request,
                detail="principal_required",
                profile=profile,
                expected_token_configured=bool(_configured_api_token(container)),
            )
            raise HTTPException(status_code=401, detail="principal_required")
        context = RequestContext(
            principal_id=principal_id,
            authenticated=True,
            auth_source="propertyquarry_google_identity",
            access_email=str(propertyquarry_identity_session.get("email") or "").strip().lower(),
            operator_id="",
        )
        setattr(request.state, "ea_request_context", context)
        return context
    workspace_session = _workspace_session_payload(request, container)
    if workspace_session is not None:
        principal_id = str(workspace_session.get("principal_id") or "").strip()
        if not principal_id:
            _log_auth_failure(request, detail="principal_required", profile=profile, expected_token_configured=bool(_configured_api_token(container)))
            raise HTTPException(status_code=401, detail="principal_required")
        role = str(workspace_session.get("role") or "principal").strip().lower() or "principal"
        operator_id = str(workspace_session.get("operator_id") or "").strip() if role == "operator" else ""
        context = RequestContext(
            principal_id=principal_id,
            authenticated=True,
            auth_source="workspace_access_session",
            access_email=str(workspace_session.get("email") or "").strip().lower(),
            operator_id=operator_id,
        )
        setattr(request.state, "ea_request_context", context)
        return context
    edge_assertion = getattr(request.state, VERIFIED_PRINCIPAL_ASSERTION_STATE_KEY, None)
    if isinstance(edge_assertion, VerifiedPrincipalAssertion):
        context = RequestContext(
            principal_id=edge_assertion.principal_id,
            authenticated=True,
            auth_source=edge_assertion.auth_source,
        )
        setattr(request.state, "ea_request_context", context)
        return context
    loopback_no_auth_allowed = _loopback_no_auth_allowed(request, container)
    token_authenticated_on_loopback = False
    if loopback_no_auth_allowed:
        expected = _configured_api_token(container)
        token_authenticated_on_loopback = bool(expected and hmac.compare_digest(_extract_token(request), expected))
    if loopback_no_auth_allowed and not token_authenticated_on_loopback:
        principal_id = _resolved_principal_id(request, container=container, authenticated=True)
        if not principal_id:
            _log_auth_failure(request, detail="principal_required", profile=profile, expected_token_configured=bool(_configured_api_token(container)))
            raise HTTPException(status_code=401, detail="principal_required")
        context = RequestContext(
            principal_id=principal_id,
            authenticated=True,
            auth_source="loopback_no_auth",
            operator_id=_requested_operator_id(request),
        )
        setattr(request.state, "ea_request_context", context)
        return context
    authenticated = False
    if profile.auth_mode in {"token", "token_or_access"}:
        expected = _configured_api_token(container)
        if not expected:
            _log_auth_failure(request, detail="auth_required", profile=profile, expected_token_configured=False)
            raise HTTPException(status_code=401, detail="auth_required")
        provided = _extract_token(request)
        if not hmac.compare_digest(provided, expected):
            _log_auth_failure(request, detail="auth_required", profile=profile, expected_token_configured=True)
            raise HTTPException(status_code=401, detail="auth_required")
        authenticated = True

    elif profile.auth_mode == "access":
        if not profile.default_principal_fallback_allowed:
            _log_auth_failure(request, detail="auth_required", profile=profile, expected_token_configured=False)
            raise HTTPException(status_code=401, detail="auth_required")

    principal_id = _resolved_principal_id(request, container=container, authenticated=authenticated)
    if not principal_id:
        _log_auth_failure(request, detail="principal_required", profile=profile, expected_token_configured=bool(_configured_api_token(container)))
        raise HTTPException(status_code=401, detail="principal_required")
    context = RequestContext(
        principal_id=principal_id,
        authenticated=authenticated,
        auth_source="api_token" if authenticated else "anonymous",
        operator_id=(
            _requested_operator_id(request)
            if authenticated and str(profile.mode or "").strip().lower() != "prod"
            else ""
        ),
    )
    setattr(request.state, "ea_request_context", context)
    return context


def get_request_context_if_available(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RequestContext:
    cached_context = getattr(request.state, "ea_request_context", None)
    if isinstance(cached_context, RequestContext):
        return cached_context
    if isinstance(access_identity, DependsMarker):
        access_identity = get_cloudflare_access_identity(request, container)
    edge_assertion = getattr(request.state, VERIFIED_PRINCIPAL_ASSERTION_STATE_KEY, None)
    profile = _runtime_profile(container)
    if (
        profile.auth_mode in {"token", "token_or_access", "access"}
        and access_identity is None
        and not isinstance(edge_assertion, VerifiedPrincipalAssertion)
        and not _loopback_no_auth_allowed(request, container)
        and not _request_supplies_auth_material(request)
    ):
        context = RequestContext(principal_id="", authenticated=False, auth_source="anonymous")
        setattr(request.state, "ea_request_context", context)
        return context
    try:
        return get_request_context(request=request, container=container, access_identity=access_identity)
    except HTTPException as exc:
        if int(exc.status_code or 0) != 401:
            raise
        context = RequestContext(principal_id="", authenticated=False, auth_source="anonymous")
        setattr(request.state, "ea_request_context", context)
        return context


def require_runtime_metrics_auth(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> None:
    """Metrics require system-token auth or an allowlisted Access operator."""

    expected = _configured_api_token(container)
    provided = _extract_token(request)
    if expected and hmac.compare_digest(provided, expected):
        # A shared system token can read process metrics without acquiring a
        # tenant identity or consulting any caller-controlled principal header.
        return None
    try:
        context = get_request_context(
            request=request,
            container=container,
            access_identity=access_identity,
        )
    except HTTPException as exc:
        if int(exc.status_code or 0) == 401:
            raise HTTPException(status_code=401, detail="metrics_auth_required") from exc
        raise
    if context.auth_source == "cloudflare_access" and is_operator_context(context):
        return None
    if not context.authenticated:
        raise HTTPException(status_code=401, detail="metrics_auth_required")
    raise HTTPException(status_code=403, detail="metrics_operator_scope_required")


def require_operator_context(context: RequestContext = Depends(get_request_context)) -> None:
    if not is_operator_context(context):
        raise HTTPException(status_code=403, detail="operator_scope_required")


def resolve_principal_id(requested_principal_id: str | None, context: RequestContext) -> str:
    requested = str(requested_principal_id or "").strip()
    if requested and requested != context.principal_id:
        raise HTTPException(status_code=403, detail="principal_scope_mismatch")
    return context.principal_id
