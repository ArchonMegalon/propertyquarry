from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from importlib import import_module
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, Query
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.dependencies import (
    get_cloudflare_access_identity,
    get_container,
    get_request_context,
    require_request_auth,
)
from app.api.errors import install_error_handlers
from app.api.ingress import (
    INGRESS_POLICY_CONTRACT_VERSION,
    IngressAbuseMiddleware,
    IngressPolicy,
)
from app.api.ingress_admission import (
    INGRESS_ADMISSION_CONTRACT_VERSION,
    AdmissionBackend,
    AdmissionOperation,
    AdmissionOutcome,
    IngressAdmissionContractError,
    InMemoryIngressAdmissionStore,
    PostgresIngressAdmissionStore,
)
from app.api.principal_identity import PrincipalIdentityMiddleware, PrincipalIdentityPolicy
from app.api.propertyquarry_localization import PropertyQuarryLocalizationMiddleware
from app.api.threadpool_compat import inline_sync_handlers_enabled, install_inline_threadpool_compat
from app.container import build_container
from app.observability import RuntimeMetrics
from app.services.admission_control import resolve_api_admission_database_url
from app.settings import get_settings, validate_startup_settings

_PROPERTY_SEARCH_PREWARM_CONTAINER = None

_PROPERTYQUARRY_RUNTIME_PROFILE = "propertyquarry"
_PROPERTYQUARRY_APP_SECTIONS = frozenset(
    {
        "account",
        "agents",
        "alerts",
        "billing",
        "properties",
        "properties/packets",
        "research",
        "shortlist",
    }
)
_PROPERTYQUARRY_ALLOWED_ROUTE_PATHS = frozenset(
    {
        "/",
        "/app",
        "/app/actions/sign-out",
        "/app/api/access-sessions",
        "/app/api/handoffs",
        "/app/api/offers",
        "/app/api/signals/google/property-sync",
        "/app/api/signals/google/status",
        "/app/api/signals/google/willhaben-sync",
        "/app/api/signals/willhaben/property-tour",
        "/app/example/shortlist",
        "/app/properties/notifications/preview",
        "/app/research-tour-handoff/{token}",
        "/app/search",
        "/cookies",
        "/data-deletion",
        "/directory",
        "/directory/profile/{profile_id}",
        "/disclaimers",
        "/docs",
        "/get-started",
        "/google/callback",
        "/google/connect",
        "/guides/wohnung-kaufen-wien-checkliste",
        "/health",
        "/health/live",
        "/health/ready",
        "/healthz",
        "/how-it-works",
        "/how-it-works/score",
        "/imprint",
        "/integrations",
        "/integrations/{channel_name}",
        "/internal/metrics",
        "/manifest.webmanifest",
        "/markets/vienna",
        "/pricing",
        "/privacy",
        "/privacy/analytics-consent",
        "/product",
        "/pwa-icon-{size}.png",
        "/pwa-icon.svg",
        "/refunds",
        "/register",
        "/robots.txt",
        "/security",
        "/service-worker.js",
        "/sign-in",
        "/sign-in/current-session",
        "/sign-in/email-link",
        "/sign-in/google",
        "/sitemap.xml",
        "/static",
        "/subprocessors",
        "/support",
        "/terms",
        "/user-data-deletion",
        "/v1/onboarding/finalize",
        "/v1/onboarding/google/callback",
        "/v1/onboarding/google/start",
        "/v1/onboarding/start",
        "/v1/onboarding/status",
        "/v1/register/start",
        "/v1/register/verify",
        "/version",
        "/workspace-access/{token}",
        "/workspace-invites/{token}",
        "/workspace-invites/{token}/accept",
    }
)
_PROPERTYQUARRY_ALLOWED_ROUTE_PREFIXES = (
    "/app/actions/access-sessions/",
    "/app/actions/handoffs/",
    "/app/actions/settings/",
    "/app/api/access-sessions/",
    "/app/api/followups/",
    "/app/api/handoffs/",
    "/app/api/offers/",
    "/app/api/people/{person_id}/preference-profile",
    "/app/api/properties/",
    "/app/api/property-",
    "/app/api/property/",
    "/app/api/signals/google/photos/",
    "/app/api/signals/property/",
    "/app/api/settings/",
    "/app/api/stakeholders/",
    "/app/assets/",
    "/app/channel/handoffs/",
    "/app/handoffs/",
    "/app/research/",
    "/app/settings/",
    "/app/shortlist/",
    "/tours/",
    "/v1/integrations/fliplink/",
    "/v1/onboarding/property-search/",
)


