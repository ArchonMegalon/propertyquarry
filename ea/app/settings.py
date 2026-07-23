from __future__ import annotations

import os
import secrets
import warnings
from dataclasses import dataclass, replace


_PROCESS_SIGNING_SECRET = secrets.token_urlsafe(48)

SUPPORTED_RUNTIME_MODES = ("dev", "test", "prod")
SUPPORTED_RUNTIME_ROLES = (
    "api",
    "worker",
    "scheduler",
    "openvoice",
    "operator-tools",
    "render-tools",
    "property-search-migrate",
)


def _to_int(raw: str, default: int) -> int:
    try:
        return int(raw)
    except Exception:
        return default


def _env_truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_optional_bool(raw: str | None) -> bool | None:
    normalized = str(raw or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


@dataclass(frozen=True)
class CoreSettings:
    app_name: str
    app_version: str
    role: str
    host: str
    port: int
    log_level: str
    tenant_id: str


@dataclass(frozen=True)
class RuntimeSettings:
    mode: str
    storage_fallback_allowed_override: bool | None = None


@dataclass(frozen=True)
class StorageSettings:
    backend: str
    database_url: str
    artifacts_dir: str


@dataclass(frozen=True)
class AuthSettings:
    api_token: str
    default_principal_id: str
    signing_secret: str = ""
    allow_loopback_no_auth: bool = False
    cf_access_team_domain: str = ""
    cf_access_audiences: tuple[str, ...] = ()
    cf_access_certs_url: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_token.strip()) or self.cf_access_enabled

    @property
    def cf_access_enabled(self) -> bool:
        return bool(self.cf_access_team_domain.strip()) and bool(self.cf_access_audiences)


@dataclass(frozen=True)
class PolicySettings:
    max_rewrite_chars: int
    approval_required_chars: int
    approval_ttl_minutes: int


@dataclass(frozen=True)
class ChannelSettings:
    default_list_limit: int


@dataclass(frozen=True)
class FeatureSettings:
    public_side_surfaces_enabled: bool = False
    public_results_enabled: bool = False
    public_tours_enabled: bool = False
    public_memorials_enabled: bool = False
    legacy_runtime_surfaces_enabled: bool = False


@dataclass(frozen=True)
class RuntimeProfile:
    mode: str
    storage_backend: str
    durability: str
    auth_mode: str
    principal_source: str
    database_required: bool
    database_configured: bool
    source_backend: str

    @property
    def caller_principal_header_allowed(self) -> bool:
        return self.principal_source in {
            "caller_header_or_default",
            "authenticated_header_or_default",
            "authenticated_header",
        }

    @property
    def caller_principal_header_requires_authentication(self) -> bool:
        return self.principal_source in {
            "authenticated_header_or_default",
            "authenticated_header",
        }

    @property
    def default_principal_fallback_allowed(self) -> bool:
        return self.principal_source in {
            "caller_header_or_default",
            "authenticated_header_or_default",
            "access_identity_or_default",
            "default_principal",
        }


@dataclass(frozen=True)
class Settings:
    core: CoreSettings
    runtime: RuntimeSettings
    storage: StorageSettings
    auth: AuthSettings
    policy: PolicySettings
    channels: ChannelSettings
    features: FeatureSettings

    @property
    def app_name(self) -> str:
        return self.core.app_name

    @property
    def app_version(self) -> str:
        return self.core.app_version

    @property
    def role(self) -> str:
        return self.core.role

    @property
    def host(self) -> str:
        return self.core.host

    @property
    def port(self) -> int:
        return self.core.port

    @property
    def log_level(self) -> str:
        return self.core.log_level

    @property
    def tenant_id(self) -> str:
        return self.core.tenant_id

    @property
    def runtime_mode(self) -> str:
        return self.runtime.mode

    @property
    def storage_fallback_allowed_override(self) -> bool | None:
        return self.runtime.storage_fallback_allowed_override

    @property
    def storage_backend(self) -> str:
        return self.storage.backend

    @property
    def database_url(self) -> str:
        return self.storage.database_url

    @property
    def ledger_backend(self) -> str:
        return self.storage.backend

    @property
    def storage_fallback_allowed(self) -> bool:
        if is_prod_mode(self.runtime.mode):
            return False
        override = self.runtime.storage_fallback_allowed_override
        if override is None:
            return True
        return override

    @property
    def public_side_surfaces_enabled(self) -> bool:
        return self.features.public_side_surfaces_enabled

    @property
    def public_results_enabled(self) -> bool:
        return self.features.public_results_enabled

    @property
    def public_tours_enabled(self) -> bool:
        return self.features.public_tours_enabled

    @property
    def public_memorials_enabled(self) -> bool:
        return self.features.public_memorials_enabled

    @property
    def legacy_runtime_surfaces_enabled(self) -> bool:
        return self.features.legacy_runtime_surfaces_enabled


