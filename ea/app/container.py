from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from app.repositories.onemin_manager import build_onemin_manager_service_repo
from app.repositories.provider_bindings import build_provider_binding_service_repo
from app.services.brain_router import BrainRouterService
from app.services.channel_runtime import ChannelRuntimeService, build_channel_runtime
from app.services.cognitive_load import CognitiveLoadService
from app.services.evidence_runtime import EvidenceRuntimeService, build_evidence_runtime
from app.services.memory_runtime import MemoryRuntimeService, build_memory_runtime
from app.services.onboarding import OnboardingService, build_onboarding_service
from app.services.onemin_manager import OneminManagerService, register_onemin_manager
from app.services.orchestrator import (
    RewriteOrchestrator,
    build_artifact_repo,
    build_default_orchestrator,
)
from app.services.planner import PlannerService
from app.services.policy import PolicyDecisionService
from app.services.preference_profile_service import (
    PreferenceProfileService,
    build_preference_profile_service,
)
from app.services.proactive_horizon import ProactiveHorizonService
from app.services.provider_registry import ProviderRegistryService
from app.services.admission_control import (
    AdmissionBackendUnavailable,
    probe_admission_cursor,
    resolve_api_admission_database_url,
)
from app.services.skills import SkillCatalogService
from app.services.task_contracts import TaskContractService, build_task_contract_service
from app.services.tool_execution import ToolExecutionService
from app.services.tool_runtime import ToolRuntimeService, build_tool_runtime
from app.settings import (
    RuntimeProfile,
    Settings,
    SUPPORTED_RUNTIME_MODES,
    SUPPORTED_RUNTIME_ROLES,
    ensure_storage_fallback_allowed,
    ensure_prod_api_token_configured,
    ensure_storage_fallback_allowed,
    get_settings,
    settings_with_storage_backend,
    validate_startup_settings,
)


def _database_url(settings: Settings) -> str:
    direct = getattr(settings, "database_url", None)
    if direct is not None:
        value = str(direct or "").strip()
        if value:
            return value
    storage = getattr(settings, "storage", None)
    if storage is None:
        return str(direct or "").strip()
    return str(getattr(storage, "database_url", "") or "").strip()


class _ReadinessSettingsError(RuntimeError):
    pass


_MISSING_READINESS_SETTING = object()


def _resolve_readiness_setting(
    settings: Settings,
    *,
    direct_name: str,
    nested_name: str,
    nested_value_name: str,
    default: str,
    allowed_values: tuple[str, ...],
    allow_undeclared_default: bool = False,
) -> str:
    direct_raw = getattr(
        settings,
        direct_name,
        _MISSING_READINESS_SETTING,
    )
    nested_settings = getattr(
        settings,
        nested_name,
        _MISSING_READINESS_SETTING,
    )
    nested_raw = (
        _MISSING_READINESS_SETTING
        if nested_settings is _MISSING_READINESS_SETTING
        else getattr(
            nested_settings,
            nested_value_name,
            _MISSING_READINESS_SETTING,
        )
    )
    direct = str(
        "" if direct_raw is _MISSING_READINESS_SETTING else direct_raw or ""
    ).strip().lower()
    nested = str(
        "" if nested_raw is _MISSING_READINESS_SETTING else nested_raw or ""
    ).strip().lower()
    if direct and nested and direct != nested:
        raise _ReadinessSettingsError(
            f"readiness_settings_conflict:{direct_name}"
        )
    resolved = nested or direct
    if resolved:
        if resolved not in allowed_values:
            raise _ReadinessSettingsError(
                f"readiness_settings_invalid:{direct_name}"
            )
        return resolved
    if (
        allow_undeclared_default
        and direct_raw is _MISSING_READINESS_SETTING
        and nested_settings is _MISSING_READINESS_SETTING
    ):
        return default
    raise _ReadinessSettingsError(f"readiness_settings_missing:{direct_name}")


def _runtime_mode(settings: Settings) -> str:
    return _resolve_readiness_setting(
        settings,
        direct_name="runtime_mode",
        nested_name="runtime",
        nested_value_name="mode",
        default="dev",
        allowed_values=SUPPORTED_RUNTIME_MODES,
    )