def propertyquarry_runtime_profile_enabled() -> bool:
    """Resolve the explicit, fail-closed API surface profile."""

    raw_profile = str(os.environ.get("PROPERTYQUARRY_RUNTIME_PROFILE") or "").strip().lower()
    if not raw_profile:
        return False
    if raw_profile == _PROPERTYQUARRY_RUNTIME_PROFILE:
        return True
    raise RuntimeError(
        "PROPERTYQUARRY_RUNTIME_PROFILE must be unset or 'propertyquarry'"
    )


def _propertyquarry_section_endpoint(  # type: ignore[no-untyped-def]
    *,
    section: str,
    app_shell_endpoint,
):
    def propertyquarry_section(
        request: Request,
        container=Depends(get_container),
        context=Depends(get_request_context),
        access_identity=Depends(get_cloudflare_access_identity),
        run_id: str = Query(default=""),
        candidate: str = Query(default=""),
        agent_id: str = Query(default=""),
        load_agent: str = Query(default=""),
        run_agent: str = Query(default=""),
        packet_missing: str = Query(default=""),
        missing_candidate_ref: str = Query(default=""),
        stale_run: str = Query(default=""),
        missing_run_id: str = Query(default=""),
        full: str = Query(default=""),
    ) -> HTMLResponse:
        return app_shell_endpoint(
            section=section,
            request=request,
            container=container,
            context=context,
            access_identity=access_identity,
            run_id=run_id,
            candidate=candidate,
            agent_id=agent_id,
            load_agent=load_agent,
            run_agent=run_agent,
            packet_missing=packet_missing,
            missing_candidate_ref=missing_candidate_ref,
            stale_run=stale_run,
            missing_run_id=missing_run_id,
            full=full,
        )

    propertyquarry_section.__name__ = (
        f"propertyquarry_app_{section.replace('-', '_').replace('/', '_')}"
    )
    return propertyquarry_section


def _apply_propertyquarry_runtime_profile(  # type: ignore[no-untyped-def]
    app: FastAPI,
    *,
    app_shell_endpoint,
) -> None:
    for section in sorted(_PROPERTYQUARRY_APP_SECTIONS):
        path = f"/app/{section}"
        app.add_api_route(
            path,
            _propertyquarry_section_endpoint(
                section=section,
                app_shell_endpoint=app_shell_endpoint,
            ),
            methods=["GET"],
            response_class=HTMLResponse,
            include_in_schema=False,
        )

    app.router.routes[:] = [
        route
        for route in app.router.routes
        if _propertyquarry_route_path_allowed(str(getattr(route, "path", "")))
    ]
    app.openapi_schema = None
    app.state.propertyquarry_runtime_profile = _PROPERTYQUARRY_RUNTIME_PROFILE


class CachedStaticFiles(StaticFiles):
    def file_response(
        self,
        full_path: str,
        stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code=status_code)
        response.headers.setdefault("Cache-Control", "public, max-age=3600, stale-while-revalidate=86400")
        return response