def _runtime_mode(raw: str | None) -> str:
    if raw is None:
        return "dev"
    mode = str(raw).strip().lower()
    if mode not in SUPPORTED_RUNTIME_MODES:
        raise RuntimeError("EA_RUNTIME_MODE must be one of dev, test, prod")
    return mode


def _runtime_role(raw: str | None) -> str:
    if raw is None:
        return "api"
    role = str(raw).strip().lower()
    if role not in SUPPORTED_RUNTIME_ROLES:
        raise RuntimeError(
            "EA_ROLE must be one of api, worker, scheduler, openvoice, "
            "operator-tools, render-tools, property-search-migrate"
        )
    return role


def ensure_runtime_authority_valid(settings: Settings) -> None:
    runtime = getattr(settings, "runtime", None)
    mode = str(getattr(runtime, "mode", "") or "").strip().lower()
    if mode not in SUPPORTED_RUNTIME_MODES:
        raise RuntimeError("EA_RUNTIME_MODE must be one of dev, test, prod")
    core = getattr(settings, "core", None)
    role = (
        "api"
        if core is None
        else str(getattr(core, "role", "") or "").strip().lower()
    )
    if role not in SUPPORTED_RUNTIME_ROLES:
        raise RuntimeError(
            "EA_ROLE must be one of api, worker, scheduler, openvoice, "
            "operator-tools, render-tools, property-search-migrate"
        )
    if role == "operator-tools" and mode == "prod":
        raise RuntimeError(
            "EA_ROLE=operator-tools requires EA_RUNTIME_MODE=dev or test"
        )


def is_prod_mode(raw: str | None) -> bool:
    return str(raw or "").strip().lower() == "prod"


def _database_url(settings: object) -> str:
    direct = getattr(settings, "database_url", None)
    if direct is not None:
        value = str(direct or "").strip()
        if value:
            return value
    storage = getattr(settings, "storage", None)
    if storage is None:
        return str(direct or "").strip()
    return str(getattr(storage, "database_url", "") or "").strip()


def resolve_signing_secret(settings: Settings, *, purpose: str = "") -> str:
    auth = getattr(settings, "auth", None)
    configured = ""
    if auth is not None:
        configured = str(getattr(auth, "signing_secret", "") or "").strip()
    secret = configured or _PROCESS_SIGNING_SECRET
    normalized_purpose = str(purpose or "").strip()
    if not normalized_purpose:
        return secret
    return f"{secret}:{normalized_purpose}"


def resolve_runtime_profile(settings: Settings) -> RuntimeProfile:
    source_backend = str(settings.storage.backend or "auto").strip().lower() or "auto"
    database_url = _database_url(settings)
    auth_mode = "anonymous_dev"
    api_token = str(getattr(settings.auth, "api_token", "") or "").strip()
    cf_access_enabled = bool(getattr(settings.auth, "cf_access_enabled", False))
    if api_token and cf_access_enabled:
        auth_mode = "token_or_access"
    elif api_token:
        auth_mode = "token"
    elif cf_access_enabled:
        auth_mode = "access"
    if is_prod_mode(settings.runtime.mode):
        return RuntimeProfile(
            mode="prod",
            storage_backend="postgres",
            durability="durable",
            auth_mode=auth_mode,
            principal_source="verified_identity",
            database_required=True,
            database_configured=bool(database_url),
            source_backend=source_backend,
        )
    storage_backend = "postgres" if source_backend in {"postgres"} else "memory"
    durability = "durable" if storage_backend == "postgres" else "ephemeral"
    if source_backend == "auto" and database_url:
        storage_backend = "postgres"
        durability = "durable"
    if auth_mode in {"token", "token_or_access"}:
        principal_source = "authenticated_header_or_default"
    elif auth_mode == "access":
        principal_source = "access_identity_or_default"
    else:
        principal_source = "caller_header_or_default"
    return RuntimeProfile(
        mode=settings.runtime.mode,
        storage_backend=storage_backend,
        durability=durability,
        auth_mode=auth_mode,
        principal_source=principal_source,
        database_required=storage_backend == "postgres",
        database_configured=bool(database_url),
        source_backend=source_backend,
    )


