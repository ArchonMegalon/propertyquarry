from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
import urllib.parse
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.dependencies import RequestContext, get_container, get_request_context, resolve_principal_id
from app.api.routes.landing import _normalize_browser_return_to
from app.container import AppContainer
from app.property_distance_preferences import (
    normalize_property_distance_importance,
    property_distance_importance_key,
    property_distance_preference_keys,
)
from app.product.service import build_product_service
from app.services import propertyquarry_google_identity
from app.services.google_oauth import (
    complete_google_oauth_callback,
    GOOGLE_PROVIDER_KEY,
)
from app.services.property_market_catalog import default_timezone_for_country
from app.services.registration_email import send_registration_email

router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])
register_router = APIRouter(prefix="/v1/register", tags=["registration"])


def _registration_secret(container: AppContainer) -> str:
    runtime_mode = str(getattr(getattr(container.settings, "runtime", None), "mode", "dev") or "dev").strip().lower()
    dedicated_secret = str(
        os.environ.get("PROPERTYQUARRY_IDENTITY_SESSION_SECRET") or ""
    ).strip()
    if dedicated_secret:
        if len(dedicated_secret) < 32:
            raise RuntimeError("propertyquarry_registration_secret_weak")
        return hmac.new(
            dedicated_secret.encode("utf-8"),
            b"propertyquarry-registration-verification-v1",
            hashlib.sha256,
        ).hexdigest()
    if runtime_mode == "prod":
        raise RuntimeError("propertyquarry_registration_secret_missing")
    api_token = str(getattr(getattr(container.settings, "auth", None), "api_token", "") or "").strip()
    default_principal = str(getattr(getattr(container.settings, "auth", None), "default_principal_id", "") or "").strip()
    return f"register:{runtime_mode}:{api_token or default_principal or 'local-user'}"


