from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from app.domain.models import ConnectorBinding, OnboardingState
from app.property_distance_preferences import (
    normalize_property_distance_importance,
    normalize_property_distance_preference_pairs,
    property_distance_importance_key,
    property_distance_preference_keys,
)
from app.repositories.onboarding_state import InMemoryOnboardingStateRepository, OnboardingStateRepository
from app.repositories.onboarding_state_postgres import PostgresOnboardingStateRepository
from app.services.assistant_onboarding_service import AssistantOnboardingService
from app.services.google_oauth import GOOGLE_PROVIDER_KEY, google_scope_bundle_details
try:
    from app.services.google_oauth import google_bundle_supports_workspace_sync
except ImportError:
    def google_bundle_supports_workspace_sync(
        bundle: str | None = None,
        *,
        scopes: tuple[str, ...] | list[str] | None = None,
    ) -> bool:
        effective_scopes = tuple(str(item or "").strip() for item in (scopes or ()) if str(item or "").strip())
        if not effective_scopes and bundle:
            bundle_details = google_scope_bundle_details(bundle)
            effective_scopes = tuple(str(item or "").strip() for item in list(bundle_details.get("scopes") or []) if str(item or "").strip())
        supported_signal_scopes = {
            "https://www.googleapis.com/auth/gmail.metadata",
            "https://www.googleapis.com/auth/gmail.modify",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/contacts.readonly",
            "https://www.googleapis.com/auth/drive.metadata.readonly",
        }
        return any(scope in supported_signal_scopes for scope in effective_scopes)
from app.services.memory_runtime import MemoryRuntimeService
from app.services.property_billing import (
    normalize_property_commercial,
    normalize_property_plan_key,
    property_commercial_snapshot,
    property_plan_has_unlimited_provider_results,
)
from app.services.property_market_catalog import (
    filter_selectable_property_platform_details,
    normalize_country_code,
    normalize_language_code,
    normalize_listing_mode,
    normalize_property_platform,
    normalize_property_search_preferences as normalize_property_search_preferences_contract,
    normalize_property_type_values,
    provider_options,
)
from app.services.propertyquarry_teable_projection import fetch_propertyquarry_subscription_fields
from app.services.provider_registry import ProviderRegistryService
from app.services.telegram_delivery import _telegram_binding_principal_candidates
from app.services.tool_runtime import ToolRuntimeService
from app.services.telegram_onboarding_service import (
    TELEGRAM_IDENTITY_CONNECTOR,
    TELEGRAM_OFFICIAL_BOT_CONNECTOR,
)
from app.services.whatsapp_onboarding_service import (
    WHATSAPP_BUSINESS_CONNECTOR,
    WHATSAPP_EXPORT_CONNECTOR,
)
from app.settings import Settings, ensure_storage_fallback_allowed, get_settings

GOOGLE_ONBOARDING_BUNDLE_ALIASES = {
    "identity": "identity",
    "send": "send",
    "verify": "verify",
    "all": "all",
    "core": "core",
    "photos": "photos",
    "core_photos": "core_photos",
    "full_workspace": "full_workspace",
    "full_workspace_photos": "full_workspace_photos",
}

_GOOGLE_OAUTH_MISSING_CONFIG_HELP: dict[str, str] = {
    "google_oauth_client_id_missing": "Set EA_GOOGLE_OAUTH_CLIENT_ID and EA_GOOGLE_OAUTH_CLIENT_SECRET.",
    "google_oauth_client_secret_missing": "Set EA_GOOGLE_OAUTH_CLIENT_ID and EA_GOOGLE_OAUTH_CLIENT_SECRET.",
    "google_oauth_redirect_uri_missing": "Set EA_GOOGLE_OAUTH_REDIRECT_URI.",
    "google_oauth_state_secret_missing": "Set EA_GOOGLE_OAUTH_STATE_SECRET.",
    "google_oauth_provider_secret_key_missing": "Set EA_PROVIDER_SECRET_KEY.",
}


def _google_oauth_missing_config_detail(error_code: str) -> str:
    normalized = str(error_code or "").strip()
    return _GOOGLE_OAUTH_MISSING_CONFIG_HELP.get(
        normalized,
        normalized or "Google OAuth is not configured for this host.",
    )


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _property_commercial_has_billing_evidence(commercial: dict[str, object] | None) -> bool:
    payload = dict(commercial or {})
    if any(
        payload.get(key)
        for key in (
            "last_order_id",
            "last_capture_id",
            "pending_order_id",
            "pending_plan_key",
            "captured_at",
            "plan_source",
            "last_billing_event_id",
            "last_billing_event_type",
        )
    ):
        return True
    billing_events = payload.get("billing_events_json")
    return isinstance(billing_events, list) and bool(billing_events)


def _property_commercial_is_paid(commercial: dict[str, object] | None) -> bool:
    return normalize_property_plan_key(dict(commercial or {}).get("active_plan_key")) in {"plus", "agent"}


def _property_commercial_restore_candidate_from_teable(fields: dict[str, object] | None) -> dict[str, object]:
    payload = dict(fields or {})
    commercial_json = payload.get("commercial_json")
    if isinstance(commercial_json, str) and commercial_json.strip():
        try:
            parsed = json.loads(commercial_json)
            if isinstance(parsed, dict):
                payload = {**parsed, **payload}
        except Exception:
            pass
    candidate = normalize_property_commercial(
        {
            "active_plan_key": payload.get("active_plan_key") or payload.get("current_plan_key"),
            "status": payload.get("status"),
            "active_until": payload.get("active_until"),
            "last_order_id": payload.get("last_order_id"),
            "last_capture_id": payload.get("last_capture_id"),
            "last_payment_status": payload.get("last_payment_status"),
            "last_payment_amount_eur": payload.get("last_payment_amount_eur"),
            "captured_at": payload.get("captured_at"),
            "pending_plan_key": payload.get("pending_plan_key"),
            "plan_source": payload.get("plan_source") or "teable_projection_restore",
            "billing_events_json": payload.get("billing_events_json") or [],
        }
    )
    return candidate if _property_commercial_is_paid(candidate) else {}

WORKSPACE_MODE_ALIASES = {
    "personal": "personal",
    "team": "team",
    "executive_ops": "executive_ops",
}

ASSISTANT_MODE_CATALOG: tuple[dict[str, str], ...] = (
    {
        "key": "personal",
        "label": "Personal",
        "summary": "One private PropertyQuarry account for searches, shortlist review, and saved feedback.",
    },
    {
        "key": "team",
        "label": "Shared workspace",
        "summary": "A shared PropertyQuarry account for shortlist review, research follow-ups, and coordinated decisions.",
    },
    {
        "key": "executive_ops",
        "label": "Concierge search",
        "summary": "A heavier property search with deeper research, more follow-up, and stronger sharing controls.",
    },
)

FEATURED_DOMAIN_CATALOG: tuple[dict[str, str], ...] = ()

AUTO_BRIEF_CADENCE_ALIASES = {
    "daily": "daily_morning",
    "daily_morning": "daily_morning",
    "weekdays": "weekdays_morning",
    "weekdays_morning": "weekdays_morning",
}

AUTO_BRIEF_DELIVERY_CHANNELS = {"email"}
AUTO_BRIEF_RECIPIENT_REF = "morning_memo_primary"
DEFAULT_AUTO_BRIEF_CADENCE = "daily_morning"
DEFAULT_AUTO_BRIEF_DELIVERY_TIME_LOCAL = "08:00"
DEFAULT_AUTO_BRIEF_QUIET_HOURS_START = "20:00"
DEFAULT_AUTO_BRIEF_QUIET_HOURS_END = "07:00"
DEFAULT_AUTO_BRIEF_DELIVERY_WINDOW_MINUTES = 120
DEFAULT_AUTO_BRIEF_RETRY_AFTER_MINUTES = 60
PROPERTY_NOTIFICATION_CHANNELS = {"email", "telegram", "whatsapp"}
PROPERTY_NOTIFICATION_CHANNEL_LABELS = {
    "email": "Email",
    "telegram": "PropertyQuarry bot",
    "whatsapp": "WhatsApp",
}
PROPERTY_WHATSAPP_AI_SUPPORT_PURPOSE = "ai_support_only"
PROPERTY_WHATSAPP_AI_SUPPORT_OPENING_PROMPT = (
    "Ask what questions the user has about PropertyQuarry before giving property guidance."
)
ASSISTANT_WHATSAPP_AI_SUPPORT_PURPOSE = "ai_support_only"
ASSISTANT_WHATSAPP_AI_SUPPORT_OPENING_PROMPT = (
    "Ask what questions the user has about the Executive Assistant before giving workspace guidance."
)
ASSISTANT_NOTIFICATION_CHANNELS = {"email", "telegram", "whatsapp"}