def settings_with_storage_backend(settings: Settings, backend: str) -> Settings:
    normalized = str(backend or "").strip().lower() or "memory"
    return replace(settings, storage=replace(settings.storage, backend=normalized))


def ensure_storage_fallback_allowed(
    settings: Settings,
    reason: str,
    exc: Exception | None = None,
) -> None:
    if settings.storage_fallback_allowed:
        return
    if exc is not None:
        message = str(exc)
        if message.startswith("EA_RUNTIME_MODE=prod forbids memory fallback"):
            raise exc
    message = f"EA_RUNTIME_MODE=prod forbids memory fallback({reason})"
    if exc is not None:
        raise RuntimeError(message) from exc
    raise RuntimeError(message)


def ensure_prod_api_token_configured(settings: Settings) -> None:
    if not is_prod_mode(settings.runtime.mode):
        return
    if str(settings.auth.api_token or "").strip():
        return
    if bool(getattr(settings.auth, "cf_access_enabled", False)):
        return
    raise RuntimeError("EA_RUNTIME_MODE=prod requires EA_API_TOKEN or Cloudflare Access auth to be set")


def ensure_prod_signing_secret_configured(settings: Settings) -> None:
    if not is_prod_mode(settings.runtime.mode):
        return
    if str(getattr(settings.auth, "signing_secret", "") or "").strip():
        return
    raise RuntimeError("EA_RUNTIME_MODE=prod requires EA_SIGNING_SECRET")


def ensure_prod_loopback_no_auth_disabled(settings: Settings) -> None:
    if not is_prod_mode(settings.runtime.mode):
        return
    if not bool(getattr(settings.auth, "allow_loopback_no_auth", False)):
        return
    raise RuntimeError("EA_RUNTIME_MODE=prod forbids EA_ALLOW_LOOPBACK_NO_AUTH=1")