def _registration_code_digest(
    *,
    container: AppContainer,
    verification_code: str,
) -> str:
    return hmac.new(
        _registration_secret(container).encode("utf-8"),
        f"code:{str(verification_code or '').strip()}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _sign_registration_payload(*, container: AppContainer, payload: dict[str, object]) -> str:
    encoded = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(
        _registration_secret(container).encode("utf-8"),
        encoded.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{encoded}.{_urlsafe_b64encode(signature)}"


def _verify_registration_payload(*, container: AppContainer, token: str) -> dict[str, object] | None:
    encoded, dot, provided_signature = str(token or "").partition(".")
    if not encoded or not dot or not provided_signature:
        return None
    expected_signature = _urlsafe_b64encode(
        hmac.new(
            _registration_secret(container).encode("utf-8"),
            encoded.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    )
    if not hmac.compare_digest(provided_signature, expected_signature):
        return None
    try:
        payload = json.loads(_urlsafe_b64decode(encoded).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    expires_at = int(payload.get("expires_at") or 0)
    if expires_at <= int(time.time()):
        return None
    return payload


def _registration_principal_id(email: str) -> str:
    normalized = str(email or "").strip().lower()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"user-{digest}"


def _workspace_name_from_email(email: str) -> str:
    local = str(email or "").strip().split("@", 1)[0].replace(".", " ").replace("_", " ").replace("-", " ").strip()
    return " ".join(part.capitalize() for part in local.split() if part) or "Personal Workspace"


def _registration_base_url(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-host") or "").strip().lower().rstrip(".")
    request_host = str(request.url.hostname or "").strip().lower().rstrip(".")
    effective_host = forwarded or request_host
    if effective_host in {"propertyquarry.com", "www.propertyquarry.com"}:
        return f"https://{effective_host}"
    explicit = str(os.environ.get("EA_PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    redirect_uri = str(os.environ.get("EA_GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    if redirect_uri:
        parsed = urlparse(redirect_uri)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    return str(request.base_url).rstrip("/")


class RegisterStartIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    return_to: str = Field(default="/app/search", max_length=2048)


class RegisterStartOut(BaseModel):
    email: str
    verification_token: str
    verification_code: str = ""
    magic_link_url: str
    expires_at: int
    workspace_name: str
    suggested_timezone: str = "UTC"
    suggested_language: str = "en"
    email_delivery_status: str = ""
    email_delivery_provider: str = ""
    email_delivery_id: str = ""
    email_delivery_error: str = ""


class RegisterVerifyIn(BaseModel):
    verification_token: str = Field(min_length=1)
    verification_code: str = Field(default="", max_length=12)
    workspace_name: str = Field(default="", max_length=200)
    timezone: str = Field(default="", max_length=80)
    language: str = Field(default="", max_length=80)
    scope_bundle: str = Field(default="identity", max_length=50)


class RegisterVerifyOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    principal_id: str = ""
    status: str = ""
    workspace: dict[str, object] = Field(default_factory=dict)
    selected_channels: list[str] = Field(default_factory=list)
    privacy: dict[str, object] = Field(default_factory=dict)
    assistant_modes: list[dict[str, object]] = Field(default_factory=list)
    featured_domains: list[dict[str, object]] = Field(default_factory=list)
    storage_posture: dict[str, object] = Field(default_factory=dict)
    channels: dict[str, object] = Field(default_factory=dict)
    brief_preview: dict[str, object] = Field(default_factory=dict)
    next_step: str = ""
    onboarding_id: str = ""
    access_url: str = ""
    access_token: str = ""
    access_expires_at: str = ""
    google_start: dict[str, object] = Field(default_factory=dict)


class OnboardingStartIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_name: str = Field(min_length=1, max_length=200)
    workspace_mode: str = Field(default="personal", min_length=1, max_length=50)
    region: str = Field(default="", max_length=80)
    language: str = Field(default="", max_length=80)
    timezone: str = Field(default="", max_length=80)
    selected_channels: list[str] = Field(default_factory=list)


class OnboardingGoogleStartIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    scope_bundle: str = Field(default="identity", min_length=1, max_length=50)


class OnboardingTelegramStartIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    telegram_ref: str = Field(default="", max_length=200)
    identity_mode: str = Field(default="login_widget", min_length=1, max_length=80)
    history_mode: str = Field(default="future_only", min_length=1, max_length=80)
    assistant_surfaces: list[str] = Field(default_factory=list)


class OnboardingTelegramBotIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    bot_handle: str = Field(min_length=1, max_length=200)
    install_surfaces: list[str] = Field(default_factory=list)
    default_chat_ref: str = Field(default="", max_length=200)


class OnboardingTelegramBindChatIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    chat_ref: str = Field(min_length=1, max_length=200)
    bot_handle: str = Field(default="", max_length=200)
    bot_key: str = Field(default="default", min_length=1, max_length=80)


class OnboardingWhatsappBusinessIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    phone_number: str = Field(min_length=1, max_length=80)
    business_name: str = Field(default="", max_length=200)
    import_history_now: bool = Field(default=False)


class OnboardingWhatsappExportIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    export_label: str = Field(min_length=1, max_length=200)
    selected_chat_labels: list[str] = Field(default_factory=list)
    include_media: bool = Field(default=False)


class OnboardingWhatsappExportAckIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    binding_id: str = Field(min_length=1, max_length=200)
    imported_message_count: int = Field(default=0, ge=0)
    status: str = Field(default="imported", min_length=1, max_length=80)


class OnboardingFinalizeIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    retention_mode: str = Field(default="full_bodies", min_length=1, max_length=80)
    metadata_only_channels: list[str] = Field(default_factory=list)
    allow_drafts: bool = Field(default=False)
    allow_action_suggestions: bool = Field(default=True)
    allow_auto_briefs: bool = Field(default=False)
    auto_brief_cadence: str = Field(default="daily_morning", max_length=80)
    auto_brief_delivery_time_local: str = Field(default="08:00", max_length=16)
    auto_brief_quiet_hours_start: str = Field(default="20:00", max_length=16)
    auto_brief_quiet_hours_end: str = Field(default="07:00", max_length=16)
    auto_brief_recipient_email: str = Field(default="", max_length=320)
    auto_brief_delivery_channel: str = Field(default="email", max_length=80)


class OnboardingEnvelopeOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    principal_id: str = ""
    status: str = ""
    workspace: dict[str, object] = Field(default_factory=dict)
    selected_channels: list[str] = Field(default_factory=list)
    privacy: dict[str, object] = Field(default_factory=dict)
    assistant_modes: list[dict[str, object]] = Field(default_factory=list)
    featured_domains: list[dict[str, object]] = Field(default_factory=list)
    storage_posture: dict[str, object] = Field(default_factory=dict)
    channels: dict[str, object] = Field(default_factory=dict)
    brief_preview: dict[str, object] = Field(default_factory=dict)
    next_step: str = ""
    onboarding_id: str = ""


class OnboardingStartOut(OnboardingEnvelopeOut):
    pass


class OnboardingGoogleStartOut(OnboardingEnvelopeOut):
    google_start: dict[str, object] = Field(default_factory=dict)


class OnboardingTelegramStartOut(OnboardingEnvelopeOut):
    telegram_start: dict[str, object] = Field(default_factory=dict)


class OnboardingTelegramBotOut(OnboardingEnvelopeOut):
    telegram_bot: dict[str, object] = Field(default_factory=dict)


class OnboardingWhatsappBusinessOut(OnboardingEnvelopeOut):
    whatsapp_business: dict[str, object] = Field(default_factory=dict)


class OnboardingWhatsappExportOut(OnboardingEnvelopeOut):
    whatsapp_export: dict[str, object] = Field(default_factory=dict)


class OnboardingWhatsappExportAckOut(OnboardingEnvelopeOut):
    whatsapp_export: dict[str, object] = Field(default_factory=dict)


_PROPERTY_SEARCH_DISTANCE_RADIUS_KEYS = frozenset(
    property_distance_preference_keys()
)
_PROPERTY_SEARCH_DISTANCE_IMPORTANCE_KEYS = frozenset(
    property_distance_importance_key(key)
    for key in _PROPERTY_SEARCH_DISTANCE_RADIUS_KEYS
)
_PROPERTY_SEARCH_DISTANCE_INPUT_KEYS = (
    _PROPERTY_SEARCH_DISTANCE_RADIUS_KEYS
    | _PROPERTY_SEARCH_DISTANCE_IMPORTANCE_KEYS
)


class OnboardingPropertySearchPreferencesIn(BaseModel):
    # This endpoint has a large backwards-compatible non-distance preference
    # surface. Dynamic distance fields are nevertheless fail-closed against
    # the shared fact registry in the pre-validator below.
    model_config = ConfigDict(extra="allow")

    country_code: str = "AT"
    language_code: str = "de"
    listing_mode: str = "rent"
    property_type: str | list[str] = "any"
    location_query: str = ""
    keywords: str = ""
    selected_platforms: list[str] = Field(default_factory=list)
    property_commercial: dict[str, object] = Field(default_factory=dict)
    max_price_eur: int | None = None
    min_rooms: int | None = None
    min_area_m2: int | None = None
    max_results_per_source: int | None = None
    preference_person_id: str = "self"
    use_stored_feedback_preferences: bool = True
    alert_frequency: str = "daily"
    alert_channels: list[str] = Field(default_factory=lambda: ["telegram"])

    @model_validator(mode="before")
    @classmethod
    def _validate_registry_distance_preferences(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        seen: set[int] = set()

        def _validated_layer(source: dict[object, object], *, depth: int) -> dict[object, object]:
            if depth > 32 or id(source) in seen:
                raise ValueError("invalid nested property search preferences")
            seen.add(id(source))
            payload = dict(source)
            for raw_key in tuple(payload):
                raw_key_text = str(raw_key or "")
                key = raw_key_text.strip()
                if not key.casefold().startswith("max_distance_to_"):
                    continue
                if (
                    not isinstance(raw_key, str)
                    or raw_key != key
                    or key not in _PROPERTY_SEARCH_DISTANCE_INPUT_KEYS
                ):
                    raise ValueError(
                        f"unknown property distance preference: {raw_key_text}"
                    )
                raw_value = payload[raw_key]
                if key in _PROPERTY_SEARCH_DISTANCE_IMPORTANCE_KEYS:
                    if raw_value in (None, ""):
                        continue
                    normalized = normalize_property_distance_importance(
                        raw_value,
                        default="",
                    )
                    if not normalized:
                        raise ValueError(
                            f"invalid property distance importance for {key}"
                        )
                    payload[raw_key] = normalized
                    continue
                if raw_value in (None, ""):
                    continue
                if isinstance(raw_value, bool):
                    raise ValueError(f"invalid property distance radius for {key}")
                try:
                    numeric_value = float(str(raw_value).strip())
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"invalid property distance radius for {key}"
                    ) from exc
                if (
                    not math.isfinite(numeric_value)
                    or not numeric_value.is_integer()
                    or numeric_value < 0
                    or numeric_value > 7000
                ):
                    raise ValueError(f"invalid property distance radius for {key}")
                payload[raw_key] = int(numeric_value)
            nested = payload.get("raw_preferences")
            if isinstance(nested, dict):
                payload["raw_preferences"] = _validated_layer(
                    nested,
                    depth=depth + 1,
                )
            return payload

        return _validated_layer(value, depth=0)


class OnboardingPropertySearchPreferencesOut(OnboardingEnvelopeOut):
    selected_platforms: list[str] = Field(default_factory=list)


class OnboardingPropertySearchAgentUpdateIn(BaseModel):
    action: str = Field(default="save", min_length=1, max_length=40)
    patch: dict[str, object] = Field(default_factory=dict)


class OnboardingFlagshipStartIn(BaseModel):
    principal_id: str | None = Field(default=None, min_length=1, max_length=200)
    workspace_name: str = Field(default="PropertyQuarry account", min_length=1, max_length=200)
    workspace_mode: str = Field(default="property_search", min_length=1, max_length=50)
    region: str = Field(default="AT", max_length=80)
    language: str = Field(default="en", max_length=80)
    timezone: str = Field(default=default_timezone_for_country("AT"), max_length=80)
    selected_channels: list[str] = Field(default_factory=lambda: ["google", "telegram", "whatsapp"])
    scope_bundle: str = Field(default="identity", min_length=1, max_length=50)
    telegram_ref: str = Field(default="", max_length=200)
    telegram_identity_mode: str = Field(default="login_widget", min_length=1, max_length=80)
    telegram_history_mode: str = Field(default="future_only", min_length=1, max_length=80)
    telegram_assistant_surfaces: list[str] = Field(default_factory=lambda: ["dm", "group"])
    whatsapp_export_label: str = Field(default="", max_length=200)
    whatsapp_include_media: bool = Field(default=False)


class OnboardingFlagshipStartOut(OnboardingEnvelopeOut):
    flagship_start: dict[str, object] = Field(default_factory=dict)
    google_start: dict[str, object] = Field(default_factory=dict)
    telegram_start: dict[str, object] = Field(default_factory=dict)
    whatsapp_export: dict[str, object] = Field(default_factory=dict)


class OnboardingCallbackOut(BaseModel):
    provider_key: str
    principal_id: str
    binding_id: str
    connector_binding_id: str
    google_email: str
    google_subject: str
    google_hosted_domain: str
    granted_scopes: list[str]
    consent_stage: str
    workspace_mode: str
    token_status: str
    last_refresh_at: str
    reauth_required_reason: str


def _google_callback_payload(account) -> dict[str, object]:
    return {
        "provider_key": GOOGLE_PROVIDER_KEY,
        "principal_id": account.binding.principal_id,
        "binding_id": account.binding.binding_id,
        "connector_binding_id": account.connector_binding.binding_id if account.connector_binding is not None else "",
        "google_email": account.google_email,
        "google_subject": account.google_subject,
        "google_hosted_domain": account.google_hosted_domain,
        "granted_scopes": list(account.granted_scopes),
        "consent_stage": account.consent_stage,
        "workspace_mode": account.workspace_mode,
        "token_status": account.token_status,
        "last_refresh_at": account.last_refresh_at,
        "reauth_required_reason": account.reauth_required_reason,
    }


def _complete_onboarding_google_callback(
    code: str = Query(..., min_length=1),
    state: str = Query(..., min_length=1),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    try:
        account = complete_google_oauth_callback(container=container, code=code, state=state)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _google_callback_payload(account)


@router.get("/google/callback", response_model=OnboardingCallbackOut)
def onboarding_google_callback_get(
    code: str = Query(..., min_length=1),
    state: str = Query(..., min_length=1),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    return _complete_onboarding_google_callback(code=code, state=state, container=container)


@router.post("/google/callback", response_model=OnboardingCallbackOut)
def onboarding_google_callback_post(
    code: str = Query(..., min_length=1),
    state: str = Query(..., min_length=1),
    container: AppContainer = Depends(get_container),
) -> dict[str, object]:
    return _complete_onboarding_google_callback(code=code, state=state, container=container)


@register_router.post("/start", response_model=RegisterStartOut)
def register_start(
    body: RegisterStartIn,
    container: AppContainer = Depends(get_container),
    request: Request = None,
) -> RegisterStartOut:
    product = build_product_service(container)
    email = str(body.email or "").strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(status_code=400, detail="registration_email_invalid")
    expires_at = int(time.time()) + 15 * 60
    verification_code = f"{secrets.randbelow(1_000_000):06d}"
    return_to = _normalize_browser_return_to(body.return_to, default="/app/search")
    runtime_mode = str(getattr(getattr(container.settings, "runtime", None), "mode", "dev") or "dev").strip().lower()
    try:
        verification_token = _sign_registration_payload(
            container=container,
            payload={
                "token_kind": "register_challenge",
                "email": email,
                "verification_code_digest": _registration_code_digest(
                    container=container,
                    verification_code=verification_code,
                ),
                "return_to": return_to,
                "expires_at": expires_at,
            },
        )
    except RuntimeError as exc:
        if runtime_mode == "prod" and str(exc) in {
            "propertyquarry_registration_secret_missing",
            "propertyquarry_registration_secret_weak",
        }:
            raise HTTPException(
                status_code=503,
                detail="registration_verification_unavailable",
            ) from exc
        raise
    magic_link_url = "/register?" + urllib.parse.urlencode(
        {
            "token": verification_token,
            "code": verification_code,
            "return_to": return_to,
        }
    )
    email_delivery_status = ""
    email_delivery_provider = ""
    email_delivery_id = ""
    email_delivery_error = ""
    email_delivery_configured = bool(str(os.environ.get("EMAILIT_API_KEY") or "").strip())
    if email_delivery_configured:
        try:
            receipt = send_registration_email(
                recipient_email=email,
                verification_code=verification_code,
                magic_link_url=f"{_registration_base_url(request)}{magic_link_url}",
                expires_at=expires_at,
            )
            email_delivery_status = "sent"
            email_delivery_provider = receipt.provider
            email_delivery_id = receipt.message_id
            product.record_surface_event(
                principal_id=_registration_principal_id(email),
                event_type="registration_email_sent",
                surface="register_start",
                actor=email,
                metadata={"email": email},
            )
        except Exception as exc:
            email_delivery_status = "failed"
            email_delivery_error = str(exc) or "registration_email_send_failed"
            product.record_surface_event(
                principal_id=_registration_principal_id(email),
                event_type="registration_email_failed",
                surface="register_start",
                actor=email,
                metadata={
                    "email": email,
                    "error": type(exc).__name__ if runtime_mode == "prod" else email_delivery_error,
                },
            )
    if runtime_mode == "prod" and email_delivery_status != "sent":
        raise HTTPException(status_code=503, detail="registration_email_delivery_unavailable")
    expose_local_verification = runtime_mode != "prod"
    return RegisterStartOut(
        email=email,
        verification_token=verification_token,
        verification_code=verification_code if expose_local_verification else "",
        magic_link_url=magic_link_url if expose_local_verification else "",
        expires_at=expires_at,
        workspace_name=_workspace_name_from_email(email),
        suggested_timezone=default_timezone_for_country("AT"),
        suggested_language="en",
        email_delivery_status=email_delivery_status,
        email_delivery_provider=email_delivery_provider,
        email_delivery_id=email_delivery_id,
        email_delivery_error=email_delivery_error if expose_local_verification else "",
    )


@register_router.post("/verify", response_model=RegisterVerifyOut)
def register_verify(
    body: RegisterVerifyIn,
    container: AppContainer = Depends(get_container),
    request: Request = None,
) -> dict[str, object]:
    payload = _verify_registration_payload(container=container, token=body.verification_token)
    if payload is None or not hmac.compare_digest(
        str(payload.get("token_kind") or "").strip(),
        "register_challenge",
    ):
        raise HTTPException(status_code=400, detail="registration_verification_invalid")
    provided_code = str(body.verification_code or "").strip()
    expected_code_digest = str(
        payload.get("verification_code_digest") or ""
    ).strip()
    provided_code_digest = _registration_code_digest(
        container=container,
        verification_code=provided_code,
    )
    legacy_expected_code = str(payload.get("verification_code") or "").strip()
    code_matches = bool(
        provided_code
        and (
            (
                expected_code_digest
                and hmac.compare_digest(
                    provided_code_digest,
                    expected_code_digest,
                )
            )
            or (
                legacy_expected_code
                and hmac.compare_digest(provided_code, legacy_expected_code)
            )
        )
    )
    if not code_matches:
        raise HTTPException(status_code=400, detail="registration_verification_code_invalid")
    try:
        propertyquarry_google_identity.consume_propertyquarry_identity_verification_challenge(
            token=body.verification_token,
            expires_at=int(payload.get("expires_at") or 0),
            database_url=str(
                getattr(container.settings, "database_url", "") or ""
            ).strip(),
        )
    except RuntimeError as exc:
        if str(exc) == "propertyquarry_identity_verification_replayed":
            raise HTTPException(
                status_code=409,
                detail="registration_verification_already_used",
            ) from exc
        raise
    email = str(payload.get("email") or "").strip().lower()
    principal_id = _registration_principal_id(email)
    workspace_name = str(body.workspace_name or "").strip() or _workspace_name_from_email(email)
    language = str(body.language or "").strip() or "en"
    timezone = str(body.timezone or "").strip() or default_timezone_for_country("AT")
    status = container.onboarding.start_workspace(
        principal_id=principal_id,
        workspace_name=workspace_name,
        workspace_mode="personal",
        region="",
        language=language,
        timezone=timezone,
        selected_channels=(),
    )
    google_return_to = "/register?ready=1&google_identity=confirmed"
    google_start_url = "/sign-in/google?" + urllib.parse.urlencode(
        {"return_to": google_return_to}
    )
    google_ready = propertyquarry_google_identity.propertyquarry_google_identity_configured()
    google_start: dict[str, object] = {
        "auth_url": google_start_url,
        "identity_only": True,
        "ready": google_ready,
        "start_url": google_start_url,
    }
    if not google_ready:
        google_start.update({"auth_url": "", "start_url": ""})
        try:
            propertyquarry_google_identity.load_propertyquarry_google_identity_config()
        except RuntimeError as exc:
            google_start.update(
                {
                    "error": str(exc or "google_oauth_propertyquarry_unavailable"),
                    "detail": "Google identity is not configured for PropertyQuarry on this host.",
                }
            )
    access = build_product_service(container).issue_workspace_access_session(
        principal_id=principal_id,
        email=email,
        role="principal",
        display_name=workspace_name,
        source_kind="register",
    )
    return {
        **status,
        "google_start": google_start,
        "access_url": str(access.get("access_url") or "").strip(),
        "access_token": str(access.get("access_token") or "").strip(),
        "access_expires_at": str(access.get("expires_at") or "").strip(),
    }


@router.post("/start", response_model=OnboardingStartOut)
def onboarding_start(
    body: OnboardingStartIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    return container.onboarding.start_workspace(
        principal_id=principal_id,
        workspace_name=body.workspace_name,
        workspace_mode=body.workspace_mode,
        region=body.region,
        language=body.language,
        timezone=body.timezone,
        selected_channels=tuple(body.selected_channels),
    )


@router.post("/flagship/start", response_model=OnboardingFlagshipStartOut)
def onboarding_flagship_start(
    body: OnboardingFlagshipStartIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    return container.onboarding.start_flagship(
        principal_id=principal_id,
        workspace_name=body.workspace_name,
        workspace_mode=body.workspace_mode,
        region=body.region,
        language=body.language,
        timezone=body.timezone,
        selected_channels=tuple(body.selected_channels),
        scope_bundle=body.scope_bundle,
        telegram_ref=body.telegram_ref,
        telegram_identity_mode=body.telegram_identity_mode,
        telegram_history_mode=body.telegram_history_mode,
        telegram_assistant_surfaces=tuple(body.telegram_assistant_surfaces),
        whatsapp_export_label=body.whatsapp_export_label,
        whatsapp_include_media=bool(body.whatsapp_include_media),
    )


@router.get("/status", response_model=OnboardingStartOut)
def onboarding_status(
    principal_id: str | None = None,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    resolved = resolve_principal_id(principal_id, context)
    return container.onboarding.status(principal_id=resolved)


@router.post("/property-search/preferences", response_model=OnboardingPropertySearchPreferencesOut)
def onboarding_upsert_property_search_preferences(
    body: OnboardingPropertySearchPreferencesIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(None, context)
    preferences_payload = dict(body.model_dump())
    explicit_fields = set(getattr(body, "model_fields_set", set()) or set())
    for implicit_default_key in ("language_code",):
        if implicit_default_key not in explicit_fields:
            preferences_payload.pop(implicit_default_key, None)
    return container.onboarding.upsert_property_search_preferences(
        principal_id=principal_id,
        property_search_preferences_json=preferences_payload,
    )


@router.get("/property-search/preferences", response_model=OnboardingPropertySearchPreferencesOut)
def onboarding_get_property_search_preferences(
    principal_id: str | None = None,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    resolved = resolve_principal_id(principal_id, context)
    return container.onboarding.status(principal_id=resolved)


@router.post("/property-search/agents/{agent_id}", response_model=OnboardingPropertySearchPreferencesOut)
def onboarding_update_property_search_agent(
    agent_id: str,
    body: OnboardingPropertySearchAgentUpdateIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(None, context)
    try:
        return container.onboarding.update_property_search_agent(
            principal_id=principal_id,
            agent_id=agent_id,
            action=body.action,
            patch=body.patch,
        )
    except ValueError as exc:
        detail = str(exc) or "property_search_agent_update_failed"
        status_code = 404 if detail == "property_search_agent_not_found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc


@router.post("/google/start", response_model=OnboardingGoogleStartOut)
def onboarding_google_start(
    body: OnboardingGoogleStartIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    return container.onboarding.start_google(
        principal_id=principal_id,
        scope_bundle=body.scope_bundle,
    )


@router.post("/telegram/start", response_model=OnboardingTelegramStartOut)
def onboarding_telegram_start(
    body: OnboardingTelegramStartIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    return container.onboarding.start_telegram(
        principal_id=principal_id,
        telegram_ref=body.telegram_ref,
        identity_mode=body.identity_mode,
        history_mode=body.history_mode,
        assistant_surfaces=tuple(body.assistant_surfaces),
    )


@router.post("/telegram/link-bot", response_model=OnboardingTelegramBotOut)
def onboarding_telegram_link_bot(
    body: OnboardingTelegramBotIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    return container.onboarding.link_telegram_bot(
        principal_id=principal_id,
        bot_handle=body.bot_handle,
        install_surfaces=tuple(body.install_surfaces),
        default_chat_ref=body.default_chat_ref,
    )


@router.post("/telegram/bind-chat", response_model=OnboardingTelegramBotOut)
def onboarding_telegram_bind_chat(
    body: OnboardingTelegramBindChatIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    try:
        return container.onboarding.bind_telegram_chat(
            principal_id=principal_id,
            chat_ref=body.chat_ref,
            bot_handle=body.bot_handle,
            bot_key=body.bot_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/whatsapp/start-business", response_model=OnboardingWhatsappBusinessOut)
def onboarding_whatsapp_start_business(
    body: OnboardingWhatsappBusinessIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    return container.onboarding.start_whatsapp_business(
        principal_id=principal_id,
        phone_number=body.phone_number,
        business_name=body.business_name,
        import_history_now=body.import_history_now,
    )


@router.post("/whatsapp/import-export", response_model=OnboardingWhatsappExportOut)
def onboarding_whatsapp_import_export(
    body: OnboardingWhatsappExportIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    return container.onboarding.import_whatsapp_export(
        principal_id=principal_id,
        export_label=body.export_label,
        selected_chat_labels=tuple(body.selected_chat_labels),
        include_media=body.include_media,
    )


@router.post("/whatsapp/import-export/ack", response_model=OnboardingWhatsappExportAckOut)
def onboarding_whatsapp_import_export_ack(
    body: OnboardingWhatsappExportAckIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    return container.onboarding.acknowledge_whatsapp_export_import(
        principal_id=principal_id,
        binding_id=body.binding_id,
        imported_message_count=body.imported_message_count,
        status=body.status,
    )


@router.post("/finalize", response_model=OnboardingStartOut)
def onboarding_finalize(
    body: OnboardingFinalizeIn,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    principal_id = resolve_principal_id(body.principal_id, context)
    return container.onboarding.finalize(
        principal_id=principal_id,
        retention_mode=body.retention_mode,
        metadata_only_channels=tuple(body.metadata_only_channels),
        allow_drafts=body.allow_drafts,
        allow_action_suggestions=body.allow_action_suggestions,
        allow_auto_briefs=body.allow_auto_briefs,
        auto_brief_cadence=body.auto_brief_cadence,
        auto_brief_delivery_time_local=body.auto_brief_delivery_time_local,
        auto_brief_quiet_hours_start=body.auto_brief_quiet_hours_start,
        auto_brief_quiet_hours_end=body.auto_brief_quiet_hours_end,
        auto_brief_recipient_email=body.auto_brief_recipient_email,
        auto_brief_delivery_channel=body.auto_brief_delivery_channel,
    )