class PropertyQuarryReleaseIdentityMiddleware:
    """Bind the measured app document to the replica that served it."""

    def __init__(self, app: ASGIApp, *, identity_provider) -> None:  # type: ignore[no-untyped-def]
        self.app = app
        self.identity_provider = identity_provider

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") not in {"GET", "HEAD"}
            or scope.get("path") != "/app/search"
        ):
            await self.app(scope, receive, send)
            return

        identity = self.identity_provider()
        from app.api.routes.health import RELEASE_IDENTITY_RESPONSE_HEADERS

        header_names = {
            header_name.lower().encode("ascii")
            for _field, header_name in RELEASE_IDENTITY_RESPONSE_HEADERS
        }

        async def send_with_release_identity(message: Message) -> None:
            if message.get("type") == "http.response.start":
                existing = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in header_names
                ]
                existing.extend(
                    (
                        header_name.lower().encode("ascii"),
                        identity.get(field, "").encode("ascii"),
                    )
                    for field, header_name in RELEASE_IDENTITY_RESPONSE_HEADERS
                )
                message = {**message, "headers": existing}
            await send(message)

        await self.app(scope, receive, send_with_release_identity)


async def _prewarm_provider_health_cache() -> None:
    try:
        from app.api.routes.responses import prewarm_provider_health_snapshot_cache

        await prewarm_provider_health_snapshot_cache(lightweight=True)
    except Exception:
        return


async def _prewarm_property_search_surface_cache() -> None:
    container = _PROPERTY_SEARCH_PREWARM_CONTAINER
    try:
        from app.api.routes.landing import (
            prewarm_property_search_shell_cache,
            prewarm_property_search_surface_cache,
        )

        await asyncio.to_thread(prewarm_property_search_surface_cache)
        if container is not None:
            await asyncio.to_thread(prewarm_property_search_shell_cache, container=container)
            container.readiness.mark_startup_gate_ready("property_search_shell_prewarm")
    except Exception:
        if container is not None:
            container.readiness.mark_startup_gate_failed("property_search_shell_prewarm")
        return


def _include_public_routes(
    app: FastAPI,
    *,
    settings,
    public_documents_router: APIRouter,
    landing_setup_router: APIRouter,
    landing_actions_router: APIRouter,
    landing_channel_router: APIRouter,
    landing_objects_router: APIRouter,
    landing_workspace_router: APIRouter,
    landing_router: APIRouter,
    fliplink_public_router: APIRouter,
    subscribr_public_router: APIRouter,
    dadan_public_router: APIRouter,
    heyy_public_router: APIRouter,
    payfunnels_public_router: APIRouter,
    product_api_public_router: APIRouter,
    health_router: APIRouter,
    register_router: APIRouter,
) -> None:
    app.include_router(public_documents_router)
    app.include_router(landing_setup_router)
    app.include_router(landing_actions_router)
    app.include_router(landing_channel_router)
    app.include_router(landing_objects_router)
    app.include_router(landing_workspace_router)
    app.include_router(landing_router)
    app.include_router(fliplink_public_router)
    app.include_router(subscribr_public_router)
    app.include_router(dadan_public_router)
    app.include_router(heyy_public_router)
    app.include_router(payfunnels_public_router)
    app.include_router(product_api_public_router)
    if settings.public_results_enabled:
        from app.api.routes.public_results import router as public_results_router

        app.include_router(public_results_router)
    if settings.public_tours_enabled:
        from app.api.routes.public_tours import router as public_tours_router

        app.include_router(public_tours_router)
    if settings.public_memorials_enabled:
        from app.api.routes.public_memorials import router as public_memorials_router

        app.include_router(public_memorials_router)
    app.include_router(health_router)
    app.include_router(register_router)