def _email_sender_domain(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if "<" in normalized and ">" in normalized:
        normalized = normalized.split("<", 1)[1].split(">", 1)[0].strip()
    if "@" not in normalized:
        return ""
    return normalized.rsplit("@", 1)[1].strip().strip(".")


def _propertyquarry_sender_domain_allowed(value: str) -> bool:
    domain = _email_sender_domain(value)
    return domain == "propertyquarry.com" or domain.endswith(".propertyquarry.com")


def ensure_prod_registration_email_sender_domain(settings: Settings) -> None:
    if not is_prod_mode(settings.runtime.mode):
        return
    for key in ("EA_REGISTRATION_EMAIL_FROM", "EA_EMAIL_DEFAULT_FROM", "EA_REGISTRATION_EMAIL_FROM_FALLBACK"):
        value = str(os.environ.get(key) or "").strip()
        if value and not _propertyquarry_sender_domain_allowed(value):
            raise RuntimeError(
                "EA_RUNTIME_MODE=prod requires PropertyQuarry email sender domains"
            )


def validate_startup_settings(settings: Settings) -> RuntimeProfile:
    ensure_runtime_authority_valid(settings)
    ensure_prod_api_token_configured(settings)
    ensure_prod_signing_secret_configured(settings)
    ensure_prod_loopback_no_auth_disabled(settings)
    ensure_prod_registration_email_sender_domain(settings)
    profile = resolve_runtime_profile(settings)
    if is_prod_mode(settings.runtime.mode):
        if profile.storage_backend != "postgres":
            raise RuntimeError("EA_RUNTIME_MODE=prod requires a durable postgres runtime profile")
        if not _database_url(settings):
            raise RuntimeError("EA_RUNTIME_MODE=prod requires DATABASE_URL")
    return profile


def get_settings() -> Settings:
    app_name = (os.environ.get("EA_APP_NAME") or "PropertyQuarry").strip() or "PropertyQuarry"
    app_version = (os.environ.get("EA_APP_VERSION") or "0.3.0").strip() or "0.3.0"
    role = _runtime_role(os.environ.get("EA_ROLE"))
    host = (os.environ.get("EA_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    port = max(1, min(65535, _to_int(os.environ.get("EA_PORT") or "8090", 8090)))
    log_level = (os.environ.get("EA_LOG_LEVEL") or "INFO").strip().upper() or "INFO"
    tenant_id = (os.environ.get("EA_TENANT_ID") or "default").strip() or "default"
    runtime_mode = _runtime_mode(os.environ.get("EA_RUNTIME_MODE"))
    storage_fallback_allowed_override = _env_optional_bool(os.environ.get("EA_STORAGE_FALLBACK_ALLOWED"))

    legacy_backend = (os.environ.get("EA_LEDGER_BACKEND") or "").strip().lower()
    configured_storage_backend = (os.environ.get("EA_STORAGE_BACKEND") or "").strip().lower()
    if legacy_backend and not configured_storage_backend:
        warnings.warn(
            "EA_LEDGER_BACKEND is deprecated; use EA_STORAGE_BACKEND instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    elif legacy_backend and configured_storage_backend:
        warnings.warn(
            "EA_LEDGER_BACKEND is deprecated and ignored when EA_STORAGE_BACKEND is set.",
            DeprecationWarning,
            stacklevel=2,
        )
    storage_backend = (configured_storage_backend or legacy_backend or "auto").strip().lower() or "auto"
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    artifacts_dir = (os.environ.get("EA_ARTIFACTS_DIR") or "/tmp/ea_artifacts").strip() or "/tmp/ea_artifacts"

    api_token = (os.environ.get("EA_API_TOKEN") or "").strip()
    signing_secret = (os.environ.get("EA_SIGNING_SECRET") or "").strip()
    default_principal_id = (os.environ.get("EA_DEFAULT_PRINCIPAL_ID") or "local-user").strip() or "local-user"
    allow_loopback_no_auth = _env_truthy(os.environ.get("EA_ALLOW_LOOPBACK_NO_AUTH"))
    cf_access_team_domain = (os.environ.get("EA_CF_ACCESS_TEAM_DOMAIN") or "").strip().lower().rstrip("/")
    raw_cf_access_aud = (os.environ.get("EA_CF_ACCESS_AUD") or "").strip()
    cf_access_audiences = tuple(
        value for value in {part.strip() for part in raw_cf_access_aud.split(",") if part.strip()}
    )
    cf_access_certs_url = (os.environ.get("EA_CF_ACCESS_CERTS_URL") or "").strip()
    if not cf_access_certs_url and cf_access_team_domain:
        cf_access_certs_url = f"https://{cf_access_team_domain}/cdn-cgi/access/certs"
    max_rewrite_chars = max(1, _to_int(os.environ.get("EA_MAX_REWRITE_CHARS") or "20000", 20000))
    approval_required_chars = max(1, _to_int(os.environ.get("EA_APPROVAL_THRESHOLD_CHARS") or "5000", 5000))
    approval_ttl_minutes = max(1, _to_int(os.environ.get("EA_APPROVAL_TTL_MINUTES") or "120", 120))
    default_list_limit = max(1, min(500, _to_int(os.environ.get("EA_CHANNEL_DEFAULT_LIMIT") or "50", 50)))
    raw_public_side_surfaces_enabled = os.environ.get("PROPERTYQUARRY_ENABLE_PUBLIC_SIDE_SURFACES")
    if raw_public_side_surfaces_enabled is None:
        raw_public_side_surfaces_enabled = os.environ.get("EA_ENABLE_PUBLIC_SIDE_SURFACES")
    raw_public_results_enabled = os.environ.get("PROPERTYQUARRY_ENABLE_PUBLIC_RESULTS")
    if raw_public_results_enabled is None:
        raw_public_results_enabled = os.environ.get("EA_ENABLE_PUBLIC_RESULTS")
    raw_public_tours_enabled = os.environ.get("PROPERTYQUARRY_ENABLE_PUBLIC_TOURS")
    if raw_public_tours_enabled is None:
        raw_public_tours_enabled = os.environ.get("EA_ENABLE_PUBLIC_TOURS")
    raw_public_memorials_enabled = os.environ.get("PROPERTYQUARRY_ENABLE_PUBLIC_MEMORIALS")
    if raw_public_memorials_enabled is None:
        raw_public_memorials_enabled = os.environ.get("EA_ENABLE_PUBLIC_MEMORIALS")
    raw_legacy_runtime_surfaces_enabled = os.environ.get("PROPERTYQUARRY_ENABLE_LEGACY_RUNTIME_SURFACES")
    if raw_legacy_runtime_surfaces_enabled is None:
        raw_legacy_runtime_surfaces_enabled = os.environ.get("EA_ENABLE_LEGACY_RUNTIME_SURFACES")
    public_side_surfaces_enabled = _env_truthy(raw_public_side_surfaces_enabled)
    public_results_enabled = (
        public_side_surfaces_enabled
        if raw_public_results_enabled is None
        else _env_truthy(raw_public_results_enabled)
    )
    public_tours_enabled = (
        public_side_surfaces_enabled
        if raw_public_tours_enabled is None
        else _env_truthy(raw_public_tours_enabled)
    )
    public_memorials_enabled = (
        public_side_surfaces_enabled
        if raw_public_memorials_enabled is None
        else _env_truthy(raw_public_memorials_enabled)
    )
    inferred_legacy_runtime_surfaces_enabled = bool(
        str(os.environ.get("EA_TELEGRAM_INGEST_SECRET") or "").strip()
        or str(os.environ.get("EA_TELEGRAM_BOT_TOKEN") or "").strip()
        or (
            str(os.environ.get("EA_API_TOKEN") or "").strip()
            and (
                str(os.environ.get("EA_TRUST_AUTHENTICATED_PRINCIPAL_HEADER") or "").strip()
                or str(os.environ.get("EA_OPERATOR_PRINCIPAL_IDS") or "").strip()
            )
        )
    )
    legacy_runtime_surfaces_enabled = (
        inferred_legacy_runtime_surfaces_enabled
        if raw_legacy_runtime_surfaces_enabled is None
        else _env_truthy(raw_legacy_runtime_surfaces_enabled)
    )

    settings = Settings(
        core=CoreSettings(
            app_name=app_name,
            app_version=app_version,
            role=role,
            host=host,
            port=port,
            log_level=log_level,
            tenant_id=tenant_id,
        ),
        runtime=RuntimeSettings(
            mode=runtime_mode,
            storage_fallback_allowed_override=storage_fallback_allowed_override,
        ),
        storage=StorageSettings(
            backend=storage_backend,
            database_url=database_url,
            artifacts_dir=artifacts_dir,
        ),
        auth=AuthSettings(
            api_token=api_token,
            default_principal_id=default_principal_id,
            signing_secret=signing_secret,
            allow_loopback_no_auth=allow_loopback_no_auth,
            cf_access_team_domain=cf_access_team_domain,
            cf_access_audiences=cf_access_audiences,
            cf_access_certs_url=cf_access_certs_url,
        ),
        policy=PolicySettings(
            max_rewrite_chars=max_rewrite_chars,
            approval_required_chars=approval_required_chars,
            approval_ttl_minutes=approval_ttl_minutes,
        ),
        channels=ChannelSettings(default_list_limit=default_list_limit),
        features=FeatureSettings(
            public_side_surfaces_enabled=public_side_surfaces_enabled
            or public_results_enabled
            or public_tours_enabled
            or public_memorials_enabled,
            public_results_enabled=public_results_enabled,
            public_tours_enabled=public_tours_enabled,
            public_memorials_enabled=public_memorials_enabled,
            legacy_runtime_surfaces_enabled=legacy_runtime_surfaces_enabled,
        ),
    )
    ensure_runtime_authority_valid(settings)
    ensure_prod_api_token_configured(settings)
    return settings