def normalize_property_notification_channel(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "mail": "email",
        "e_mail": "email",
        "e-mail": "email",
        "telegram_bot": "telegram",
        "propertyquarry_bot": "telegram",
        "propertyquarry_telegram": "telegram",
        "heyy": "whatsapp",
        "whats_app": "whatsapp",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in PROPERTY_NOTIFICATION_CHANNELS:
        raise ValueError("property_notification_channel_invalid")
    return normalized


def normalize_property_notification_channels(
    values: object,
    *,
    fallback: object = "email",
) -> tuple[str, ...]:
    raw_values: list[object] = []
    if isinstance(values, str):
        raw_values = [item for item in values.replace(";", ",").split(",")]
    elif isinstance(values, (list, tuple, set)):
        raw_values = list(values)
    elif values is not None:
        raw_values = [values]
    normalized: list[str] = []
    for raw_value in raw_values:
        if not str(raw_value or "").strip():
            continue
        channel = normalize_property_notification_channel(raw_value)
        if channel not in normalized:
            normalized.append(channel)
    if not normalized and fallback is not None:
        channel = normalize_property_notification_channel(fallback)
        normalized.append(channel)
    return tuple(normalized)


def normalize_property_whatsapp_ai_support_phone(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 7:
        raise ValueError("property_whatsapp_ai_support_phone_invalid")
    return f"+{digits}"


def normalize_assistant_whatsapp_ai_support_phone(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 7:
        raise ValueError("assistant_whatsapp_ai_support_phone_invalid")
    return f"+{digits}"


def _clean_telegram_bot_handle(value: object) -> str:
    normalized = str(value or "").strip().lstrip("@")
    return "".join(ch for ch in normalized if ch.isalnum() or ch == "_")[:64]


def _propertyquarry_telegram_bot_public_profile() -> dict[str, object]:
    explicit_handle = _clean_telegram_bot_handle(
        os.getenv("PROPERTYQUARRY_TELEGRAM_BOT_HANDLE")
        or os.getenv("EA_PROPERTYQUARRY_TELEGRAM_BOT_HANDLE")
        or os.getenv("EA_TELEGRAM_BOT_HANDLE")
    )
    raw_registry = str(os.getenv("EA_TELEGRAM_BOT_REGISTRY_JSON") or "").strip()
    selected_key = "default"
    selected_handle = explicit_handle
    if raw_registry:
        try:
            parsed = json.loads(raw_registry)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            preferred_keys = ("propertyquarry", "default", *tuple(str(key or "").strip() for key in parsed.keys()))
            for key in preferred_keys:
                row = parsed.get(key)
                if not isinstance(row, dict):
                    continue
                handle = _clean_telegram_bot_handle(row.get("handle"))
                if handle:
                    selected_key = str(key or "default").strip() or "default"
                    selected_handle = explicit_handle or handle
                    break
    return {
        "label": "PropertyQuarry bot",
        "bot_key": selected_key,
        "handle": selected_handle,
        "display_handle": f"@{selected_handle}" if selected_handle else "",
        "connect_url": f"https://t.me/{selected_handle}" if selected_handle else "",
    }


PROPERTY_SEARCH_RAW_PREFERENCES_MAX_DEPTH = 256


def flatten_property_search_preferences_snapshot(
    value: dict[str, object] | None,
    *,
    max_depth: int = PROPERTY_SEARCH_RAW_PREFERENCES_MAX_DEPTH,
) -> dict[str, object]:
    """Flatten bounded legacy ``raw_preferences`` snapshots.

    Older full-state saves nested the preceding snapshot once per write.  Walk
    that chain iteratively, merge deepest-to-outermost so newer values win, and
    never return the structural key itself.  The depth bound and identity set
    keep malformed in-process inputs from causing unbounded work; JSON-backed
    production values cannot contain object cycles.
    """

    outer = dict(value or {})
    nested: object = outer.pop("raw_preferences", None)
    layers: list[dict[str, object]] = [outer]
    seen: set[int] = set()
    bounded_depth = max(0, int(max_depth or 0))
    depth = 0
    while isinstance(nested, dict) and depth < bounded_depth:
        object_id = id(nested)
        if object_id in seen:
            raise ValueError("property_search_raw_preferences_object_cycle")
        seen.add(object_id)
        layer = dict(nested)
        nested = layer.pop("raw_preferences", None)
        layers.append(layer)
        depth += 1
    if isinstance(nested, dict):
        raise ValueError("property_search_raw_preferences_depth_limit_exceeded")
    flattened: dict[str, object] = {}
    for layer in reversed(layers):
        flattened.update(layer)
    flattened.pop("raw_preferences", None)
    return flattened


class OnboardingService(AssistantOnboardingService):
    def __init__(
        self,
        *,
        onboarding_repo: OnboardingStateRepository,
        provider_registry: ProviderRegistryService,
        tool_runtime: ToolRuntimeService,
        memory_runtime: MemoryRuntimeService,
        settings: Settings,
    ) -> None:
        super().__init__(
            onboarding_repo=onboarding_repo,
            provider_registry=provider_registry,
            tool_runtime=tool_runtime,
            settings=settings,
        )
        self._memory_runtime = memory_runtime

    def erase_principal(self, principal_id: str) -> bool:
        method = getattr(self._repo, "erase_principal", None)
        return bool(method(str(principal_id or "").strip())) if callable(method) else False

    def _preferred_google_binding(self, *, principal_id: str):  # type: ignore[no-untyped-def]
        google_binding = self._provider_registry.get_persisted_binding_record(
            binding_id=f"{principal_id}:{GOOGLE_PROVIDER_KEY}",
            principal_id=principal_id,
        )
        if google_binding is not None:
            status = str(getattr(google_binding, "status", "") or "").strip().lower()
            token_status = str(dict(getattr(google_binding, "auth_metadata_json", {}) or {}).get("token_status") or "").strip().lower()
            if status == "enabled" and token_status != "revoked":
                return google_binding
        for binding in self._provider_registry.list_persisted_binding_records(principal_id=principal_id, limit=100):
            if str(getattr(binding, "provider_key", "") or "").strip().lower() != GOOGLE_PROVIDER_KEY:
                continue
            status = str(getattr(binding, "status", "") or "").strip().lower()
            token_status = str(dict(getattr(binding, "auth_metadata_json", {}) or {}).get("token_status") or "").strip().lower()
            if status == "enabled" and token_status != "revoked":
                return binding
        return google_binding

    def start_workspace(
        self,
        *,
        principal_id: str,
        workspace_name: str,
        workspace_mode: str,
        region: str,
        language: str,
        timezone: str,
        selected_channels: tuple[str, ...],
    ) -> dict[str, object]:
        normalized_channels = self._normalize_channels(selected_channels)
        normalized_workspace_mode = self._normalize_workspace_mode(workspace_mode)
        state = self._repo.get_for_principal(principal_id)
        channel_preferences = dict(state.channel_preferences_json if state is not None else {})
        for channel in normalized_channels:
            channel_preferences.setdefault(channel, {})
        saved = self._repo.upsert_state(
            principal_id=principal_id,
            onboarding_id=state.onboarding_id if state is not None else None,
            workspace_name=workspace_name,
            workspace_mode=normalized_workspace_mode,
            region=region,
            language=language,
            timezone=timezone,
            selected_channels=normalized_channels,
            property_search_preferences_json=dict(state.property_search_preferences_json if state is not None else {}),
            channel_preferences_json=channel_preferences,
            privacy_preferences_json=dict(state.privacy_preferences_json if state is not None else {}),
            brief_preview_json={},
            status="started",
        )
        return self.status(principal_id=principal_id, state_override=saved)

    @staticmethod
    def _merge_current_property_search_agent(
        *,
        existing_preferences: dict[str, object],
        normalized_preferences: dict[str, object],
    ) -> dict[str, object]:
        existing_agents = OnboardingService._normalize_property_search_agents(
            existing_preferences,
            active_agent_id=str(existing_preferences.get("active_search_agent_id") or "").strip(),
            enforce_plan_limit=False,
        )
        current_agent = OnboardingService._normalize_property_search_agent(
            normalized_preferences,
            fallback=normalized_preferences,
        )
        active_agent_id = str(normalized_preferences.get("active_search_agent_id") or "").strip()
        if not active_agent_id:
            active_agent_id = str(existing_preferences.get("active_search_agent_id") or "").strip()
        if active_agent_id:
            current_agent["agent_id"] = active_agent_id
        current_agent["is_active"] = True
        merged: list[dict[str, object]] = []
        replaced = False
        for agent in existing_agents:
            if str(agent.get("agent_id") or "") == str(current_agent.get("agent_id") or ""):
                merged.append(current_agent)
                replaced = True
            else:
                agent["is_active"] = False
                merged.append(agent)
        if not replaced:
            merged.append(current_agent)
        normalized_preferences["search_agents"] = OnboardingService._normalize_property_search_agents(
            {
                **normalized_preferences,
                "search_agents": merged,
            },
            active_agent_id=str(current_agent.get("agent_id") or ""),
            enforce_plan_limit=False,
        )
        normalized_preferences["active_search_agent_id"] = str(current_agent.get("agent_id") or "")
        return normalized_preferences

    def _registration_principal_alias(self, principal_id: str) -> str:
        normalized = str(principal_id or "").strip().lower()
        prefix = "cf-email:"
        if not normalized.startswith(prefix):
            return ""
        email = normalized[len(prefix) :].strip()
        if not email or "@" not in email:
            return ""
        digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]
        return f"user-{digest}"

    def _bridge_browser_principal_state(self, principal_id: str) -> OnboardingState | None:
        existing = self._repo.get_for_principal(principal_id)
        if existing is not None:
            return existing
        alias_principal_id = self._registration_principal_alias(principal_id)
        if not alias_principal_id:
            return None
        aliased = self._repo.get_for_principal(alias_principal_id)
        if aliased is None:
            return None
        return self._repo.upsert_state(
            principal_id=principal_id,
            workspace_name=aliased.workspace_name,
            workspace_mode=aliased.workspace_mode,
            region=aliased.region,
            language=aliased.language,
            timezone=aliased.timezone,
            selected_channels=aliased.selected_channels,
            property_search_preferences_json=dict(aliased.property_search_preferences_json),
            privacy_preferences_json=dict(aliased.privacy_preferences_json),
            channel_preferences_json=dict(aliased.channel_preferences_json),
            brief_preview_json=dict(aliased.brief_preview_json),
            status=aliased.status,
        )

    def upsert_property_search_preferences(
        self,
        *,
        principal_id: str,
        property_search_preferences_json: dict[str, object],
    ) -> dict[str, object]:
        state = self._ensure_state(principal_id)
        normalized_preferences = self._normalize_property_search_preferences(property_search_preferences_json)
        raw_incoming_preferences = dict(property_search_preferences_json or {})
        if "language_code" not in raw_incoming_preferences and "language" not in raw_incoming_preferences:
            normalized_preferences["language_code"] = normalize_language_code(
                None,
                country_code=str(normalized_preferences.get("country_code") or "AT"),
            )
        existing_preferences = dict(state.property_search_preferences_json or {})
        existing_raw_preferences = dict(existing_preferences.get("raw_preferences") or {}) if isinstance(existing_preferences.get("raw_preferences"), dict) else {}
        incoming_commercial = dict(normalized_preferences.get("property_commercial") or {}) if isinstance(normalized_preferences.get("property_commercial"), dict) else {}
        existing_commercial = dict(existing_preferences.get("property_commercial") or {}) if isinstance(existing_preferences.get("property_commercial"), dict) else {}
        incoming_has_explicit_commercial = isinstance(raw_incoming_preferences.get("property_commercial"), dict) and bool(raw_incoming_preferences.get("property_commercial"))
        incoming_is_empty_free = (
            incoming_has_explicit_commercial
            and not _property_commercial_is_paid(incoming_commercial)
            and not _property_commercial_has_billing_evidence(incoming_commercial)
        )
        if not incoming_has_explicit_commercial and existing_commercial:
            normalized_preferences["property_commercial"] = existing_commercial
        elif incoming_is_empty_free and _property_commercial_is_paid(existing_commercial):
            normalized_preferences["property_commercial"] = existing_commercial
        raw_preferences = dict(normalized_preferences.get("raw_preferences") or {}) if isinstance(normalized_preferences.get("raw_preferences"), dict) else {}
        incoming_raw_commercial = dict(raw_preferences.get("property_commercial") or {}) if isinstance(raw_preferences.get("property_commercial"), dict) else {}
        existing_raw_commercial = dict(existing_raw_preferences.get("property_commercial") or {}) if isinstance(existing_raw_preferences.get("property_commercial"), dict) else {}
        if not incoming_has_explicit_commercial and not incoming_raw_commercial and existing_raw_commercial:
            raw_preferences["property_commercial"] = existing_raw_commercial
            normalized_preferences["raw_preferences"] = raw_preferences
        elif incoming_is_empty_free and _property_commercial_is_paid(existing_commercial) and existing_raw_commercial:
            raw_preferences["property_commercial"] = existing_raw_commercial
            normalized_preferences["raw_preferences"] = raw_preferences
        existing_has_saved_agents = isinstance(existing_preferences.get("search_agents"), (list, tuple)) and bool(existing_preferences.get("search_agents"))
        if existing_has_saved_agents and not isinstance(raw_incoming_preferences.get("search_agents"), (list, tuple)):
            normalized_preferences = self._merge_current_property_search_agent(
                existing_preferences=existing_preferences,
                normalized_preferences=normalized_preferences,
            )
        saved = self._repo.upsert_state(
            principal_id=state.principal_id,
            onboarding_id=state.onboarding_id,
            workspace_name=state.workspace_name,
            workspace_mode=state.workspace_mode,
            region=state.region,
            language=state.language,
            timezone=state.timezone,
            selected_channels=state.selected_channels,
            property_search_preferences_json=normalized_preferences,
            privacy_preferences_json=dict(state.privacy_preferences_json),
            channel_preferences_json=dict(state.channel_preferences_json),
            brief_preview_json=dict(state.brief_preview_json),
            status=state.status,
        )
        return self.status(principal_id=principal_id, state_override=saved)

    def _restore_property_commercial_from_teable(
        self,
        *,
        principal_id: str,
        state: OnboardingState | None,
    ) -> OnboardingState | None:
        if state is None:
            return None
        current_preferences = dict(state.property_search_preferences_json or {})
        current_commercial = (
            dict(current_preferences.get("property_commercial") or {})
            if isinstance(current_preferences.get("property_commercial"), dict)
            else {}
        )
        if _property_commercial_is_paid(current_commercial):
            return state
        remote_fields = fetch_propertyquarry_subscription_fields(principal_id=principal_id)
        remote_commercial = _property_commercial_restore_candidate_from_teable(remote_fields)
        if not remote_commercial:
            return state
        remote_active_until = _parse_utc(remote_commercial.get("active_until"))
        if remote_active_until is not None and remote_active_until <= datetime.now(timezone.utc):
            return state
        merged_preferences = dict(current_preferences)
        merged_preferences["property_commercial"] = remote_commercial
        raw_preferences = dict(merged_preferences.get("raw_preferences") or {}) if isinstance(merged_preferences.get("raw_preferences"), dict) else {}
        raw_preferences["property_commercial"] = remote_commercial
        merged_preferences["raw_preferences"] = raw_preferences
        return self._repo.upsert_state(
            principal_id=state.principal_id,
            onboarding_id=state.onboarding_id,
            workspace_name=state.workspace_name,
            workspace_mode=state.workspace_mode,
            region=state.region,
            language=state.language,
            timezone=state.timezone,
            selected_channels=state.selected_channels,
            property_search_preferences_json=merged_preferences,
            privacy_preferences_json=dict(state.privacy_preferences_json),
            channel_preferences_json=dict(state.channel_preferences_json),
            brief_preview_json=dict(state.brief_preview_json),
            status=state.status,
        )

    def update_property_search_agent(
        self,
        *,
        principal_id: str,
        agent_id: str,
        action: str,
        patch: dict[str, object] | None = None,
    ) -> dict[str, object]:
        state = self._ensure_state(principal_id)
        preferences = dict(state.property_search_preferences_json or {})
        agents = self._normalize_property_search_agents(
            preferences,
            active_agent_id=str(preferences.get("active_search_agent_id") or "").strip(),
            # Management operates on the durable collection. Subscription
            # limits decide which agents may execute; they must not make
            # already-saved searches unreachable for pause, edit, or delete.
            enforce_plan_limit=False,
        )
        normalized_agent_id = str(agent_id or "").strip()
        if normalized_agent_id in {"", "current"}:
            normalized_agent_id = str(agents[0].get("agent_id") or "current")
        matched_index = next(
            (index for index, agent in enumerate(agents) if str(agent.get("agent_id") or "") == normalized_agent_id),
            -1,
        )
        if matched_index < 0:
            raise ValueError("property_search_agent_not_found")
        normalized_action = str(action or "").strip().lower()
        agent = dict(agents[matched_index])
        if normalized_action in {"pause", "disable"}:
            agent["enabled"] = False
            agent["status"] = "paused"
        elif normalized_action in {"resume", "enable"}:
            agent["enabled"] = True
            agent["status"] = "active"
            preferences["active_search_agent_id"] = str(agent.get("agent_id") or normalized_agent_id)
        elif normalized_action in {"load", "select"}:
            loaded_preferences = (
                dict(agent.get("preferences_json") or {})
                if isinstance(agent.get("preferences_json"), dict)
                else {}
            )
            if not loaded_preferences:
                loaded_preferences = {
                    "country_code": agent.get("country_code"),
                    "region_code": agent.get("region_code"),
                    "location_query": agent.get("location_query"),
                    "listing_mode": agent.get("listing_mode"),
                    "property_type": agent.get("property_type"),
                    "selected_platforms": list(agent.get("selected_platforms") or []),
                    "search_agent_enabled": bool(agent.get("enabled")),
                    "search_agent_duration_days": agent.get("duration_days"),
                    "search_agent_notification_limit": agent.get("notification_limit"),
                    "search_agent_notification_period": agent.get("notification_period"),
                }
            response = self.status(principal_id=principal_id)
            response["loaded_search_agent_id"] = str(agent.get("agent_id") or normalized_agent_id)
            response["loaded_property_search_preferences"] = loaded_preferences
            return response
        elif normalized_action in {"delete", "remove"}:
            agents.pop(matched_index)
            preferences["search_agents"] = agents
            if str(preferences.get("active_search_agent_id") or "") == normalized_agent_id:
                preferences["active_search_agent_id"] = str(agents[0].get("agent_id") or "") if agents else ""
            return self.upsert_property_search_preferences(
                principal_id=principal_id,
                property_search_preferences_json=preferences,
            )
        elif normalized_action in {"duplicate", "copy"}:
            agent_limit = self._property_search_agent_limit(preferences)
            if agent_limit > 0 and len(agents) >= agent_limit:
                raise ValueError(f"property_search_agent_limit_reached:{agent_limit}")
            duplicate = dict(agent)
            seed = f"{principal_id}|{agent.get('agent_id')}|{len(agents) + 1}"
            duplicate["agent_id"] = f"agent-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"
            duplicate["name"] = f"{str(agent.get('name') or 'Saved search').strip()} copy"
            duplicate["enabled"] = False
            duplicate["status"] = "paused"
            agents.append(duplicate)
            preferences["search_agents"] = agents
            return self.upsert_property_search_preferences(
                principal_id=principal_id,
                property_search_preferences_json=preferences,
            )
        elif normalized_action in {"save", "update"}:
            for key, value in dict(patch or {}).items():
                if key in {
                    "name",
                    "location_query",
                    "country_code",
                    "region_code",
                    "listing_mode",
                    "property_type",
                    "notification_limit",
                    "notification_period",
                    "duration_days",
                    "selected_platforms",
                    "last_run_at",
                    "next_run_at",
                    "sent_in_current_window",
                    "preferences_json",
                }:
                    agent[key] = value
        else:
            raise ValueError("unsupported_property_search_agent_action")
        agents[matched_index] = agent
        preferences["search_agents"] = agents
        return self.upsert_property_search_preferences(
            principal_id=principal_id,
            property_search_preferences_json=preferences,
        )

    def start_flagship(
        self,
        *,
        principal_id: str,
        workspace_name: str,
        workspace_mode: str,
        region: str,
        language: str,
        timezone: str,
        selected_channels: tuple[str, ...],
        scope_bundle: str,
        telegram_ref: str,
        telegram_identity_mode: str,
        telegram_history_mode: str,
        telegram_assistant_surfaces: tuple[str, ...],
        whatsapp_export_label: str,
        whatsapp_include_media: bool,
    ) -> dict[str, object]:
        normalized_channels = self._normalize_channels(selected_channels)
        selected_channels_set = set(normalized_channels)
        selected_channels_list = list(dict.fromkeys(normalized_channels))
        status = self.start_workspace(
            principal_id=principal_id,
            workspace_name=workspace_name,
            workspace_mode=workspace_mode,
            region=region,
            language=language,
            timezone=timezone,
            selected_channels=normalized_channels,
        )
        google_start: dict[str, object] = {}
        telegram_start: dict[str, object] = {}
        whatsapp_export: dict[str, object] = {}

        if "google" in selected_channels_set:
            status = self.start_google(
                principal_id=principal_id,
                scope_bundle=scope_bundle,
                browser_source="flagship",
            )
            google_start = dict(status.get("google_start") or {})
        if "telegram" in selected_channels_set:
            status = self.start_telegram(
                principal_id=principal_id,
                telegram_ref=telegram_ref,
                identity_mode=telegram_identity_mode,
                history_mode=telegram_history_mode,
                assistant_surfaces=telegram_assistant_surfaces,
            )
            telegram_start = dict(status.get("telegram_start") or {})
        if "whatsapp" in selected_channels_set:
            export_label = str(whatsapp_export_label or "").strip() or f"{workspace_name} Flagship Export"
            status = self.import_whatsapp_export(
                principal_id=principal_id,
                export_label=export_label,
                selected_chat_labels=(),
                include_media=bool(whatsapp_include_media),
            )
            whatsapp_export = dict(status.get("whatsapp_export") or {})

        if google_start:
            status["google_start"] = google_start
        if telegram_start:
            status["telegram_start"] = telegram_start
        if whatsapp_export:
            status["whatsapp_export"] = whatsapp_export

        status["flagship_start"] = {
            "profile": "executive_flagship",
            "selected_channels": selected_channels_list,
            "google_bundle": scope_bundle if "google" in selected_channels_set else "",
            "telegram_started": "telegram" in selected_channels_set,
            "whatsapp_export_started": "whatsapp" in selected_channels_set,
            "stage": (
                "ready_for_activation"
                if (
                    "google" in selected_channels_set
                    and str(
                        dict((status.get("channels", {}) or {}).get("google") or {}).get("status") or ""
                    ).lower()
                    == "ready_to_connect"
                )
                else "partial"
            ),
        }
        return status

    def start_google(
        self,
        *,
        principal_id: str,
        scope_bundle: str,
        redirect_uri_override: str | None = None,
        return_to: str | None = None,
        browser_source: str | None = None,
    ) -> dict[str, object]:
        requested_bundle = str(scope_bundle or "identity").strip().lower() or "identity"
        if requested_bundle not in GOOGLE_ONBOARDING_BUNDLE_ALIASES:
            raise RuntimeError("onboarding_google_scope_bundle_invalid")
        state = self._ensure_state(principal_id)
        google_pref = dict((state.channel_preferences_json or {}).get("google") or {})
        google_pref["requested_bundle"] = requested_bundle
        oauth_bundle = GOOGLE_ONBOARDING_BUNDLE_ALIASES[requested_bundle]
        bundle_details = google_scope_bundle_details(oauth_bundle)
        google_pref["oauth_bundle"] = oauth_bundle
        try:
            packet = self._google_oauth.build_start(
                principal_id=principal_id,
                scope_bundle=oauth_bundle,
                redirect_uri_override=redirect_uri_override,
                return_to=return_to,
                browser_source=browser_source,
            )
            google_pref["status"] = "ready_to_connect"
            google_pref["requested_scopes"] = list(packet.requested_scopes)
            google_pref["auth_url"] = packet.auth_url
            google_pref["bundle_label"] = str(bundle_details.get("label") or oauth_bundle)
            google_pref["bundle_summary"] = str(bundle_details.get("summary") or "")
            google_pref["next_step"] = (
                f"Complete {google_pref['bundle_label']} to finish Google account linking."
                if oauth_bundle == "identity"
                else f"Complete {google_pref['bundle_label']} consent to unlock that assistant bundle."
            )
            updated = self._replace_channel_pref(state, "google", google_pref, status="in_progress")
            payload = self.status(principal_id=principal_id, state_override=updated)
            payload["google_start"] = {
                "ready": True,
                "requested_bundle": requested_bundle,
                "oauth_bundle": oauth_bundle,
                "bundle_label": google_pref["bundle_label"],
                "bundle_summary": google_pref["bundle_summary"],
                "start_url": packet.auth_url,
                "auth_url": packet.auth_url,
                "requested_scopes": list(packet.requested_scopes),
                "capabilities": list(bundle_details.get("capabilities") or ()),
                "limitations": list(bundle_details.get("limitations") or ()),
            }
            return payload
        except RuntimeError as exc:
            google_pref["status"] = "credentials_missing"
            reason = str(exc or "").strip() or "google_oauth_not_ready"
            help_text = _google_oauth_missing_config_detail(reason)
            google_pref["next_step"] = f"{reason}: {help_text}" if reason else help_text
            updated = self._replace_channel_pref(state, "google", google_pref, status="in_progress")
            payload = self.status(principal_id=principal_id, state_override=updated)
            google_channel_status = dict(payload.get("channels", {}).get("google") or {})
            payload["google_start"] = {
                "ready": False,
                "requested_bundle": requested_bundle,
                "start_url": "",
                "auth_url": "",
                "requested_scopes": [],
                "error": reason,
                "detail": help_text,
            }
            google_channel_status.update(
                {
                    "status": "credentials_missing",
                    "detail": help_text,
                    "next_step": help_text,
                }
            )
            payload.setdefault("channels", {})["google"] = google_channel_status
            return payload

    def start_telegram(
        self,
        *,
        principal_id: str,
        telegram_ref: str,
        identity_mode: str,
        history_mode: str,
        assistant_surfaces: tuple[str, ...],
    ) -> dict[str, object]:
        external_ref = str(telegram_ref or "").strip() or principal_id
        binding = self._telegram_identity.stage_identity(
            principal_id=principal_id,
            telegram_ref=external_ref,
            identity_mode=identity_mode,
            history_mode=history_mode,
            assistant_surfaces=assistant_surfaces,
        )
        surfaces = tuple(sorted({str(v).strip().lower() for v in assistant_surfaces if str(v).strip()}))
        state = self._ensure_state(principal_id)
        telegram_pref = dict((state.channel_preferences_json or {}).get("telegram") or {})
        telegram_pref.update(
            {
                "telegram_ref": external_ref,
                "identity_mode": str(identity_mode or "login_widget").strip() or "login_widget",
                "history_mode": str(history_mode or "future_only").strip() or "future_only",
                "assistant_surfaces": list(surfaces),
                "binding_id": binding.binding_id,
                "status": "guided_manual",
                "next_step": "Open the PropertyQuarry bot and send /start when you want Telegram alerts.",
            }
        )
        updated = self._replace_channel_pref(state, "telegram", telegram_pref, status="in_progress")
        payload = self.status(principal_id=principal_id, state_override=updated)
        payload["telegram_start"] = {
            "binding_id": binding.binding_id,
            "status": "guided_manual",
            "detail": telegram_pref["next_step"],
        }
        return payload

    def link_telegram_bot(
        self,
        *,
        principal_id: str,
        bot_handle: str,
        install_surfaces: tuple[str, ...],
        default_chat_ref: str,
    ) -> dict[str, object]:
        binding = self._telegram_bot.link_official_bot(
            principal_id=principal_id,
            bot_handle=str(bot_handle or "").strip() or principal_id,
            install_surfaces=install_surfaces,
            default_chat_ref=default_chat_ref,
        )
        external_ref = str(bot_handle or "").strip() or principal_id
        surfaces = tuple(sorted({str(v).strip().lower() for v in install_surfaces if str(v).strip()}))
        state = self._ensure_state(principal_id)
        telegram_pref = dict((state.channel_preferences_json or {}).get("telegram") or {})
        telegram_pref.update(
            {
                "bot_handle": external_ref,
                "bot_binding_id": binding.binding_id,
                "install_surfaces": list(surfaces),
                "default_chat_ref": str(default_chat_ref or "").strip(),
                "status": "bot_link_requested",
                "next_step": "Open the PropertyQuarry bot, send /start, and keep future alerts in that chat.",
            }
        )
        updated = self._replace_channel_pref(state, "telegram", telegram_pref, status="in_progress")
        payload = self.status(principal_id=principal_id, state_override=updated)
        payload["telegram_bot"] = {
            "binding_id": binding.binding_id,
            "status": "bot_link_requested",
        }
        return payload

    def bind_telegram_chat(
        self,
        *,
        principal_id: str,
        chat_ref: str,
        bot_handle: str,
        bot_key: str = "default",
    ) -> dict[str, object]:
        normalized_chat_ref = str(chat_ref or "").strip()
        if not normalized_chat_ref:
            raise ValueError("telegram_chat_ref_required")
        normalized_bot_handle = str(bot_handle or "").strip()
        normalized_bot_key = str(bot_key or "default").strip() or "default"
        official_external_ref = normalized_bot_handle or principal_id
        official_binding = self._tool_runtime.upsert_connector_binding(
            principal_id=principal_id,
            connector_name=TELEGRAM_OFFICIAL_BOT_CONNECTOR,
            external_account_ref=official_external_ref,
            scope_json={"install_surfaces": ["dm"]},
            auth_metadata_json={
                "default_chat_ref": normalized_chat_ref,
                "bot_handle": normalized_bot_handle,
                "bot_key": normalized_bot_key,
                "status": "enabled",
            },
            status="enabled",
        )
        identity_binding = self._tool_runtime.upsert_connector_binding(
            principal_id=principal_id,
            connector_name=TELEGRAM_IDENTITY_CONNECTOR,
            external_account_ref=normalized_chat_ref,
            scope_json={"assistant_surfaces": ["dm"]},
            auth_metadata_json={
                "identity_mode": "bot_webhook",
                "history_mode": "future_only",
                "default_chat_ref": normalized_chat_ref,
                "bot_handle": normalized_bot_handle,
                "bot_key": normalized_bot_key,
                "status": "enabled",
                "manual_bound": True,
            },
            status="enabled",
        )
        state = self._ensure_state(principal_id)
        telegram_pref = dict((state.channel_preferences_json or {}).get("telegram") or {})
        telegram_pref.update(
            {
                "bot_handle": official_external_ref,
                "bot_binding_id": official_binding.binding_id,
                "binding_id": identity_binding.binding_id,
                "default_chat_ref": normalized_chat_ref,
                "status": "enabled",
                "next_step": "Telegram direct messages are bound and ready.",
            }
        )
        updated = self._replace_channel_pref(state, "telegram", telegram_pref, status="in_progress")
        payload = self.status(principal_id=principal_id, state_override=updated)
        payload["telegram_bot"] = {
            "binding_id": official_binding.binding_id,
            "identity_binding_id": identity_binding.binding_id,
            "status": "enabled",
            "default_chat_ref": normalized_chat_ref,
        }
        return payload

    def start_whatsapp_business(
        self,
        *,
        principal_id: str,
        phone_number: str,
        business_name: str,
        import_history_now: bool,
    ) -> dict[str, object]:
        binding = self._whatsapp_business.start_business_onboarding(
            principal_id=principal_id,
            phone_number=phone_number,
            business_name=business_name,
            import_history_now=import_history_now,
        )
        external_ref = str(phone_number or "").strip() or principal_id
        state = self._ensure_state(principal_id)
        whatsapp_pref = dict((state.channel_preferences_json or {}).get("whatsapp") or {})
        whatsapp_pref.update(
            {
                "mode": "business",
                "phone_number": external_ref,
                "business_name": str(business_name or "").strip(),
                "import_history_now": bool(import_history_now),
                "binding_id": binding.binding_id,
                "status": "planned_business",
                "next_step": "Use Business onboarding when the adapter lands, and trigger history sync inside the allowed onboarding window.",
            }
        )
        updated = self._replace_channel_pref(state, "whatsapp", whatsapp_pref, status="in_progress")
        payload = self.status(principal_id=principal_id, state_override=updated)
        payload["whatsapp_business"] = {
            "binding_id": binding.binding_id,
            "status": "planned_business",
        }
        return payload

    def import_whatsapp_export(
        self,
        *,
        principal_id: str,
        export_label: str,
        selected_chat_labels: tuple[str, ...],
        include_media: bool,
    ) -> dict[str, object]:
        binding = self._whatsapp_import.plan_export_ingest(
            principal_id=principal_id,
            export_label=export_label,
            selected_chat_labels=selected_chat_labels,
            include_media=include_media,
        )
        external_ref = str(export_label or "").strip() or principal_id
        chats = tuple(str(v).strip() for v in selected_chat_labels if str(v).strip())
        state = self._ensure_state(principal_id)
        whatsapp_pref = dict((state.channel_preferences_json or {}).get("whatsapp") or {})
        whatsapp_pref.update(
            {
                "mode": "export",
                "export_label": external_ref,
                "selected_chat_labels": list(chats),
                "include_media": bool(include_media),
                "ingestion_mode": "planned_only",
                "binding_id": binding.binding_id,
                "status": "export_planned",
                "next_step": "Plan explicit WhatsApp export intake; generic automatic WhatsApp history import is not promised here.",
            }
        )
        updated = self._replace_channel_pref(state, "whatsapp", whatsapp_pref, status="in_progress")
        payload = self.status(principal_id=principal_id, state_override=updated)
        payload["whatsapp_export"] = {
            "binding_id": binding.binding_id,
            "status": "export_planned",
        }
        return payload

    def acknowledge_whatsapp_export_import(
        self,
        *,
        principal_id: str,
        binding_id: str,
        imported_message_count: int,
        status: str,
    ) -> dict[str, object]:
        state = self._ensure_state(principal_id)
        binding = self._find_whatsapp_export_binding(principal_id=principal_id, binding_id=binding_id)
        if binding is None:
            raise RuntimeError("onboarding_whatsapp_export_binding_not_found")
        normalized_status = str(status or "imported").strip()
        self._chat_export_ingest.ack_import(
            principal_id=principal_id,
            binding_id=binding.binding_id,
            imported_message_count=imported_message_count,
            status=normalized_status,
        )
        whatsapp_pref = dict((state.channel_preferences_json or {}).get("whatsapp") or {})
        if str(whatsapp_pref.get("binding_id") or "") != binding.binding_id:
            whatsapp_pref["binding_id"] = binding.binding_id
        completion_status = (
            "export_intake_complete"
            if normalized_status.lower() in {"imported", "completed", "import_acknowledged", "ok"}
            else "export_planned"
        )
        whatsapp_pref.update(
            {
                "mode": "export",
                "ingestion_mode": "plan_confirmed",
                "status": completion_status,
                "last_imported_count": int(imported_message_count or 0),
                "next_step": "Use the next setup steps to finalize preferences and prepare the first useful property shortlist.",
            }
        )
        updated = self._replace_channel_pref(state, "whatsapp", whatsapp_pref, status="in_progress")
        payload = self.status(principal_id=principal_id, state_override=updated)
        payload["whatsapp_export"] = {
            "binding_id": binding.binding_id,
            "status": "import_acknowledged",
            "imported_message_count": int(imported_message_count or 0),
        }
        return payload

    def _find_whatsapp_export_binding(self, *, principal_id: str, binding_id: str) -> ConnectorBinding | None:
        for binding in self._tool_runtime.list_connector_bindings(principal_id=principal_id, limit=200):
            if binding.connector_name != WHATSAPP_EXPORT_CONNECTOR:
                continue
            if binding.binding_id == binding_id:
                return binding
        return None

    def finalize(
        self,
        *,
        principal_id: str,
        retention_mode: str,
        metadata_only_channels: tuple[str, ...],
        allow_drafts: bool,
        allow_action_suggestions: bool,
        allow_auto_briefs: bool,
        auto_brief_cadence: str = DEFAULT_AUTO_BRIEF_CADENCE,
        auto_brief_delivery_time_local: str = DEFAULT_AUTO_BRIEF_DELIVERY_TIME_LOCAL,
        auto_brief_quiet_hours_start: str = DEFAULT_AUTO_BRIEF_QUIET_HOURS_START,
        auto_brief_quiet_hours_end: str = DEFAULT_AUTO_BRIEF_QUIET_HOURS_END,
        auto_brief_recipient_email: str = "",
        auto_brief_delivery_channel: str = "email",
    ) -> dict[str, object]:
        state = self._ensure_state(principal_id)
        privacy = {
            "retention_mode": str(retention_mode or "full_bodies").strip() or "full_bodies",
            "metadata_only_channels": list(self._normalize_channels(metadata_only_channels)),
            "allow_drafts": bool(allow_drafts),
            "allow_action_suggestions": bool(allow_action_suggestions),
            "allow_auto_briefs": bool(allow_auto_briefs),
        }
        google_binding = self._preferred_google_binding(principal_id=principal_id)
        google_state = self._provider_registry.binding_state(GOOGLE_PROVIDER_KEY, principal_id=principal_id)
        connectors = self._connectors_for_status(principal_id=principal_id)
        channel_statuses = self._channel_statuses(
            principal_id=principal_id,
            state=state,
            google_binding=google_binding,
            google_state=google_state,
            connectors=connectors,
        )
        self._upsert_morning_memo_delivery_preference(
            principal_id=principal_id,
            state=state,
            google_binding=google_binding,
            allow_auto_briefs=allow_auto_briefs,
            cadence=auto_brief_cadence,
            delivery_time_local=auto_brief_delivery_time_local,
            quiet_hours_start=auto_brief_quiet_hours_start,
            quiet_hours_end=auto_brief_quiet_hours_end,
            recipient_email=auto_brief_recipient_email,
            delivery_channel=auto_brief_delivery_channel,
        )
        preview = self._build_brief_preview(
            principal_id=principal_id,
            state=state,
            privacy=privacy,
            channel_statuses=channel_statuses,
            google_binding=google_binding,
            connectors=connectors,
        )
        saved = self._repo.upsert_state(
            principal_id=principal_id,
            onboarding_id=state.onboarding_id,
            workspace_name=state.workspace_name,
            workspace_mode=self._normalize_workspace_mode(state.workspace_mode),
            region=state.region,
            language=state.language,
            timezone=state.timezone,
            selected_channels=state.selected_channels,
            privacy_preferences_json=privacy,
            channel_preferences_json=dict(state.channel_preferences_json),
            brief_preview_json=preview,
            status="ready_for_brief",
        )
        return self.status(principal_id=principal_id, state_override=saved)

    def status(self, *, principal_id: str, state_override: OnboardingState | None = None) -> dict[str, object]:
        state = state_override or self._bridge_browser_principal_state(principal_id) or self._repo.get_for_principal(principal_id)
        state = self._restore_property_commercial_from_teable(principal_id=principal_id, state=state)
        google_binding = self._preferred_google_binding(principal_id=principal_id)
        google_state = self._provider_registry.binding_state(GOOGLE_PROVIDER_KEY, principal_id=principal_id)
        connectors = self._connectors_for_status(principal_id=principal_id)
        channel_statuses = self._channel_statuses(
            principal_id=principal_id,
            state=state,
            google_binding=google_binding,
            google_state=google_state,
            connectors=connectors,
        )
        morning_memo_schedule = self._morning_memo_schedule(
            principal_id=principal_id,
            state=state,
            google_binding=google_binding,
        )
        preview = dict(state.brief_preview_json) if state is not None and state.brief_preview_json else self._build_brief_preview(
            principal_id=principal_id,
            state=state,
            privacy=dict(state.privacy_preferences_json) if state is not None else {},
            channel_statuses=channel_statuses,
            google_binding=google_binding,
            connectors=connectors,
        )
        next_step = self._next_step(
            state=state,
            channel_statuses=channel_statuses,
            morning_memo_schedule=morning_memo_schedule,
        )
        normalized_workspace_mode = self._normalize_workspace_mode(state.workspace_mode if state is not None else "personal")
        raw_workspace_mode = str(state.workspace_mode or "").strip().lower() if state is not None else ""
        preview_requires_refresh = bool(
            state is not None
            and raw_workspace_mode
            and raw_workspace_mode != normalized_workspace_mode
        )
        if preview_requires_refresh:
            preview = self._build_brief_preview(
                principal_id=principal_id,
                state=state,
                privacy=dict(state.privacy_preferences_json) if state is not None else {},
                channel_statuses=channel_statuses,
                google_binding=google_binding,
                connectors=connectors,
            )
        raw_property_preferences = dict(state.property_search_preferences_json if state is not None else {})

        def _normalize_property_type_for_response(value: object) -> str:
            if isinstance(value, (list, tuple, set)):
                normalized = [str(item or "").strip().lower() for item in value if str(item or "").strip()]
                return ",".join(normalized) if len(normalized) > 1 else (normalized[0] if normalized else "any")
            text = str(value or "").strip()
            return text or "any"

        if isinstance(raw_property_preferences.get("property_type"), (list, tuple, set)):
            raw_property_preferences["property_type"] = _normalize_property_type_for_response(
                raw_property_preferences.get("property_type")
            )

        normalized_search_agents: list[dict[str, object]] = []
        for raw_agent in list(raw_property_preferences.get("search_agents") or ()):
            if not isinstance(raw_agent, dict):
                continue
            agent_copy = dict(raw_agent)
            if isinstance(agent_copy.get("property_type"), (list, tuple, set)):
                agent_copy["property_type"] = _normalize_property_type_for_response(agent_copy.get("property_type"))
            preferences_json = agent_copy.get("preferences_json")
            if isinstance(preferences_json, dict) and isinstance(preferences_json.get("property_type"), (list, tuple, set)):
                normalized_preferences_json = dict(preferences_json)
                normalized_preferences_json["property_type"] = _normalize_property_type_for_response(
                    normalized_preferences_json.get("property_type")
                )
                agent_copy["preferences_json"] = normalized_preferences_json
            normalized_search_agents.append(agent_copy)
        if normalized_search_agents:
            raw_property_preferences["search_agents"] = normalized_search_agents
        preview_privacy = dict(preview.get("privacy_posture") or {})
        preview_privacy["auto_briefs_schedule"] = morning_memo_schedule
        preview["privacy_posture"] = preview_privacy
        property_notifications = self._property_notification_preferences(state)
        assistant_notifications = self._assistant_notification_preferences(state)
        return {
            "principal_id": principal_id,
            "status": state.status if state is not None else "draft",
            "workspace": {
                "name": state.workspace_name if state is not None else "",
                "mode": normalized_workspace_mode,
                "region": state.region if state is not None else "",
                "language": state.language if state is not None else "",
                "timezone": state.timezone if state is not None else "",
            },
            "selected_channels": list(state.selected_channels if state is not None else ()),
            "property_search_preferences": raw_property_preferences,
            "privacy": dict(state.privacy_preferences_json) if state is not None else {},
            "delivery_preferences": {
                "morning_memo": morning_memo_schedule,
                "assistant_notifications": assistant_notifications,
                "property_notifications": property_notifications,
            },
            "assistant_modes": [dict(row) for row in ASSISTANT_MODE_CATALOG],
            "featured_domains": [dict(row) for row in FEATURED_DOMAIN_CATALOG],
            "storage_posture": {
                "source_of_truth": "PropertyQuarry Postgres",
                "projection_note": "Teable can mirror onboarding, account, and import state, but it is not the canonical message ledger.",
                "attachment_note": "Large media and exports belong in object storage rather than the browser edge or operator spreadsheet layer.",
            },
            "channels": channel_statuses,
            "brief_preview": preview,
            "next_step": next_step,
            "onboarding_id": state.onboarding_id if state is not None else "",
        }

    def compact_status(self, *, principal_id: str) -> dict[str, object]:
        """Local-only account snapshot for authenticated page first paint."""
        state = self._bridge_browser_principal_state(principal_id) or self._repo.get_for_principal(principal_id)
        raw_property_preferences = dict(state.property_search_preferences_json if state is not None else {})

        def _normalize_property_type_for_response(value: object) -> str:
            if isinstance(value, (list, tuple, set)):
                normalized = [str(item or "").strip().lower() for item in value if str(item or "").strip()]
                return ",".join(normalized) if len(normalized) > 1 else (normalized[0] if normalized else "any")
            text = str(value or "").strip()
            return text or "any"

        if isinstance(raw_property_preferences.get("property_type"), (list, tuple, set)):
            raw_property_preferences["property_type"] = _normalize_property_type_for_response(
                raw_property_preferences.get("property_type")
            )
        normalized_search_agents: list[dict[str, object]] = []
        for raw_agent in list(raw_property_preferences.get("search_agents") or ()):
            if not isinstance(raw_agent, dict):
                continue
            agent_copy = dict(raw_agent)
            if isinstance(agent_copy.get("property_type"), (list, tuple, set)):
                agent_copy["property_type"] = _normalize_property_type_for_response(agent_copy.get("property_type"))
            preferences_json = agent_copy.get("preferences_json")
            if isinstance(preferences_json, dict) and isinstance(preferences_json.get("property_type"), (list, tuple, set)):
                normalized_preferences_json = dict(preferences_json)
                normalized_preferences_json["property_type"] = _normalize_property_type_for_response(
                    normalized_preferences_json.get("property_type")
                )
                agent_copy["preferences_json"] = normalized_preferences_json
            normalized_search_agents.append(agent_copy)
        if normalized_search_agents:
            raw_property_preferences["search_agents"] = normalized_search_agents

        channel_preferences = dict(state.channel_preferences_json if state is not None else {})
        google_pref = dict(channel_preferences.get("google") or {})
        telegram_pref = dict(channel_preferences.get("telegram") or {})
        whatsapp_pref = dict(channel_preferences.get("whatsapp") or {})
        normalized_workspace_mode = self._normalize_workspace_mode(state.workspace_mode if state is not None else "personal")
        preview = dict(state.brief_preview_json) if state is not None and state.brief_preview_json else {}
        preview_privacy = dict(preview.get("privacy_posture") or {})
        preview_privacy["auto_briefs_schedule"] = self._morning_memo_schedule(
            principal_id=principal_id,
            state=state,
            google_binding=None,
        )
        preview["privacy_posture"] = preview_privacy
        property_notifications = self._property_notification_preferences(state)
        assistant_notifications = self._assistant_notification_preferences(state)
        google_email = str(google_pref.get("account_email") or google_pref.get("email") or "").strip()
        return {
            "principal_id": principal_id,
            "status": state.status if state is not None else "draft",
            "workspace": {
                "name": state.workspace_name if state is not None else "",
                "mode": normalized_workspace_mode,
                "region": state.region if state is not None else "",
                "language": state.language if state is not None else "",
                "timezone": state.timezone if state is not None else "",
            },
            "selected_channels": list(state.selected_channels if state is not None else ()),
            "property_search_preferences": raw_property_preferences,
            "privacy": dict(state.privacy_preferences_json) if state is not None else {},
            "delivery_preferences": {
                "morning_memo": preview_privacy["auto_briefs_schedule"],
                "assistant_notifications": assistant_notifications,
                "property_notifications": property_notifications,
            },
            "assistant_modes": [dict(row) for row in ASSISTANT_MODE_CATALOG],
            "featured_domains": [dict(row) for row in FEATURED_DOMAIN_CATALOG],
            "storage_posture": {
                "source_of_truth": "PropertyQuarry Postgres",
                "projection_note": "Teable can mirror onboarding, account, and import state, but it is not the canonical message ledger.",
                "attachment_note": "Large media and exports belong in object storage rather than the browser edge or operator spreadsheet layer.",
            },
            "channels": {
                "google": {
                    "status": str(google_pref.get("status") or ("connected" if google_email else "not_connected")),
                    "connected_account_email": google_email,
                },
                "telegram": {"status": str(telegram_pref.get("status") or "not_connected")},
                "whatsapp": {"status": str(whatsapp_pref.get("status") or "not_connected")},
            },
            "brief_preview": preview,
            "next_step": "Open the current search or update the account settings.",
            "onboarding_id": state.onboarding_id if state is not None else "",
        }

    def list_property_search_agent_principals(self, *, limit: int = 1000) -> tuple[str, ...]:
        try:
            normalized_limit = max(int(limit or 0), 1)
        except Exception:
            return ()
        projected_list = getattr(
            self._repo,
            "list_enabled_property_search_agent_principals",
            None,
        )
        if callable(projected_list):
            try:
                projected_principals = projected_list(limit=normalized_limit)
                return tuple(
                    dict.fromkeys(
                        principal_id
                        for value in projected_principals
                        for principal_id in (str(value or "").strip(),)
                        if principal_id
                    )
                )
            except Exception:
                # A projection-capable repository must fail closed. Falling
                # back to full onboarding rows can exhaust scheduler memory.
                return ()
        try:
            rows = self._repo.list_states(limit=normalized_limit)
        except Exception:
            return ()
        principals: list[str] = []
        for state in rows:
            principal_id = str(getattr(state, "principal_id", "") or "").strip()
            if not principal_id:
                continue
            raw_property_preferences = dict(getattr(state, "property_search_preferences_json", {}) or {})
            agents = self._normalize_property_search_agents(
                raw_property_preferences,
                active_agent_id=str(raw_property_preferences.get("active_search_agent_id") or "").strip(),
                enforce_plan_limit=False,
            )
            if not any(bool(agent.get("enabled")) for agent in agents):
                continue
            principals.append(principal_id)
        return tuple(dict.fromkeys(principals))

    def _ensure_state(self, principal_id: str) -> OnboardingState:
        existing = self._bridge_browser_principal_state(principal_id) or self._repo.get_for_principal(principal_id)
        if existing is not None:
            return existing
        return self._repo.upsert_state(principal_id=principal_id, status="draft")

    def _assistant_notification_preferences(self, state: OnboardingState | None) -> dict[str, object]:
        channel_preferences = dict(getattr(state, "channel_preferences_json", {}) or {}) if state is not None else {}
        raw_preferences = channel_preferences.get("assistant_notifications")
        preferences = dict(raw_preferences or {}) if isinstance(raw_preferences, dict) else {}
        whatsapp_ai_support_phone = str(preferences.get("whatsapp_ai_support_phone") or "").strip()
        return {
            "notification_scope": "morning_memo_queue_and_support",
            "whatsapp_ai_support_phone": whatsapp_ai_support_phone,
            "whatsapp_ai_support_enabled": bool(whatsapp_ai_support_phone),
            "whatsapp_notification_opt_in": bool(preferences.get("whatsapp_notification_opt_in")),
            "whatsapp_ai_support_purpose": ASSISTANT_WHATSAPP_AI_SUPPORT_PURPOSE,
            "whatsapp_ai_support_opening_prompt": ASSISTANT_WHATSAPP_AI_SUPPORT_OPENING_PROMPT,
            "channels": [
                {"key": "email", "label": "Email", "enabled": True, "status": "available"},
                {"key": "telegram", "label": "Telegram", "enabled": True, "status": "available"},
                {"key": "whatsapp", "label": "WhatsApp", "enabled": True, "status": "available"},
            ],
        }

    def update_assistant_notification_preferences(
        self,
        *,
        principal_id: str,
        whatsapp_ai_support_phone: object | None = None,
        whatsapp_notification_opt_in: object | None = None,
    ) -> dict[str, object]:
        state = self._ensure_state(principal_id)
        channel_preferences = dict(state.channel_preferences_json or {})
        assistant_notifications = dict(channel_preferences.get("assistant_notifications") or {})
        normalized_support_phone: str | None = None
        if whatsapp_ai_support_phone is not None:
            normalized_support_phone = normalize_assistant_whatsapp_ai_support_phone(whatsapp_ai_support_phone)
        assistant_notifications.update(
            {
                "notification_scope": "morning_memo_queue_and_support",
                "whatsapp_ai_support_purpose": ASSISTANT_WHATSAPP_AI_SUPPORT_PURPOSE,
                "whatsapp_ai_support_opening_prompt": ASSISTANT_WHATSAPP_AI_SUPPORT_OPENING_PROMPT,
            }
        )
        if whatsapp_notification_opt_in is not None:
            assistant_notifications["whatsapp_notification_opt_in"] = bool(whatsapp_notification_opt_in)
        if normalized_support_phone is not None:
            if normalized_support_phone:
                assistant_notifications["whatsapp_ai_support_phone"] = normalized_support_phone
                assistant_notifications["whatsapp_ai_support_status"] = "ready"
            else:
                assistant_notifications.pop("whatsapp_ai_support_phone", None)
                assistant_notifications["whatsapp_ai_support_status"] = "missing"
        channel_preferences["assistant_notifications"] = assistant_notifications
        for channel in ASSISTANT_NOTIFICATION_CHANNELS:
            channel_preferences.setdefault(channel, {})
        selected = {
            str(channel or "").strip().lower()
            for channel in state.selected_channels
            if str(channel or "").strip()
        }
        if (
            str(assistant_notifications.get("whatsapp_ai_support_phone") or "").strip()
            or bool(assistant_notifications.get("whatsapp_notification_opt_in"))
        ):
            selected.add("whatsapp")
        saved = self._repo.upsert_state(
            principal_id=state.principal_id,
            onboarding_id=state.onboarding_id,
            workspace_name=state.workspace_name,
            workspace_mode=state.workspace_mode,
            region=state.region,
            language=state.language,
            timezone=state.timezone,
            selected_channels=tuple(sorted(selected)),
            property_search_preferences_json=dict(state.property_search_preferences_json),
            privacy_preferences_json=dict(state.privacy_preferences_json),
            channel_preferences_json=channel_preferences,
            brief_preview_json=dict(state.brief_preview_json),
            status=state.status,
        )
        return self.status(principal_id=principal_id, state_override=saved)

    def _property_notification_preferences(self, state: OnboardingState | None) -> dict[str, object]:
        channel_preferences = dict(getattr(state, "channel_preferences_json", {}) or {}) if state is not None else {}
        raw_preferences = channel_preferences.get("property_notifications")
        preferences = dict(raw_preferences or {}) if isinstance(raw_preferences, dict) else {}
        telegram_pref = dict(channel_preferences.get("telegram") or {})
        telegram_status = str(telegram_pref.get("status") or "").strip().lower()
        telegram_chat_ref = str(telegram_pref.get("default_chat_ref") or "").strip()
        telegram_connected = bool(telegram_chat_ref and telegram_status == "enabled")
        telegram_bot_profile = _propertyquarry_telegram_bot_public_profile()
        whatsapp_ai_support_phone = str(preferences.get("whatsapp_ai_support_phone") or "").strip()
        configured_channel = str(preferences.get("preferred_channel") or "").strip().lower()
        raw_selected_channels = preferences.get("selected_channels")
        preferred_channel = ""
        if configured_channel:
            try:
                normalized_preferred_channel = normalize_property_notification_channel(configured_channel)
            except ValueError:
                normalized_preferred_channel = ""
            if normalized_preferred_channel:
                preferred_channel = normalized_preferred_channel
        try:
            selected_channels = normalize_property_notification_channels(
                raw_selected_channels,
                fallback=None,
            )
        except ValueError:
            selected_channels = ()
        if preferred_channel and preferred_channel not in selected_channels:
            preferred_channel = ""
        return {
            "preferred_channel": preferred_channel,
            "preferred_label": PROPERTY_NOTIFICATION_CHANNEL_LABELS.get(preferred_channel, "") if preferred_channel else "",
            "selected_channels": list(selected_channels),
            "selected_labels": [
                PROPERTY_NOTIFICATION_CHANNEL_LABELS.get(channel, channel.title())
                for channel in selected_channels
            ],
            "configured": bool(configured_channel or raw_selected_channels),
            "notification_scope": "scout_updates",
            "whatsapp_ai_support_phone": whatsapp_ai_support_phone,
            "whatsapp_ai_support_enabled": bool(whatsapp_ai_support_phone),
            "whatsapp_notification_opt_in": "whatsapp" in selected_channels,
            "whatsapp_ai_support_purpose": PROPERTY_WHATSAPP_AI_SUPPORT_PURPOSE,
            "whatsapp_ai_support_opening_prompt": PROPERTY_WHATSAPP_AI_SUPPORT_OPENING_PROMPT,
            "telegram_bot": {
                **telegram_bot_profile,
                "connected": telegram_connected,
                "status_label": "Connected" if telegram_connected else "Open the bot and send /start",
            },
            "channels": [
                {"key": "email", "label": "Email", "enabled": True, "status": "available"},
                {"key": "telegram", "label": "PropertyQuarry bot", "enabled": True, "status": "available"},
                {"key": "whatsapp", "label": "WhatsApp", "enabled": True, "status": "available"},
                {"key": "signal", "label": "Signal", "enabled": False, "status": "coming_soon"},
            ],
        }

    def update_property_notification_preferences(
        self,
        *,
        principal_id: str,
        preferred_channel: object,
        selected_channels: object | None = None,
        whatsapp_ai_support_phone: object | None = None,
    ) -> dict[str, object]:
        state = self._ensure_state(principal_id)
        normalized_channel = normalize_property_notification_channel(preferred_channel)
        channel_preferences = dict(state.channel_preferences_json or {})
        property_notifications = dict(channel_preferences.get("property_notifications") or {})
        existing_selected_channels = property_notifications.get("selected_channels")
        normalized_channels = normalize_property_notification_channels(
            existing_selected_channels if selected_channels is None else selected_channels,
            fallback=normalized_channel,
        )
        if normalized_channel not in normalized_channels:
            raise ValueError("property_notification_primary_not_selected")
        normalized_support_phone: str | None = None
        if whatsapp_ai_support_phone is not None:
            normalized_support_phone = normalize_property_whatsapp_ai_support_phone(whatsapp_ai_support_phone)
        property_notifications.update(
            {
                "preferred_channel": normalized_channel,
                "selected_channels": list(normalized_channels),
                "selected_labels": [
                    PROPERTY_NOTIFICATION_CHANNEL_LABELS.get(channel, channel.title())
                    for channel in normalized_channels
                ],
                "notification_scope": "scout_updates",
                "whatsapp_notification_opt_in": "whatsapp" in normalized_channels,
                "signal_status": "coming_soon",
                "whatsapp_ai_support_purpose": PROPERTY_WHATSAPP_AI_SUPPORT_PURPOSE,
                "whatsapp_ai_support_opening_prompt": PROPERTY_WHATSAPP_AI_SUPPORT_OPENING_PROMPT,
            }
        )
        if normalized_support_phone is not None:
            if normalized_support_phone:
                property_notifications["whatsapp_ai_support_phone"] = normalized_support_phone
                property_notifications["whatsapp_ai_support_status"] = "ready"
            else:
                property_notifications.pop("whatsapp_ai_support_phone", None)
                property_notifications["whatsapp_ai_support_status"] = "missing"
        channel_preferences["property_notifications"] = property_notifications
        for channel in PROPERTY_NOTIFICATION_CHANNELS:
            channel_preferences.setdefault(channel, {})
        saved = self._repo.upsert_state(
            principal_id=state.principal_id,
            onboarding_id=state.onboarding_id,
            workspace_name=state.workspace_name,
            workspace_mode=state.workspace_mode,
            region=state.region,
            language=state.language,
            timezone=state.timezone,
            selected_channels=tuple(state.selected_channels),
            property_search_preferences_json=dict(state.property_search_preferences_json),
            privacy_preferences_json=dict(state.privacy_preferences_json),
            channel_preferences_json=channel_preferences,
            brief_preview_json=dict(state.brief_preview_json),
            status=state.status,
        )
        return self.status(principal_id=principal_id, state_override=saved)

    def _replace_channel_pref(
        self,
        state: OnboardingState,
        channel: str,
        value: dict[str, object],
        *,
        status: str,
    ) -> OnboardingState:
        prefs = dict(state.channel_preferences_json or {})
        prefs[str(channel or "").strip().lower()] = dict(value or {})
        selected = set(state.selected_channels)
        selected.add(str(channel or "").strip().lower())
        return self._repo.upsert_state(
            principal_id=state.principal_id,
            onboarding_id=state.onboarding_id,
            workspace_name=state.workspace_name,
            workspace_mode=state.workspace_mode,
            region=state.region,
            language=state.language,
            timezone=state.timezone,
            selected_channels=tuple(sorted(selected)),
            privacy_preferences_json=dict(state.privacy_preferences_json),
            channel_preferences_json=prefs,
            brief_preview_json=dict(state.brief_preview_json),
            status=status,
        )

    def _connectors_for_status(self, *, principal_id: str) -> list[ConnectorBinding]:
        merged: list[ConnectorBinding] = []
        seen: set[str] = set()
        for candidate_principal_id in _telegram_binding_principal_candidates(principal_id):
            for binding in self._tool_runtime.list_connector_bindings(candidate_principal_id, limit=100):
                binding_id = str(binding.binding_id or "").strip()
                if binding_id and binding_id in seen:
                    continue
                if binding_id:
                    seen.add(binding_id)
                merged.append(binding)
        return merged

    def _channel_statuses(
        self,
        *,
        principal_id: str,
        state: OnboardingState | None,
        google_binding,
        google_state,
        connectors: list[ConnectorBinding],
    ) -> dict[str, dict[str, object]]:
        channel_prefs = dict(state.channel_preferences_json) if state is not None else {}
        by_name: dict[str, list[ConnectorBinding]] = {}
        for binding in connectors:
            by_name.setdefault(binding.connector_name, []).append(binding)
        google_pref = dict(channel_prefs.get("google") or {})
        google_requested_bundle = str(google_pref.get("requested_bundle") or "").strip().lower() or "identity"
        google_bundle = google_scope_bundle_details(google_requested_bundle)
        google_status = "not_selected"
        google_detail = "Select Google during onboarding if you want Google sign-in."
        granted_scopes = []
        if google_binding is not None:
            google_status = "connected"
            granted_scopes = list(dict(google_binding.auth_metadata_json or {}).get("granted_scopes") or [])
            if google_bundle_supports_workspace_sync(scopes=tuple(granted_scopes)):
                google_detail = "Google is linked for this principal and can now feed workspace signals according to the granted bundle."
            else:
                google_detail = "Google sign-in is connected for this account."
        elif google_state is not None and bool(google_state.secret_configured):
            if google_pref:
                google_status = "ready_to_connect"
                google_detail = f"{google_bundle['label']} can be connected through the existing OAuth flow."
            else:
                google_status = "available"
                google_detail = "Google onboarding is available. PropertyQuarry only needs the narrow Google sign-in bundle by default."
        elif google_state is not None:
            google_status = "credentials_missing"
            google_detail = (
                "Google OAuth credentials are not configured for this PropertyQuarry host yet. "
                "Set EA_GOOGLE_OAUTH_CLIENT_ID, EA_GOOGLE_OAUTH_CLIENT_SECRET, "
                "EA_GOOGLE_OAUTH_REDIRECT_URI, EA_GOOGLE_OAUTH_STATE_SECRET, and EA_PROVIDER_SECRET_KEY."
            )
        telegram_pref = dict(channel_prefs.get("telegram") or {})
        telegram_status = str(telegram_pref.get("status") or "").strip() or "not_selected"
        telegram_detail = str(telegram_pref.get("next_step") or "").strip() or (
            "Telegram alerts use the PropertyQuarry bot. Sign-in and chat delivery stay separate."
        )
        telegram_identity_bindings = by_name.get(TELEGRAM_IDENTITY_CONNECTOR, [])
        telegram_bot_bindings = by_name.get(TELEGRAM_OFFICIAL_BOT_CONNECTOR, [])
        telegram_bot_profile = _propertyquarry_telegram_bot_public_profile()
        telegram_chat_bound = any(
            str(dict(binding.auth_metadata_json or {}).get("default_chat_ref") or binding.external_account_ref or "").strip()
            for binding in telegram_identity_bindings
            if str(binding.status or "").strip().lower() == "enabled"
        )
        if telegram_chat_bound:
            telegram_status = "enabled"
        elif telegram_bot_bindings:
            telegram_status = "bot_link_requested"
        elif telegram_identity_bindings:
            telegram_status = telegram_status or "guided_manual"
        whatsapp_pref = dict(channel_prefs.get("whatsapp") or {})
        whatsapp_status = str(whatsapp_pref.get("status") or "").strip() or "not_selected"
        whatsapp_detail = str(whatsapp_pref.get("next_step") or "").strip() or (
            "WhatsApp stays split between supported business onboarding and explicit export-planned intake."
        )
        if by_name.get(WHATSAPP_BUSINESS_CONNECTOR):
            whatsapp_status = "planned_business"
        elif by_name.get(WHATSAPP_EXPORT_CONNECTOR):
            export_statuses = [str(binding.status or "") for binding in by_name.get(WHATSAPP_EXPORT_CONNECTOR, [])]
            if any(status.strip().lower() in {"import_acknowledged", "export_intake_complete", "imported", "completed"} for status in export_statuses):
                whatsapp_status = "import_acknowledged"
            elif any(status.strip().lower() == "planned" for status in export_statuses):
                whatsapp_status = "export_planned"
            else:
                whatsapp_status = "export_planned"
        return {
            "google": {
                "status": google_status,
                "requested_bundle": google_requested_bundle,
                "granted_scopes": granted_scopes,
                "detail": google_detail,
                "bundle_label": str(google_bundle.get("label") or "Google sign-in"),
                "bundle_summary": str(google_bundle.get("summary") or ""),
                "capabilities": list(google_bundle.get("capabilities") or ()),
                "limitations": list(google_bundle.get("limitations") or ()),
                "bundle_options": [
                    google_scope_bundle_details("identity"),
                ],
                "history_import_posture": "PropertyQuarry treats Google as optional account access. It does not assume mailbox or calendar ingestion from sign-in alone.",
            },
            "telegram": {
                "status": telegram_status,
                "detail": telegram_detail,
                "identity_path": "Telegram Login / OIDC",
                "bot_path": "PropertyQuarry bot",
                "product_bot": {
                    **telegram_bot_profile,
                    "connected": bool(telegram_chat_bound),
                    "status_label": "Connected" if telegram_chat_bound else "Open the bot and send /start",
                },
                "history_import_posture": "Identity linking does not import full Telegram history. Start future-only or import later through explicit workflows.",
                "capabilities": [
                    "Sign in with Telegram identity",
                    "Receive property alerts in a Telegram chat",
                    "Use the PropertyQuarry bot as the durable interaction surface",
                ],
                "limitations": [
                    "Telegram sign-in does not import past chats.",
                ],
                "bindings": [binding.binding_id for binding in telegram_identity_bindings + telegram_bot_bindings],
            },
            "whatsapp": {
                "status": whatsapp_status,
                "ingestion_mode": str(whatsapp_pref.get("ingestion_mode") or "planned_only"),
                "detail": whatsapp_detail,
                "path_options": [
                    {
                        "key": "business",
                        "label": "WhatsApp Business onboarding",
                        "summary": "Preferred when a business-grade account can be onboarded and history sync is triggered in the supported onboarding window.",
                    },
                    {
                        "key": "export",
                        "label": "WhatsApp export planning",
                        "summary": "Fallback for personal or unsupported paths: stage export-file intake explicitly instead of pretending a generic sync exists.",
                    },
                ],
                "capabilities": [
                    "Stage Business onboarding separately from export intake planning",
                    "Keep historical import and future sync as distinct events",
                ],
                "limitations": [
                    "No blanket promise that EA can pull every WhatsApp message automatically",
                ],
                "bindings": [binding.binding_id for binding in by_name.get(WHATSAPP_BUSINESS_CONNECTOR, []) + by_name.get(WHATSAPP_EXPORT_CONNECTOR, [])],
            },
        }

    def _build_brief_preview(
        self,
        *,
        principal_id: str,
        state: OnboardingState | None,
        privacy: dict[str, object],
        channel_statuses: dict[str, dict[str, object]],
        google_binding,
        connectors: list[ConnectorBinding],
    ) -> dict[str, object]:
        workspace_name = state.workspace_name if state is not None and state.workspace_name else "Assistant"
        selected_channels = list(state.selected_channels if state is not None else ())
        metadata_only_channels = list(privacy.get("metadata_only_channels") or [])
        channel_prefs = dict(state.channel_preferences_json if state is not None else {})
        connectors_by_name: dict[str, list[ConnectorBinding]] = {}
        for binding in connectors:
            connectors_by_name.setdefault(binding.connector_name, []).append(binding)
        connected: list[str] = []
        history_state: list[str] = []
        top_contacts: list[str] = []
        for channel in selected_channels:
            prefs = dict(channel_prefs.get(channel) or {})
            channel_state = dict(channel_statuses.get(channel) or {})
            status = str(channel_state.get("status") or prefs.get("status") or "not_selected").strip()
            if channel == "google":
                google_email = str(dict(getattr(google_binding, "auth_metadata_json", {}) or {}).get("google_email") or "").strip().lower()
                if google_email:
                    connected.append(f"Google linked as {google_email}")
                    top_contacts.append(google_email)
                    history_state.append(
                        f"Google is connected through {channel_state.get('bundle_label') or 'Google sign-in'} with exactly the granted bundle."
                    )
                elif status == "ready_to_connect":
                    history_state.append("Google consent is staged but not completed yet.")
                else:
                    history_state.append("Google is selected but not connected yet.")
            elif channel == "telegram":
                telegram_ref = str(prefs.get("telegram_ref") or "").strip()
                bot_handle = str(prefs.get("bot_handle") or "").strip()
                if telegram_ref:
                    connected.append(f"Telegram identity staged as {telegram_ref}")
                    top_contacts.append(telegram_ref)
                if bot_handle:
                    connected.append(f"PropertyQuarry bot planned as {bot_handle}")
                history_mode = str(prefs.get("history_mode") or "future_only").replace("_", " ")
                history_state.append(f"Telegram starts as {history_mode}; identity linking does not imply full history import.")
            elif channel == "whatsapp":
                mode = str(prefs.get("mode") or "not_selected").strip()
                if mode == "business":
                    phone_number = str(prefs.get("phone_number") or "").strip()
                    if phone_number:
                        connected.append(f"WhatsApp Business staged for {phone_number}")
                        top_contacts.append(phone_number)
                    if bool(prefs.get("import_history_now")):
                        history_state.append("WhatsApp Business is staged with explicit history-sync intent during the supported onboarding window.")
                    else:
                        history_state.append("WhatsApp Business is staged without pretending a history sync already happened.")
                elif mode == "export":
                    export_label = str(prefs.get("export_label") or "").strip()
                    connected.append(f"WhatsApp export lane staged as {export_label or 'export intake plan'}")
                    history_state.append("WhatsApp history intake is staged from a planned export flow; no automatic bulk pull is claimed yet.")
                else:
                    history_state.append("WhatsApp is selected but not configured yet.")
        if not selected_channels:
            history_state.append("No channels are selected yet, so setup posture is still based on preferences rather than live sources.")
        normalized_workspace_mode = self._normalize_workspace_mode(state.workspace_mode if state is not None else "personal")
        top_themes = list(self._top_themes_for_mode(normalized_workspace_mode, selected_channels))
        if not top_contacts:
            top_contacts = ["No imported contacts yet; the assistant will seed a watchlist after the first real sync or planned intake."]
        first_brief_lines = [
            "Reply first: identify the highest-friction thread across connected channels.",
            "Calendar watch: surface the next real commitment and the people attached to it.",
            "Commitment ledger: keep promises, drafts, and pending replies visible with source traces.",
        ]
        if "telegram" in selected_channels:
            first_brief_lines.append("Telegram recap: distinguish DM urgency from group chatter instead of flattening them together.")
        if "whatsapp" in selected_channels:
            first_brief_lines.append("WhatsApp digest: separate planned export intake from future live sync so the timeline stays honest.")
        suggested_actions = [
            "Connect Google if you want a faster return path or verified account identity.",
            "Choose whether Telegram starts future-only or with a later explicit import step.",
            "Pick either WhatsApp Business onboarding or export intake plan; do not leave both ambiguous.",
        ]
        trust_notes = [
            "Postgres is the source of truth for onboarding, bindings, memory, jobs, and receipts when durable storage is configured.",
            "Teable is projection-grade at most: useful for operator views, not the canonical message ledger.",
            "The assistant only claims history it can actually import or observe through supported channel paths.",
        ]
        return {
            "headline": f"{workspace_name} keeps one accountable property search workspace instead of scattered tabs and half-tracked listings.",
            "principal_id": principal_id,
            "workspace_mode": normalized_workspace_mode,
            "who_you_are": [
                f"Workspace: {workspace_name}",
                f"Mode: {normalized_workspace_mode.replace('_', ' ')}",
                f"Timezone: {state.timezone if state is not None and state.timezone else 'unspecified'}",
            ],
            "connected_channels": connected,
            "selected_channels": selected_channels,
            "history_import_state": history_state,
            "top_themes": top_themes,
            "top_contacts": top_contacts,
            "privacy_posture": {
                "retention_mode": str(privacy.get('retention_mode') or 'full_bodies'),
                "metadata_only_channels": metadata_only_channels,
                "allow_drafts": bool(privacy.get("allow_drafts", False)),
                "allow_action_suggestions": bool(privacy.get("allow_action_suggestions", False)),
                "allow_auto_briefs": bool(privacy.get("allow_auto_briefs", False)),
            },
            "first_brief": first_brief_lines,
            "first_brief_preview": first_brief_lines,
            "suggested_actions": suggested_actions,
            "trust_notes": trust_notes,
        }

    def _next_step(
        self,
        *,
        state: OnboardingState | None,
        channel_statuses: dict[str, dict[str, object]],
        morning_memo_schedule: dict[str, object] | None = None,
    ) -> str:
        if state is None or not state.workspace_name:
            return "Start onboarding with a workspace name, mode, and channel selection."
        google_status = str(dict(channel_statuses.get("google") or {}).get("status") or "")
        if "google" in state.selected_channels and google_status in {"available", "ready_to_connect"}:
            google_label = str(dict(channel_statuses.get("google") or {}).get("bundle_label") or "Google sign-in")
            return f"Complete {google_label} to finish Google account linking."
        if "telegram" in state.selected_channels and str(dict(channel_statuses.get("telegram") or {}).get("status") or "") == "guided_manual":
            return "Open the PropertyQuarry bot and send /start when you want Telegram alerts."
        if "whatsapp" in state.selected_channels and str(dict(channel_statuses.get("whatsapp") or {}).get("status") or "") in {"planned_business", "export_planned", "not_selected"}:
            return "Choose the WhatsApp path: supported business onboarding or export-planned intake."
        if not dict(state.privacy_preferences_json):
            return "Finalize your account preferences so PropertyQuarry can open the first useful property workflow cleanly."
        if bool(dict(state.privacy_preferences_json).get("allow_auto_briefs")) and not bool(
            dict(morning_memo_schedule or {}).get("resolved_recipient_email")
        ):
            return "Connect Google or set a delivery email so notifications can actually send when you enable them."
        return "Review the first shortlist, save feedback, and only then add more sites or alert channels."

    @staticmethod
    def _normalize_auto_brief_cadence(value: str) -> str:
        normalized = str(value or "").strip().lower() or DEFAULT_AUTO_BRIEF_CADENCE
        return AUTO_BRIEF_CADENCE_ALIASES.get(normalized, DEFAULT_AUTO_BRIEF_CADENCE)

    @staticmethod
    def _normalize_auto_brief_delivery_channel(value: str) -> str:
        normalized = str(value or "").strip().lower() or "email"
        if normalized in AUTO_BRIEF_DELIVERY_CHANNELS:
            return normalized
        return "email"

    @staticmethod
    def _normalize_property_search_preferences(value: dict[str, object] | None) -> dict[str, object]:
        raw = flatten_property_search_preferences_snapshot(value)
        # ``raw_preferences`` is an internal snapshot, not a user preference.
        # Full-state updates may feed the normalized payload back into this
        # method, so retaining the prior snapshot here would nest the entire
        # document once more on every save.
        raw.pop("raw_preferences", None)
        raw = {
            **raw,
            **normalize_property_search_preferences_contract(raw),
        }
        catalog_distance_keys = property_distance_preference_keys()
        catalog_importance_keys = {
            property_distance_importance_key(key)
            for key in catalog_distance_keys
        }
        catalog_input_keys = set(catalog_distance_keys) | catalog_importance_keys
        for raw_key in tuple(raw):
            raw_key_text = str(raw_key or "")
            normalized_key = raw_key_text.strip()
            if (
                normalized_key.casefold().startswith("max_distance_to_")
                and (
                    not isinstance(raw_key, str)
                    or raw_key != normalized_key
                    or normalized_key not in catalog_input_keys
                )
            ):
                raw.pop(raw_key, None)
        raw = normalize_property_distance_preference_pairs(raw)
        selected_platforms: list[str] = []
        raw_selected_platforms = raw.get("selected_platforms")
        if isinstance(raw_selected_platforms, (list, tuple, set)):
            for item in raw_selected_platforms:
                normalized = str(item or "").strip().lower()
                if normalized and normalized not in selected_platforms:
                    selected_platforms.append(normalized)
        raw_platform = raw.get("platform")
        if raw_platform:
            normalized_platform = str(raw_platform or "").strip().lower()
            if normalized_platform and normalized_platform not in selected_platforms:
                selected_platforms.append(normalized_platform)

        country_code = normalize_country_code(raw.get("country_code"))
        region_code = str(raw.get("region_code") or "").strip().lower()
        raw_full_region_scope = raw.get("full_region_scope")
        full_region_scope = (
            raw_full_region_scope is True
            or str(raw_full_region_scope or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}
        )
        raw_selected_location_values = raw.get("selected_location_values")
        selected_location_values: list[str] = []
        if isinstance(raw_selected_location_values, (list, tuple, set)):
            for item in raw_selected_location_values:
                normalized_value = str(item or "").strip()
                if normalized_value and normalized_value not in selected_location_values:
                    selected_location_values.append(normalized_value)
        raw_saved_shortlist_candidates = raw.get("saved_shortlist_candidates")
        saved_shortlist_candidates: list[dict[str, object]] = []
        if isinstance(raw_saved_shortlist_candidates, (list, tuple)):
            for item in raw_saved_shortlist_candidates:
                if not isinstance(item, dict):
                    continue
                saved_shortlist_candidates.append(dict(item))
                if len(saved_shortlist_candidates) >= 200:
                    break
        saved_shortlist_share_slug = str(raw.get("saved_shortlist_share_slug") or "").strip()
        location_query = str(raw.get("location_query") or "").strip()
        if full_region_scope and not location_query and region_code:
            try:
                from app.services.property_market_catalog import region_label_for_country_region
                location_query = region_label_for_country_region(country_code, region_code)
            except Exception:
                location_query = region_code.replace("_", " ").title()
        elif not location_query and selected_location_values:
            location_query = ", ".join(selected_location_values)
        language_code = normalize_language_code(raw.get("language_code"), country_code=country_code)
        listing_mode = normalize_listing_mode(raw.get("listing_mode"))
        property_type = normalize_property_type_values(raw.get("property_type"))
        raw_search_agents = raw.get("search_agents")
        cleaned_raw = dict(raw)
        for internal_key in (
            "min_match_score",
            "raw_preferences",
            # These collections have canonical top-level fields. Keeping a
            # second copy in the raw snapshot doubles the largest account
            # payloads and makes every authenticated page pay to de-TOAST it.
            "saved_shortlist_candidates",
            "search_agents",
        ):
            cleaned_raw.pop(internal_key, None)
        selected_platforms, removed_platform_details = filter_selectable_property_platform_details(
            selected_platforms,
            country_code=country_code,
            listing_mode=listing_mode,
            include_distressed_sale_signals=raw.get("include_distressed_sale_signals"),
        )

        max_results_per_source = raw.get("max_results_per_source")
        try:
            normalized_max = int(max_results_per_source) if max_results_per_source is not None else None
            if normalized_max is not None and normalized_max <= 0:
                normalized_max = None
        except Exception:
            normalized_max = None
        search_agent_enabled = raw.get("search_agent_enabled") is True or str(raw.get("search_agent_enabled") or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}
        try:
            search_agent_duration_days = int(float(str(raw.get("search_agent_duration_days") or "").strip()))
        except Exception:
            search_agent_duration_days = 30
        search_agent_duration_days = max(7, min(365, search_agent_duration_days))
        notification_period = str(raw.get("search_agent_notification_period") or "").strip().lower()
        if notification_period not in {"day", "week"}:
            notification_period = "day"
        try:
            notification_limit = int(float(str(raw.get("search_agent_notification_limit") or "").strip()))
        except Exception:
            notification_limit = 5
        notification_limit = max(1, min(50, notification_limit))

        preference_person_id = str(raw.get("preference_person_id") or "self").strip() or "self"
        raw_search_mode = str(raw.get("search_mode") or "").strip().lower()
        search_mode = raw_search_mode if raw_search_mode in {"strict", "discovery"} else "strict"
        property_commercial = normalize_property_commercial(
            dict(raw.get("property_commercial") or {})
            if isinstance(raw.get("property_commercial"), dict)
            else {}
        )
        commercial_snapshot = property_commercial_snapshot({"property_commercial": property_commercial} if property_commercial else {})
        active_plan_key = str(commercial_snapshot.get("current_plan_key") or property_commercial.get("active_plan_key") or "").strip().lower()
        try:
            plan_result_cap = int(commercial_snapshot.get("max_results_per_source") or 0)
        except Exception:
            plan_result_cap = 0
        if property_plan_has_unlimited_provider_results(active_plan_key, plan_result_cap):
            normalized_max = None
        elif normalized_max is not None:
            normalized_max = max(1, min(plan_result_cap or 10, normalized_max))
        promoted_numeric_keys = (
            "max_price_eur",
            "min_rooms",
            "min_area_m2",
            "available_within_years",
            "max_commute_minutes_transit",
            "max_commute_minutes_drive",
            "max_commute_minutes_bike",
            "max_commute_minutes_walk",
        )
        promoted_numeric: dict[str, int] = {}
        for numeric_key in tuple(
            dict.fromkeys(
                (
                    *promoted_numeric_keys,
                    *(
                        key
                        for key in catalog_distance_keys
                        if key in raw
                    ),
                )
            )
        ):
            try:
                value = int(float(str(raw.get(numeric_key) or "").strip()))
            except Exception:
                value = 0
            if value > 0:
                promoted_numeric[numeric_key] = value
        promoted_string_keys = (
            "keywords",
            "avoid_keywords",
            "custom_location_query",
            "custom_keywords",
            "investment_research_mode",
            "commute_destination",
            "additional_reachability_targets",
            "university_name",
            "school_evidence_priority",
            "parking_pressure_preference",
            "search_goal",
        )
        promoted_strings = {
            key: str(raw.get(key) or "").strip()
            for key in tuple(
                dict.fromkeys(
                    (
                        *promoted_string_keys,
                        *(
                            key
                            for key in catalog_importance_keys
                            if key in raw
                        ),
                    )
                )
            )
            if str(raw.get(key) or "").strip()
        }
        if isinstance(raw.get("keyword_preferences"), dict):
            promoted_strings["keyword_preferences_json"] = json.dumps(
                {
                    str(key or "").strip(): str(value or "").strip()
                    for key, value in dict(raw.get("keyword_preferences") or {}).items()
                    if str(key or "").strip() and str(value or "").strip()
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        promoted_lists = {
            key: [str(item or "").strip() for item in list(raw.get(key) or []) if str(item or "").strip()]
            for key in ("keywords", "avoid_keywords", "preferred_reachability_modes", "school_stage_preferences", "desired_project_stages")
            if isinstance(raw.get(key), (list, tuple, set))
        }
        promoted_flags = {
            key: (
                raw.get(key) is True
                or str(raw.get(key) or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}
            )
            for key in (
                "use_stored_feedback_preferences",
                "include_broker_direct_sources",
                "include_community_signals",
                "require_manual_validation_for_community",
                "include_developer_project_signals",
                "include_public_housing_signals",
                "include_distressed_sale_signals",
                "enable_building_risk_research",
                "enable_market_supply_research",
                "enable_location_risk_research",
                "enable_trust_risk_scoring",
                "enable_family_mode",
                "enable_commute_research",
                "apply_unknowns_penalty",
                "enable_action_readiness_research",
                "enable_lifestyle_research",
                "prefer_good_air_quality",
                "prefer_heat_resilient_home",
                "prefer_air_conditioning",
                "prefer_attic_apartment",
                "avoid_attic_apartment",
                "prefer_low_crime_area",
                "require_drinking_water_quality_research",
                "require_parking_pressure_check",
                "avoid_cesspit_or_septic_risk",
                "require_winter_access_research",
                "avoid_flood_risk_area",
                "require_floorplan",
                "require_barrier_free",
                "use_flatbee_reputation_penalty",
            )
            if key in raw
        }
        normalized_preferences = {
            "country_code": country_code,
            "region_code": region_code,
            "language_code": language_code,
            "listing_mode": listing_mode,
            "property_type": property_type,
            "location_query": location_query,
            "selected_location_values": [] if full_region_scope else selected_location_values,
            "full_region_scope": full_region_scope,
            "search_mode": search_mode,
            "selected_platforms": selected_platforms,
            "max_results_per_source": normalized_max,
            "preference_person_id": preference_person_id,
            "search_agent_enabled": search_agent_enabled,
            "search_agent_duration_days": search_agent_duration_days,
            "search_agent_notification_limit": notification_limit,
            "search_agent_notification_period": notification_period,
            "property_commercial": property_commercial,
            "saved_shortlist_candidates": saved_shortlist_candidates,
            "saved_shortlist_share_slug": saved_shortlist_share_slug,
            "raw_preferences": cleaned_raw,
            **promoted_numeric,
            **promoted_strings,
            **promoted_lists,
            **promoted_flags,
        }
        if removed_platform_details:
            normalized_preferences["provider_selection_filter_applied"] = True
            normalized_preferences["provider_selection_filter_removed"] = [
                str(row.get("platform") or "").strip()
                for row in removed_platform_details
                if str(row.get("platform") or "").strip()
            ]
            normalized_preferences["provider_selection_filter_removed_details"] = [dict(row) for row in removed_platform_details]
        normalized_preferences["active_search_agent_id"] = str(raw.get("active_search_agent_id") or "").strip()
        explicit_agents = isinstance(raw_search_agents, (list, tuple))
        if explicit_agents:
            normalized_preferences["search_agents"] = OnboardingService._normalize_property_search_agents(
                {
                    **cleaned_raw,
                    **normalized_preferences,
                    # Keep the canonical collection out of raw_preferences,
                    # but pass it explicitly to the agent normalizer.  Without
                    # this handoff a full-state save silently replaces every
                    # existing agent with a generated default.
                    "search_agents": raw_search_agents,
                },
                active_agent_id=str(normalized_preferences.get("active_search_agent_id") or "").strip(),
                # This is the canonical durable snapshot. Keep every explicit
                # saved search across downgrades and imports; callers that
                # schedule execution retain the default plan-limited view.
                enforce_plan_limit=False,
            )
            if normalized_preferences["search_agents"] and not normalized_preferences["active_search_agent_id"]:
                normalized_preferences["active_search_agent_id"] = str(normalized_preferences["search_agents"][0].get("agent_id") or "")
        else:
            normalized_preferences["search_agents"] = OnboardingService._normalize_property_search_agents(
                normalized_preferences,
                active_agent_id=str(normalized_preferences.get("active_search_agent_id") or "").strip(),
            )
            if normalized_preferences["search_agents"] and not normalized_preferences["active_search_agent_id"]:
                normalized_preferences["active_search_agent_id"] = str(normalized_preferences["search_agents"][0].get("agent_id") or "")
        return normalized_preferences

    @staticmethod
    def _normalize_property_search_agents(
        preferences: dict[str, object] | None,
        *,
        active_agent_id: str = "",
        enforce_plan_limit: bool = True,
    ) -> list[dict[str, object]]:
        payload = dict(preferences or {})
        raw_agents = payload.get("search_agents")
        explicit_agents = isinstance(raw_agents, (list, tuple))
        agents: list[dict[str, object]] = []
        if isinstance(raw_agents, (list, tuple)):
            for raw_agent in raw_agents:
                if not isinstance(raw_agent, dict):
                    continue
                agents.append(OnboardingService._normalize_property_search_agent(raw_agent, fallback=payload))
        if not agents and not explicit_agents:
            agents.append(OnboardingService._normalize_property_search_agent(payload, fallback=payload))
        agent_limit = OnboardingService._property_search_agent_limit(payload) if enforce_plan_limit else 0
        seen: set[str] = set()
        deduped: list[dict[str, object]] = []
        for agent in agents:
            if agent_limit > 0 and len(deduped) >= agent_limit:
                break
            agent_id = str(agent.get("agent_id") or "").strip()
            if not agent_id or agent_id in seen:
                property_type_seed = agent.get("property_type")
                if isinstance(property_type_seed, (list, tuple, set)):
                    property_type_seed = ",".join(
                        str(item or "").strip().lower()
                        for item in property_type_seed
                        if str(item or "").strip()
                    )
                else:
                    property_type_seed = str(property_type_seed or "")
                seed = "|".join(
                    str(agent.get(key) or "")
                    if key != "property_type"
                    else property_type_seed
                    for key in ("name", "country_code", "region_code", "location_query", "listing_mode", "property_type")
                )
                agent_id = f"agent-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"
                agent["agent_id"] = agent_id
            if agent_id in seen:
                continue
            seen.add(agent_id)
            agent["is_active"] = agent_id == active_agent_id or (not active_agent_id and not deduped)
            deduped.append(agent)
        return deduped

    @staticmethod
    def _property_search_agent_limit(preferences: dict[str, object] | None) -> int:
        try:
            snapshot = property_commercial_snapshot(dict(preferences or {}))
            return max(0, int(snapshot.get("search_agent_limit") or 0))
        except Exception:
            return 1

    @staticmethod
    def _search_agent_preferences_payload(value: dict[str, object] | None) -> dict[str, object]:
        payload = {
            key: item
            for key, item in dict(value or {}).items()
            if key not in {"search_agents", "active_search_agent_id", "raw_preferences", "property_commercial", "saved_shortlist_candidates", "saved_shortlist_share_slug", "min_match_score"}
        }
        return payload

    @staticmethod
    def _normalize_property_search_agent(
        value: dict[str, object],
        *,
        fallback: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raw = dict(value or {})
        base = dict(fallback or {})
        commercial_source = (
            dict(raw.get("property_commercial") or {})
            if isinstance(raw.get("property_commercial"), dict)
            else (
                dict(base.get("property_commercial") or {})
                if isinstance(base.get("property_commercial"), dict)
                else {}
            )
        )
        commercial_snapshot = property_commercial_snapshot({"property_commercial": commercial_source} if commercial_source else {})
        plan_key = str(commercial_snapshot.get("current_plan_key") or "free").strip().lower() or "free"
        try:
            plan_result_cap = int(commercial_snapshot.get("max_results_per_source") or 0)
        except Exception:
            plan_result_cap = 0
        country_code = normalize_country_code(raw.get("country_code") or base.get("country_code"))
        region_code = str(raw.get("region_code") or base.get("region_code") or "").strip().lower()
        location_query = str(raw.get("location_query") or base.get("location_query") or "").strip()
        raw_full_region_scope = raw.get("full_region_scope", base.get("full_region_scope"))
        full_region_scope = (
            raw_full_region_scope is True
            or str(raw_full_region_scope or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled"}
        )
        listing_mode = normalize_listing_mode(raw.get("listing_mode") or base.get("listing_mode"))
        property_type = normalize_property_type_values(raw.get("property_type") or base.get("property_type"))
        raw_selected_location_values = (
            raw.get("selected_location_values")
            if isinstance(raw.get("selected_location_values"), (list, tuple, set))
            else base.get("selected_location_values")
        )
        selected_location_values = [
            normalized
            for normalized in dict.fromkeys(str(item or "").strip() for item in list(raw_selected_location_values or []))
            if normalized
        ]
        if full_region_scope:
            selected_location_values = []
        try:
            duration_days = max(7, min(365, int(float(str(raw.get("duration_days") or raw.get("search_agent_duration_days") or base.get("search_agent_duration_days") or 30).strip()))))
        except Exception:
            duration_days = 30
        notification_period = str(raw.get("notification_period") or raw.get("search_agent_notification_period") or base.get("search_agent_notification_period") or "day").strip().lower()
        if notification_period not in {"day", "week"}:
            notification_period = "day"
        try:
            notification_limit = max(1, min(50, int(float(str(raw.get("notification_limit") or raw.get("search_agent_notification_limit") or base.get("search_agent_notification_limit") or 5).strip()))))
        except Exception:
            notification_limit = 5
        raw_selected_platforms = raw.get("selected_platforms") if isinstance(raw.get("selected_platforms"), (list, tuple, set)) else base.get("selected_platforms")
        allowed_platforms = {
            str(option.get("value") or "").strip().lower()
            for option in provider_options(country_code=country_code)
            if str(option.get("value") or "").strip()
        }
        selected_platforms = [
            normalized
            for normalized in dict.fromkeys(normalize_property_platform(item) for item in list(raw_selected_platforms or []))
            if normalized and (not allowed_platforms or normalized in allowed_platforms)
        ]
        enabled = raw.get("enabled")
        if enabled is None:
            enabled = raw.get("search_agent_enabled", base.get("search_agent_enabled"))
        enabled_bool = enabled is True or str(enabled or "").strip().lower() in {"1", "true", "yes", "y", "on", "enabled", "active"}
        name = str(raw.get("name") or "").strip()
        if not name:
            label_location = location_query or country_code
            name = f"{listing_mode.title()} search · {label_location}"
        seed = "|".join([name, country_code, region_code, location_query, listing_mode, ",".join(property_type)])
        agent_id = str(raw.get("agent_id") or raw.get("id") or "").strip() or f"agent-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"
        try:
            sent_in_current_window = max(
                0,
                int(float(str(raw.get("sent_in_current_window") or base.get("sent_in_current_window") or 0).strip() or "0")),
            )
        except Exception:
            sent_in_current_window = 0
        preferences_json = (
            OnboardingService._search_agent_preferences_payload(dict(raw.get("preferences_json") or {}))
            if isinstance(raw.get("preferences_json"), dict)
            else {}
        )
        if not preferences_json:
            preferences_json = OnboardingService._search_agent_preferences_payload(base)
        if property_plan_has_unlimited_provider_results(plan_key, plan_result_cap):
            preferences_json.pop("max_results_per_source", None)
        elif "max_results_per_source" in preferences_json:
            try:
                preferences_json["max_results_per_source"] = max(
                    1,
                    min(plan_result_cap or 10, int(float(str(preferences_json.get("max_results_per_source") or "").strip()))),
                )
            except Exception:
                preferences_json.pop("max_results_per_source", None)
        preferences_json.update(
            {
                "country_code": country_code,
                "region_code": region_code,
                "location_query": location_query,
                "selected_location_values": selected_location_values,
                "full_region_scope": full_region_scope,
                "listing_mode": listing_mode,
                "property_type": property_type,
                "selected_platforms": selected_platforms,
                "search_agent_enabled": enabled_bool,
                "search_agent_duration_days": duration_days,
                "search_agent_notification_limit": notification_limit,
                "search_agent_notification_period": notification_period,
            }
        )
        return {
            "agent_id": agent_id,
            "name": name[:120],
            "enabled": enabled_bool,
            "status": "active" if enabled_bool else "paused",
            "country_code": country_code,
            "region_code": region_code,
            "location_query": location_query,
            "selected_location_values": selected_location_values,
            "full_region_scope": full_region_scope,
            "listing_mode": listing_mode,
            "property_type": property_type,
            "selected_platforms": selected_platforms,
            "provider_count": len(selected_platforms),
            "duration_days": duration_days,
            "notification_limit": notification_limit,
            "notification_period": notification_period,
            "last_run_at": str(raw.get("last_run_at") or base.get("last_run_at") or "").strip(),
            "next_run_at": str(raw.get("next_run_at") or base.get("next_run_at") or "").strip(),
            "sent_in_current_window": sent_in_current_window,
            "preferences_json": preferences_json,
        }

    @staticmethod
    def _normalize_clock_time(value: str, *, default: str) -> str:
        normalized = str(value or "").strip()
        hour, sep, minute = normalized.partition(":")
        try:
            hour_int = int(hour)
            minute_int = int(minute) if sep else 0
        except Exception:
            return default
        if 0 <= hour_int <= 23 and 0 <= minute_int <= 59:
            return f"{hour_int:02d}:{minute_int:02d}"
        return default

    @staticmethod
    def _google_binding_email(google_binding) -> str:  # type: ignore[no-untyped-def]
        if google_binding is None:
            return ""
        return str(
            dict(getattr(google_binding, "auth_metadata_json", {}) or {}).get("google_email")
            or getattr(google_binding, "external_account_ref", "")
            or ""
        ).strip().lower()

    def _load_morning_memo_preference(self, *, principal_id: str):
        for row in self._memory_runtime.list_delivery_preferences(principal_id=principal_id, limit=50):
            if str(dict(row.format_json or {}).get("schedule_kind") or "").strip().lower() == "morning_memo":
                return row
        return None

    def _morning_memo_schedule(
        self,
        *,
        principal_id: str,
        state: OnboardingState | None,
        google_binding,
    ) -> dict[str, object]:
        preference = self._load_morning_memo_preference(principal_id=principal_id)
        quiet_hours = dict(getattr(preference, "quiet_hours_json", {}) or {}) if preference is not None else {}
        format_json = dict(getattr(preference, "format_json", {}) or {}) if preference is not None else {}
        privacy = dict(state.privacy_preferences_json) if state is not None else {}
        explicit_email = str(format_json.get("recipient_email") or "").strip().lower()
        resolved_google_email = self._google_binding_email(google_binding)
        resolved_email = explicit_email or resolved_google_email
        cadence = (
            self._normalize_auto_brief_cadence(str(getattr(preference, "cadence", "") or DEFAULT_AUTO_BRIEF_CADENCE))
            if preference is not None
            else DEFAULT_AUTO_BRIEF_CADENCE
        )
        delivery_channel = self._normalize_auto_brief_delivery_channel(
            str(format_json.get("delivery_channel") or getattr(preference, "channel", "") or "email")
        )
        delivery_time_local = self._normalize_clock_time(
            str(quiet_hours.get("delivery_time_local") or ""),
            default=DEFAULT_AUTO_BRIEF_DELIVERY_TIME_LOCAL,
        )
        quiet_hours_start = self._normalize_clock_time(
            str(quiet_hours.get("quiet_hours_start") or ""),
            default=DEFAULT_AUTO_BRIEF_QUIET_HOURS_START,
        )
        quiet_hours_end = self._normalize_clock_time(
            str(quiet_hours.get("quiet_hours_end") or ""),
            default=DEFAULT_AUTO_BRIEF_QUIET_HOURS_END,
        )
        delivery_window_minutes = max(int(quiet_hours.get("delivery_window_minutes") or DEFAULT_AUTO_BRIEF_DELIVERY_WINDOW_MINUTES), 15)
        schedule_status = str(getattr(preference, "status", "") or ("active" if privacy.get("allow_auto_briefs") else "disabled")).strip().lower()
        if schedule_status not in {"active", "disabled"}:
            schedule_status = "active" if privacy.get("allow_auto_briefs") else "disabled"
        return {
            "enabled": schedule_status == "active" and bool(privacy.get("allow_auto_briefs")),
            "status": schedule_status,
            "cadence": cadence,
            "delivery_channel": delivery_channel,
            "delivery_time_local": delivery_time_local,
            "quiet_hours_start": quiet_hours_start,
            "quiet_hours_end": quiet_hours_end,
            "delivery_window_minutes": delivery_window_minutes,
            "timezone": str(quiet_hours.get("timezone") or (state.timezone if state is not None else "") or "UTC"),
            "recipient_ref": str(getattr(preference, "recipient_ref", "") or AUTO_BRIEF_RECIPIENT_REF),
            "recipient_email": explicit_email,
            "resolved_recipient_email": resolved_email,
            "recipient_target": str(format_json.get("recipient_target") or ("explicit_email" if explicit_email else "google_primary")),
            "retry_after_minutes": max(int(format_json.get("retry_after_minutes") or DEFAULT_AUTO_BRIEF_RETRY_AFTER_MINUTES), 5),
            "digest_key": str(format_json.get("digest_key") or "memo"),
            "ready": bool(resolved_email),
        }

    def _upsert_morning_memo_delivery_preference(
        self,
        *,
        principal_id: str,
        state: OnboardingState,
        google_binding,
        allow_auto_briefs: bool,
        cadence: str,
        delivery_time_local: str,
        quiet_hours_start: str,
        quiet_hours_end: str,
        recipient_email: str,
        delivery_channel: str,
    ) -> None:
        explicit_email = str(recipient_email or "").strip().lower()
        resolved_google_email = self._google_binding_email(google_binding)
        recipient_target = "explicit_email" if explicit_email else ("google_primary" if resolved_google_email else "google_primary")
        self._memory_runtime.upsert_delivery_preference(
            principal_id=principal_id,
            channel=self._normalize_auto_brief_delivery_channel(delivery_channel),
            recipient_ref=AUTO_BRIEF_RECIPIENT_REF,
            cadence=self._normalize_auto_brief_cadence(cadence),
            quiet_hours_json={
                "timezone": str(state.timezone or "").strip() or "UTC",
                "delivery_time_local": self._normalize_clock_time(
                    delivery_time_local,
                    default=DEFAULT_AUTO_BRIEF_DELIVERY_TIME_LOCAL,
                ),
                "quiet_hours_start": self._normalize_clock_time(
                    quiet_hours_start,
                    default=DEFAULT_AUTO_BRIEF_QUIET_HOURS_START,
                ),
                "quiet_hours_end": self._normalize_clock_time(
                    quiet_hours_end,
                    default=DEFAULT_AUTO_BRIEF_QUIET_HOURS_END,
                ),
                "delivery_window_minutes": DEFAULT_AUTO_BRIEF_DELIVERY_WINDOW_MINUTES,
            },
            format_json={
                "schedule_kind": "morning_memo",
                "digest_key": "memo",
                "role": "principal",
                "display_name": str(state.workspace_name or "Workspace Principal").strip() or "Workspace Principal",
                "recipient_email": explicit_email,
                "recipient_target": recipient_target,
                "delivery_channel": self._normalize_auto_brief_delivery_channel(delivery_channel),
                "retry_after_minutes": DEFAULT_AUTO_BRIEF_RETRY_AFTER_MINUTES,
            },
            status="active" if allow_auto_briefs else "disabled",
        )

    @staticmethod
    def _normalize_channels(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        allowed = {"google", "telegram", "whatsapp"}
        normalized = sorted({str(value or "").strip().lower() for value in values if str(value or "").strip().lower() in allowed})
        return tuple(normalized)

    @staticmethod
    def _normalize_workspace_mode(value: str) -> str:
        normalized = str(value or "personal").strip().lower() or "personal"
        if normalized.endswith("_creator_ops"):
            return "executive_ops"
        return WORKSPACE_MODE_ALIASES.get(normalized, "personal")

    @staticmethod
    def _top_themes_for_mode(workspace_mode: str, selected_channels: list[str]) -> tuple[str, ...]:
        normalized_mode = OnboardingService._normalize_workspace_mode(workspace_mode)
        base: list[str]
        if normalized_mode == "team":
            base = [
                "Stakeholder replies that still need an owner",
                "Meeting prep and recap across the channels already connected",
                "Handoffs that stay visible instead of sliding back into inbox drift",
            ]
        elif normalized_mode == "executive_ops":
            base = [
                "Saved-search updates that connect ranking, alerts, and follow-up",
                "Decision memory that keeps past shortlist feedback useful",
                "Property questions, notes, and next steps tied to the source listing",
            ]
        else:
            base = [
                "One clear search brief",
                "Matching homes in the selected areas and budget",
                "Feedback that improves the next shortlist",
            ]
        if "google" in selected_channels:
            base.append("Mail triage with calendar-aware context")
        if "telegram" in selected_channels:
            base.append("DM versus group urgency on Telegram")
        if "whatsapp" in selected_channels:
            base.append("WhatsApp threads that need an explicit commitment or import decision")
        return tuple(base)


def _backend_mode(settings: Settings) -> str:
    return str(settings.storage.backend or "auto").strip().lower()


def build_onboarding_repo(settings: Settings) -> OnboardingStateRepository:
    backend = _backend_mode(settings)
    log = logging.getLogger("ea.onboarding")
    if backend == "memory":
        ensure_storage_fallback_allowed(settings, "onboarding configured for memory")
        return InMemoryOnboardingStateRepository()
    if backend == "postgres":
        if not settings.database_url:
            raise RuntimeError("EA_STORAGE_BACKEND=postgres requires DATABASE_URL")
        return PostgresOnboardingStateRepository(settings.database_url)
    if settings.database_url:
        try:
            return PostgresOnboardingStateRepository(settings.database_url)
        except Exception as exc:
            ensure_storage_fallback_allowed(settings, "onboarding auto fallback", exc)
            log.warning("postgres onboarding backend unavailable in auto mode; falling back to memory: %s", exc)
    ensure_storage_fallback_allowed(settings, "onboarding auto backend without DATABASE_URL")
    return InMemoryOnboardingStateRepository()


def build_onboarding_service(
    *,
    settings: Settings | None = None,
    provider_registry: ProviderRegistryService,
    tool_runtime: ToolRuntimeService,
    memory_runtime: MemoryRuntimeService,
) -> OnboardingService:
    resolved = settings or get_settings()
    return OnboardingService(
        onboarding_repo=build_onboarding_repo(resolved),
        provider_registry=provider_registry,
        tool_runtime=tool_runtime,
        memory_runtime=memory_runtime,
        settings=resolved,
    )