def _include_authenticated_routes(
    app: FastAPI,
    *,
    auth_dependency: list,
    propertyquarry_profile: bool,
    onboarding_router: APIRouter,
    images_router: APIRouter,
    google_oauth_router: APIRouter,
    channels_router: APIRouter,
    memory_router: APIRouter,
    product_api_delivery_router: APIRouter,
    product_api_workspace_router: APIRouter,
    product_api_router: APIRouter,
    fliplink_authenticated_router: APIRouter,
    property_content_studio_router: APIRouter,
    policy_router: APIRouter,
    providers_router: APIRouter,
    plans_router: APIRouter,
    rewrite_router: APIRouter,
    runtime_router: APIRouter,
) -> None:
    def include_router(router: APIRouter, *, require_auth: bool = True) -> None:
        selected_router = (
            _propertyquarry_router(router)
            if propertyquarry_profile
            else router
        )
        app.include_router(
            selected_router,
            dependencies=auth_dependency if require_auth else None,
        )

    include_router(onboarding_router)
    include_router(images_router)
    include_router(google_oauth_router, require_auth=False)
    include_router(channels_router)
    include_router(memory_router)
    include_router(product_api_delivery_router)
    include_router(product_api_workspace_router)
    include_router(product_api_router)
    include_router(fliplink_authenticated_router)
    include_router(property_content_studio_router)
    include_router(policy_router)
    include_router(providers_router)
    include_router(plans_router)
    include_router(rewrite_router)
    include_router(runtime_router)


def _router_without_paths(router: APIRouter, *, excluded_paths: set[str]) -> APIRouter:
    filtered = APIRouter()
    for route in router.routes:
        if getattr(route, "path", "") in excluded_paths:
            continue
        filtered.routes.append(route)
    return filtered


def _propertyquarry_route_path_allowed(path: str) -> bool:
    allowed_exact = _PROPERTYQUARRY_ALLOWED_ROUTE_PATHS | {
        f"/app/{section}" for section in _PROPERTYQUARRY_APP_SECTIONS
    }
    return path in allowed_exact or any(
        path.startswith(prefix)
        for prefix in _PROPERTYQUARRY_ALLOWED_ROUTE_PREFIXES
    )


def _propertyquarry_router(router: APIRouter) -> APIRouter:
    """Select only standalone product routes before mounting shared routers."""

    filtered = APIRouter()
    for route in router.routes:
        if _propertyquarry_route_path_allowed(str(getattr(route, "path", ""))):
            filtered.routes.append(route)
    return filtered


def _include_legacy_authenticated_routes(
    app: FastAPI,
    *,
    auth_dependency: list,
    human_router: APIRouter,
    evidence_router: APIRouter,
    observations_router: APIRouter,
    delivery_router: APIRouter,
    connectors_router: APIRouter,
    ltd_runtime_router: APIRouter,
    skills_router: APIRouter,
    task_contracts_router: APIRouter,
    tools_router: APIRouter,
    responses_router: APIRouter,
) -> None:
    app.include_router(human_router, dependencies=auth_dependency)
    app.include_router(evidence_router, dependencies=auth_dependency)
    app.include_router(observations_router, dependencies=auth_dependency)
    app.include_router(delivery_router, dependencies=auth_dependency)
    app.include_router(connectors_router, dependencies=auth_dependency)
    app.include_router(ltd_runtime_router, dependencies=auth_dependency)
    app.include_router(skills_router, dependencies=auth_dependency)
    app.include_router(task_contracts_router, dependencies=auth_dependency)
    app.include_router(tools_router, dependencies=auth_dependency)
    app.include_router(responses_router, dependencies=auth_dependency)


@lru_cache(maxsize=1)
def _load_core_route_modules() -> dict[str, object]:
    modules = {
        "fliplink_integration": import_module("app.api.routes.fliplink_integration"),
        "admin_property_content_studio": import_module("app.api.routes.admin_property_content_studio"),
        "dadan_integration": import_module("app.api.routes.dadan_integration"),
        "heyy_integration": import_module("app.api.routes.heyy_integration"),
        "google_oauth": import_module("app.api.routes.google_oauth"),
        "health": import_module("app.api.routes.health"),
        "images": import_module("app.api.routes.images"),
        "landing_actions": import_module("app.api.routes.landing_actions"),
        "landing_channel": import_module("app.api.routes.landing_channel"),
        "public_documents": import_module("app.api.routes.public_documents"),
        "landing": import_module("app.api.routes.landing"),
        "landing_objects": import_module("app.api.routes.landing_objects"),
        "landing_setup": import_module("app.api.routes.landing_setup"),
        "landing_workspace": import_module("app.api.routes.landing_workspace"),
        "memory": import_module("app.api.routes.memory"),
        "observations": import_module("app.api.routes.observations"),
        "onboarding": import_module("app.api.routes.onboarding"),
        "plans": import_module("app.api.routes.plans"),
        "policy": import_module("app.api.routes.policy"),
        "providers": import_module("app.api.routes.providers"),
        "product_api": import_module("app.api.routes.product_api"),
        "product_api_delivery": import_module("app.api.routes.product_api_delivery"),
        "product_api_workspace": import_module("app.api.routes.product_api_workspace"),
        "rewrite": import_module("app.api.routes.rewrite"),
        "runtime": import_module("app.api.routes.runtime"),
    }
    return modules