def _runtime_role(settings: Settings) -> str:
    legacy_runtime = getattr(
        settings,
        "runtime",
        _MISSING_READINESS_SETTING,
    )
    # Nested-only compatibility objects predate ``core.role``. Their only safe
    # role default is API, which retains both schema and admission probes. A
    # declared-but-blank flattened or nested role still fails as malformed.
    legacy_nested_only = (
        getattr(settings, "runtime_mode", _MISSING_READINESS_SETTING)
        is _MISSING_READINESS_SETTING
        and legacy_runtime is not _MISSING_READINESS_SETTING
        and bool(str(getattr(legacy_runtime, "mode", "") or "").strip())
    )
    return _resolve_readiness_setting(
        settings,
        direct_name="role",
        nested_name="core",
        nested_value_name="role",
        default="api",
        allowed_values=SUPPORTED_RUNTIME_ROLES,
        allow_undeclared_default=legacy_nested_only,
    )


def _runtime_authority(settings: Settings) -> tuple[str, str]:
    runtime_mode = _runtime_mode(settings)
    runtime_role = _runtime_role(settings)
    if runtime_mode == "prod" and runtime_role == "operator-tools":
        raise _ReadinessSettingsError(
            "readiness_settings_invalid:operator_tools_prod"
        )
    return runtime_mode, runtime_role