@lru_cache(maxsize=1)
def _load_legacy_route_modules() -> dict[str, object]:
    modules = {
        "connectors": import_module("app.api.routes.connectors"),
        "delivery": import_module("app.api.routes.delivery"),
        "evidence": import_module("app.api.routes.evidence"),
        "human": import_module("app.api.routes.human"),
        "ltd_runtime": import_module("app.api.routes.ltd_runtime"),
        "responses": import_module("app.api.routes.responses"),
        "skills": import_module("app.api.routes.skills"),
        "task_contracts": import_module("app.api.routes.task_contracts"),
        "tools": import_module("app.api.routes.tools"),
    }
    return modules


def preload_non_channel_route_modules(*, include_legacy: bool = False) -> None:
    _load_core_route_modules()
    if include_legacy:
        _load_legacy_route_modules()


def _ingress_admission_hmac_material() -> tuple[bytes, str]:
    from app.product.property_search_storage import (
        _property_search_ingress_admission_hmac_material,
    )

    return _property_search_ingress_admission_hmac_material()


def _build_ingress_admission_store(  # type: ignore[no-untyped-def]
    *,
    settings,
    container,
    policy: IngressPolicy,
    metrics: RuntimeMetrics,
):
    if not policy.quotas_enabled:
        return None
    if (
        INGRESS_POLICY_CONTRACT_VERSION
        != INGRESS_ADMISSION_CONTRACT_VERSION
    ):
        raise RuntimeError("ingress_admission_policy_contract_mismatch")
    runtime_profile = getattr(container, "runtime_profile", None)
    backend = str(
        getattr(runtime_profile, "storage_backend", "")
        or getattr(settings, "storage_backend", "")
        or ""
    ).strip().lower()
    hmac_secret, erasure_key_id = _ingress_admission_hmac_material()
    if backend == "postgres":
        primary_database_url = str(
            getattr(getattr(container, "settings", None), "database_url", "")
            or getattr(settings, "database_url", "")
            or ""
        ).strip()
        runtime_mode = str(os.environ.get("EA_RUNTIME_MODE") or "").strip()
        database_url = (
            resolve_api_admission_database_url(
                runtime_mode=runtime_mode,
                primary_database_url=primary_database_url,
            )
            if runtime_mode.lower() in {"prod", "production"}
            else primary_database_url
        )
        store = PostgresIngressAdmissionStore(
            database_url,
            hmac_secret=hmac_secret,
            erasure_key_id=erasure_key_id,
            verify_schema=True,
        )
    elif backend == "memory":
        store = InMemoryIngressAdmissionStore(
            hmac_secret=hmac_secret,
        )
    else:
        raise RuntimeError("ingress admission backend is not resolved")
    snapshot = store.capacity_snapshot()
    metrics.record_ingress_admission_operation(
        backend=snapshot.backend.value,
        operation=AdmissionOperation.SNAPSHOT.value,
        outcome=(
            AdmissionOutcome.ALLOWED.value
            if snapshot.contract_valid
            else AdmissionOutcome.BACKEND_UNAVAILABLE.value
        ),
    )
    metrics.record_ingress_admission_capacity(
        backend=snapshot.backend.value,
        contract_valid=snapshot.contract_valid,
        rows={
            row.capacity_key.value: (row.row_count, row.hard_limit)
            for row in snapshot.rows
        },
    )
    if not snapshot.contract_valid:
        raise IngressAdmissionContractError(
            snapshot.reason,
            backend=AdmissionBackend(backend),
            operation=AdmissionOperation.SNAPSHOT,
        )
    return store