class ReadinessService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._startup_gates: dict[str, tuple[bool, str]] = {}

    def register_startup_gate(self, name: str) -> None:
        normalized = str(name or "").strip()
        if normalized and normalized not in self._startup_gates:
            self._startup_gates[normalized] = (False, "pending")

    def mark_startup_gate_ready(self, name: str, reason: str = "ready") -> None:
        normalized = str(name or "").strip()
        if normalized:
            self._startup_gates[normalized] = (True, str(reason or "ready").strip() or "ready")

    def mark_startup_gate_failed(self, name: str, reason: str = "failed") -> None:
        normalized = str(name or "").strip()
        if normalized:
            self._startup_gates[normalized] = (False, str(reason or "failed").strip() or "failed")

    def _startup_gate_blocker(self) -> str:
        for name, (ready, reason) in sorted(self._startup_gates.items()):
            if not ready:
                return f"{name}:{reason or 'pending'}"
        return ""

    def check(self) -> tuple[bool, str]:
        try:
            _runtime_authority(self._settings)
        except _ReadinessSettingsError as exc:
            return False, str(exc)
        try:
            profile = validate_startup_settings(self._settings)
        except RuntimeError as exc:
            message = str(exc)
            if "EA_API_TOKEN" in message:
                return False, "prod_api_token_missing"
            if "DATABASE_URL" in message:
                return False, "database_url_missing"
            return False, "startup_validation_failed"
        if profile.storage_backend == "memory":
            startup_blocker = self._startup_gate_blocker()
            if startup_blocker:
                return False, startup_blocker
            if str(self._settings.storage.backend or "").strip().lower() == "memory":
                return True, "memory_ready"
            return True, "auto_memory_ready"
        if not _database_url(self._settings):
            return False, "database_url_missing"
        ready, reason = self._probe_database()
        if not ready:
            return ready, reason
        startup_blocker = self._startup_gate_blocker()
        if startup_blocker:
            return False, startup_blocker
        return ready, reason

    def _probe_database(self) -> tuple[bool, str]:
        try:
            runtime_mode, runtime_role = _runtime_authority(self._settings)
        except _ReadinessSettingsError as exc:
            return False, str(exc)
        try:
            import psycopg
        except Exception:
            return False, "psycopg_missing"
        admission_database_url = ""
        if (
            runtime_mode == "prod"
            and runtime_role == "api"
        ):
            try:
                admission_database_url = resolve_api_admission_database_url(
                    runtime_mode=runtime_mode,
                    primary_database_url=_database_url(self._settings),
                )
            except RuntimeError as exc:
                return False, f"propertyquarry_admission_not_ready:{exc}"
        try:
            with psycopg.connect(
                _database_url(self._settings),
                autocommit=True,
                connect_timeout=3,
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT
                            set_config('statement_timeout', %s, FALSE),
                            set_config('lock_timeout', %s, FALSE)
                        """,
                        ("2000ms", "250ms"),
                    )
                    _ = cur.fetchone()
                    cur.execute("SELECT 1")
                    _ = cur.fetchone()
                    from app.product.property_search_schema import (
                        inspect_property_search_schema_cursor,
                        property_search_schema_readiness_required,
                    )

                    if property_search_schema_readiness_required(
                        runtime_mode=runtime_mode,
                        role=runtime_role,
                        explicit=str(
                            os.environ.get(
                                "PROPERTYQUARRY_SEARCH_SCHEMA_READINESS_REQUIRED"
                            )
                            or ""
                        ),
                    ):
                        schema_status = inspect_property_search_schema_cursor(
                            cur,
                            verify_capacity_counts=False,
                        )
                        if not schema_status.ready:
                            return (
                                False,
                                f"property_search_schema_not_ready:{schema_status.reason}",
                            )
                        from app.product.property_search_storage import (
                            _property_search_erasure_key_id,
                        )

                        try:
                            expected_erasure_key_id = (
                                _property_search_erasure_key_id()
                            )
                        except RuntimeError as exc:
                            return False, f"property_search_erasure_key_not_ready:{exc}"
                        cur.execute(
                            """
                            SELECT key_id
                            FROM property_search_erasure_key_state
                            WHERE singleton = TRUE
                            """
                        )
                        erasure_key_row = cur.fetchone()
                        if not erasure_key_row:
                            return False, "property_search_erasure_key_not_ready:key_state_missing"
                        if str(erasure_key_row[0] or "").strip() != expected_erasure_key_id:
                            return False, "property_search_erasure_key_not_ready:key_id_mismatch"
                        if (
                            runtime_mode == "prod"
                            and runtime_role == "api"
                        ):
                            try:
                                with psycopg.connect(
                                    admission_database_url,
                                    autocommit=True,
                                    connect_timeout=5,
                                    application_name=(
                                        "propertyquarry-api-admission-readiness"
                                    ),
                                ) as admission_connection:
                                    with (
                                        admission_connection.cursor()
                                    ) as admission_cursor:
                                        probe_admission_cursor(admission_cursor)
                            except AdmissionBackendUnavailable as exc:
                                return False, f"propertyquarry_admission_not_ready:{exc}"
                            except Exception as exc:
                                return (
                                    False,
                                    "propertyquarry_admission_not_ready:"
                                    f"{exc.__class__.__name__}",
                                )
                        return (
                            True,
                            f"postgres_ready:property_search_schema_v{schema_status.current_version}",
                        )
            return True, "postgres_ready"
        except Exception as exc:
            return False, f"postgres_unavailable:{exc.__class__.__name__}"


@dataclass(frozen=True)
class AppContainer:
    settings: Settings
    runtime_profile: RuntimeProfile
    orchestrator: RewriteOrchestrator
    channel_runtime: ChannelRuntimeService
    tool_runtime: ToolRuntimeService
    tool_execution: ToolExecutionService
    evidence_runtime: EvidenceRuntimeService
    memory_runtime: MemoryRuntimeService
    task_contracts: TaskContractService
    skills: SkillCatalogService
    planner: PlannerService
    provider_registry: ProviderRegistryService
    brain_router: BrainRouterService
    onemin_manager: OneminManagerService
    cognitive_load: CognitiveLoadService
    proactive_horizon: ProactiveHorizonService
    onboarding: OnboardingService
    preference_profiles: PreferenceProfileService
    readiness: ReadinessService


def _bootstrap_runtime_component(
    settings: Settings,
    reason: str,
    factory,
):
    try:
        return factory()
    except Exception as exc:
        ensure_storage_fallback_allowed(settings, reason, exc)
        raise


def _build_provider_registry(settings: Settings) -> ProviderRegistryService:
    return _bootstrap_runtime_component(
        settings,
        "provider registry bootstrap",
        lambda: ProviderRegistryService(provider_binding_repo=build_provider_binding_service_repo(settings)),
    )


def _build_artifacts(settings: Settings):
    return _bootstrap_runtime_component(
        settings,
        "artifact repo bootstrap",
        lambda: build_artifact_repo(settings),
    )


def _build_task_contracts(settings: Settings) -> TaskContractService:
    return _bootstrap_runtime_component(
        settings,
        "task contracts bootstrap",
        lambda: build_task_contract_service(settings=settings),
    )


def _build_channel_runtime(settings: Settings, policy: PolicyDecisionService) -> ChannelRuntimeService:
    return _bootstrap_runtime_component(
        settings,
        "channel runtime bootstrap",
        lambda: build_channel_runtime(settings=settings, policy=policy),
    )


def _build_memory_runtime(settings: Settings) -> MemoryRuntimeService:
    return _bootstrap_runtime_component(
        settings,
        "memory runtime bootstrap",
        lambda: build_memory_runtime(settings=settings),
    )


def _build_evidence_runtime(settings: Settings) -> EvidenceRuntimeService:
    return _bootstrap_runtime_component(
        settings,
        "evidence runtime bootstrap",
        lambda: build_evidence_runtime(settings=settings),
    )


def _build_tool_runtime(settings: Settings) -> ToolRuntimeService:
    return _bootstrap_runtime_component(
        settings,
        "tool runtime bootstrap",
        lambda: build_tool_runtime(settings=settings),
    )


def _build_onemin_manager(settings: Settings) -> OneminManagerService:
    return _bootstrap_runtime_component(
        settings,
        "onemin manager bootstrap",
        lambda: OneminManagerService(repo=build_onemin_manager_service_repo(settings)),
    )


def _build_preference_profiles(settings: Settings) -> PreferenceProfileService:
    return _bootstrap_runtime_component(
        settings,
        "preference profile bootstrap",
        lambda: build_preference_profile_service(settings),
    )


def _build_container_for_settings(settings: Settings, profile: RuntimeProfile) -> AppContainer:
    provider_registry = _build_provider_registry(settings)
    brain_router = BrainRouterService(provider_registry=provider_registry)
    artifacts = _build_artifacts(settings)
    task_contracts = _build_task_contracts(settings)
    planner = PlannerService(task_contracts, provider_registry=provider_registry, brain_router=brain_router)
    skills = SkillCatalogService(task_contracts)
    policy = PolicyDecisionService(
        max_rewrite_chars=settings.policy.max_rewrite_chars,
        approval_required_chars=settings.policy.approval_required_chars,
    )
    channel_runtime = _build_channel_runtime(settings, policy)
    memory_runtime = _build_memory_runtime(settings)
    cognitive_load = CognitiveLoadService(
        count_recent_for_principal=channel_runtime.count_recent_observations_for_principal,
        memory_runtime=memory_runtime,
    )
    channel_runtime._cognitive_load = cognitive_load  # type: ignore[attr-defined]
    channel_runtime._policy = policy  # type: ignore[attr-defined]
    evidence_runtime = _build_evidence_runtime(settings)
    tool_runtime = _build_tool_runtime(settings)
    onemin_manager = _build_onemin_manager(settings)
    register_onemin_manager(onemin_manager)
    tool_execution = ToolExecutionService(
        tool_runtime=tool_runtime,
        artifacts=artifacts,
        channel_runtime=channel_runtime,
        evidence_runtime=evidence_runtime,
        provider_registry=provider_registry,
    )
    orchestrator = build_default_orchestrator(
        settings=settings,
        artifacts=artifacts,
        task_contracts=task_contracts,
        skills=skills,
        planner=planner,
        evidence_runtime=evidence_runtime,
        memory_runtime=memory_runtime,
        provider_registry=provider_registry,
        tool_execution=tool_execution,
    )
    proactive_horizon = ProactiveHorizonService(
        memory_runtime=memory_runtime,
        orchestrator=orchestrator,
        task_contracts=task_contracts,
        channel_runtime=channel_runtime,
    )
    onboarding = build_onboarding_service(
        settings=settings,
        provider_registry=provider_registry,
        tool_runtime=tool_runtime,
        memory_runtime=memory_runtime,
    )
    preference_profiles = _build_preference_profiles(settings)
    return AppContainer(
        settings=settings,
        runtime_profile=profile,
        orchestrator=orchestrator,
        channel_runtime=channel_runtime,
        tool_runtime=tool_runtime,
        tool_execution=tool_execution,
        evidence_runtime=evidence_runtime,
        memory_runtime=memory_runtime,
        task_contracts=task_contracts,
        skills=skills,
        planner=planner,
        provider_registry=provider_registry,
        brain_router=brain_router,
        onemin_manager=onemin_manager,
        cognitive_load=cognitive_load,
        proactive_horizon=proactive_horizon,
        onboarding=onboarding,
        preference_profiles=preference_profiles,
        readiness=ReadinessService(settings),
    )


def build_container(settings: Settings | None = None) -> AppContainer:
    configured = settings or get_settings()
    profile = validate_startup_settings(configured)
    ensure_prod_api_token_configured(configured)
    log = logging.getLogger("ea.container")
    if profile.storage_backend == "memory":
        effective_settings = settings_with_storage_backend(configured, "memory")
        memory_profile = validate_startup_settings(effective_settings)
        return _build_container_for_settings(effective_settings, memory_profile)

    effective_settings = settings_with_storage_backend(configured, "postgres")
    postgres_profile = validate_startup_settings(effective_settings)
    try:
        return _build_container_for_settings(effective_settings, postgres_profile)
    except Exception as exc:
        if str(configured.storage.backend or "").strip().lower() == "auto" and configured.storage_fallback_allowed:
            log.warning("postgres runtime profile unavailable, switching whole container to memory: %s", exc)
            memory_settings = settings_with_storage_backend(configured, "memory")
            memory_profile = validate_startup_settings(memory_settings)
            return _build_container_for_settings(memory_settings, memory_profile)
        raise