def create_app() -> FastAPI:
    propertyquarry_profile = propertyquarry_runtime_profile_enabled()
    s = get_settings()
    validate_startup_settings(s)
    if inline_sync_handlers_enabled():
        install_inline_threadpool_compat()
    from app.api.routes.channels import router as channels_router
    from app.api.routes.metrics import router as metrics_router

    route_modules = _load_core_route_modules()
    fliplink_authenticated_router = route_modules["fliplink_integration"].authenticated_router
    fliplink_public_router = route_modules["fliplink_integration"].public_router
    property_content_studio_router = route_modules["admin_property_content_studio"].authenticated_router
    subscribr_public_router = route_modules["admin_property_content_studio"].public_router
    dadan_public_router = route_modules["dadan_integration"].router
    heyy_public_router = route_modules["heyy_integration"].router
    google_oauth_router = route_modules["google_oauth"].router
    health_router = route_modules["health"].router
    images_router = route_modules["images"].router
    landing_actions_router = route_modules["landing_actions"].router
    landing_channel_router = route_modules["landing_channel"].router
    public_documents_router = route_modules["public_documents"].router
    landing_router = route_modules["landing"].router
    landing_objects_router = route_modules["landing_objects"].router
    landing_setup_router = route_modules["landing_setup"].router
    landing_workspace_router = route_modules["landing_workspace"].router
    observations_router = route_modules["observations"].router
    memory_router = route_modules["memory"].router
    register_router = route_modules["onboarding"].register_router
    onboarding_router = route_modules["onboarding"].router
    plans_router = route_modules["plans"].router
    policy_router = route_modules["policy"].router
    providers_router = route_modules["providers"].router
    product_api_router = route_modules["product_api"].router
    product_api_public_router = route_modules["product_api"].public_router
    product_api_delivery_router = route_modules["product_api_delivery"].router
    payfunnels_public_router = route_modules["product_api_delivery"].public_payfunnels_router
    product_api_workspace_router = route_modules["product_api_workspace"].router
    rewrite_router = route_modules["rewrite"].router
    runtime_router = route_modules["runtime"].router
    app = FastAPI(title=s.app_name, version=s.app_version, docs_url="/api/docs", redoc_url="/api/redoc")
    app.state.runtime_metrics = RuntimeMetrics()
    app.state.container = build_container(settings=s)
    static_root = Path(__file__).resolve().parents[1] / "static"
    if static_root.exists():
        app.mount("/static", CachedStaticFiles(directory=static_root), name="static")
    app.add_middleware(
        PropertyQuarryReleaseIdentityMiddleware,
        identity_provider=route_modules["health"].release_runtime_identity,
    )
    app.add_middleware(PropertyQuarryLocalizationMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    ingress_policy = IngressPolicy.from_environ(runtime_mode=s.runtime.mode)
    app.state.ingress_admission_store = _build_ingress_admission_store(
        settings=s,
        container=app.state.container,
        policy=ingress_policy,
        metrics=app.state.runtime_metrics,
    )
    app.add_middleware(
        IngressAbuseMiddleware,
        policy=ingress_policy,
        admission_store=app.state.ingress_admission_store,
    )
    app.add_middleware(
        PrincipalIdentityMiddleware,
        policy=PrincipalIdentityPolicy.from_environ(
            runtime_mode=s.runtime.mode,
            trusted_proxy_cidrs=ingress_policy.trusted_proxy_cidrs,
        ),
    )
    install_error_handlers(app)
    is_memory_backend = str(s.storage_backend or "").strip().lower() == "memory"
    if is_memory_backend:
        app.router.on_startup.append(_prewarm_property_search_surface_cache)
    else:
        app.state.container.readiness.register_startup_gate("property_search_shell_prewarm")
        global _PROPERTY_SEARCH_PREWARM_CONTAINER
        _PROPERTY_SEARCH_PREWARM_CONTAINER = app.state.container
        app.router.on_startup.append(_prewarm_property_search_surface_cache)
    if s.legacy_runtime_surfaces_enabled and not propertyquarry_profile:
        app.router.on_startup.append(_prewarm_provider_health_cache)
    _include_public_routes(
        app,
        settings=s,
        public_documents_router=public_documents_router,
        landing_setup_router=landing_setup_router,
        landing_actions_router=landing_actions_router,
        landing_channel_router=landing_channel_router,
        landing_objects_router=landing_objects_router,
        landing_workspace_router=landing_workspace_router,
        landing_router=landing_router,
        fliplink_public_router=fliplink_public_router,
        subscribr_public_router=subscribr_public_router,
        dadan_public_router=dadan_public_router,
        heyy_public_router=heyy_public_router,
        payfunnels_public_router=payfunnels_public_router,
        product_api_public_router=product_api_public_router,
        health_router=health_router,
        register_router=register_router,
    )
    app.include_router(metrics_router)
    auth_dependency = [Depends(require_request_auth)]
    _include_authenticated_routes(
        app,
        auth_dependency=auth_dependency,
        propertyquarry_profile=propertyquarry_profile,
        onboarding_router=onboarding_router,
        images_router=images_router,
        google_oauth_router=google_oauth_router,
        channels_router=(
            channels_router
            if s.legacy_runtime_surfaces_enabled
            else _router_without_paths(
                channels_router,
                excluded_paths={
                    "/v1/channels/telegram/ingest",
                    "/v1/channels/telegram/ingest/{bot_key}",
                },
            )
        ),
        memory_router=memory_router,
        product_api_delivery_router=_router_without_paths(
            product_api_delivery_router,
            excluded_paths={"/app/api/signals/property/billing/payfunnels/webhook"},
        ),
        product_api_workspace_router=product_api_workspace_router,
        product_api_router=product_api_router,
        fliplink_authenticated_router=fliplink_authenticated_router,
        property_content_studio_router=property_content_studio_router,
        policy_router=policy_router,
        providers_router=(
            providers_router
            if s.legacy_runtime_surfaces_enabled
            else _router_without_paths(providers_router, excluded_paths={"/v1/providers/registry"})
        ),
        plans_router=plans_router,
        rewrite_router=rewrite_router,
        runtime_router=runtime_router,
    )
    if s.legacy_runtime_surfaces_enabled and not propertyquarry_profile:
        legacy_route_modules = _load_legacy_route_modules()
        connectors_router = legacy_route_modules["connectors"].router
        delivery_router = legacy_route_modules["delivery"].router
        evidence_router = legacy_route_modules["evidence"].router
        human_router = legacy_route_modules["human"].router
        ltd_runtime_router = legacy_route_modules["ltd_runtime"].router
        responses_router = legacy_route_modules["responses"].router
        skills_router = legacy_route_modules["skills"].router
        task_contracts_router = legacy_route_modules["task_contracts"].router
        tools_router = legacy_route_modules["tools"].router

        _include_legacy_authenticated_routes(
            app,
            auth_dependency=auth_dependency,
            human_router=human_router,
            evidence_router=evidence_router,
            observations_router=observations_router,
            delivery_router=delivery_router,
            connectors_router=connectors_router,
            ltd_runtime_router=ltd_runtime_router,
            skills_router=skills_router,
            task_contracts_router=task_contracts_router,
            tools_router=tools_router,
            responses_router=responses_router,
        )
    if propertyquarry_profile:
        _apply_propertyquarry_runtime_profile(
            app,
            app_shell_endpoint=route_modules["landing"].app_shell,
        )
    return app
