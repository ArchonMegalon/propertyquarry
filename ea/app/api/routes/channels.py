from __future__ import annotations

import ast
import concurrent.futures
import contextlib
import hashlib
import hmac
import json
import os
import re
import time
import threading
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from importlib import import_module
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, Depends, Request
from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.api.dependencies import RequestContext
from app.api.dependencies import get_container
from app.channels.telegram.adapter import TelegramObservationAdapter
from app.container import AppContainer
from app.domain.models import ToolInvocationRequest
from app.product import service as product_service_module
from app.product.projections import compact_text
from app.product.service import build_product_service
from app.services import google_oauth as google_oauth_service
from app.services.ltd_runtime_catalog import LtdRuntimeCatalogService
from app.services.ltd_runtime_skill_projection import projected_task_key, projected_task_key_for_request
from app.services.property_billing import property_commercial_snapshot
from app.services.telegram_session_service import (
    TelegramLocalResolver,
    TelegramReplyMemoryState,
    TelegramTurnContext,
    TelegramTurnDecision,
    build_turn_context,
    resolve_telegram_message_payload,
    run_local_resolvers,
)
from app.services.telegram_onboarding_service import TELEGRAM_IDENTITY_CONNECTOR, TELEGRAM_OFFICIAL_BOT_CONNECTOR
from app.services.telegram_delivery import (
    _telegram_html_with_titled_links,
    _telegram_visible_button_label,
    decode_telegram_feedback_callback_data,
)

router = APIRouter(prefix="/v1/channels", tags=["channels"])
_telegram = TelegramObservationAdapter()
_SAFE_MATH_RE = re.compile(r"^[0-9\.\+\-\*\/\(\)\s=\?]+$")
_TELEGRAM_ASSISTANT_ACK = "Let me check that and get back to you here."


def _telegram_env_int(env_key: str, fallback: int) -> int:
    raw = str(os.getenv(env_key) or "").strip()
    try:
        return max(int(raw or str(fallback)), 1)
    except Exception:
        return max(fallback, 1)


_TELEGRAM_ASYNC_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="telegram-ea")
_TELEGRAM_PAID_RENDER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_telegram_env_int("EA_TELEGRAM_PAID_RENDER_WORKERS", 2),
    thread_name_prefix="telegram-ea-paid-render",
)
_TELEGRAM_FREE_RENDER_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=_telegram_env_int("EA_TELEGRAM_FREE_RENDER_WORKERS", 1),
    thread_name_prefix="telegram-ea-free-render",
)
_TELEGRAM_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
_URL_RE = re.compile(r"https?://[^\s<>\"]+")
_MATH_WORD_NUMBERS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
}
_TELEGRAM_MONTH_ALIASES = {
    "jan": 1,
    "january": 1,
    "jänner": 1,
    "jaenner": 1,
    "januar": 1,
    "feb": 2,
    "february": 2,
    "februar": 2,
    "mar": 3,
    "march": 3,
    "märz": 3,
    "maerz": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "mai": 5,
    "jun": 6,
    "june": 6,
    "juni": 6,
    "jul": 7,
    "july": 7,
    "juli": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "okt": 10,
    "october": 10,
    "oktober": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "dez": 12,
    "december": 12,
    "dezember": 12,
}

def _responses_route_module():
    return import_module("app.api.routes.responses")


responses_route = _responses_route_module()


def _telegram_bot_registry() -> dict[str, dict[str, object]]:
    registry: dict[str, dict[str, object]] = {}
    raw_registry = str(os.getenv("EA_TELEGRAM_BOT_REGISTRY_JSON") or "").strip()
    if raw_registry:
        try:
            parsed = json.loads(raw_registry)
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            for raw_key, raw_value in parsed.items():
                key = str(raw_key or "").strip()
                if not key or not isinstance(raw_value, dict):
                    continue
                token = str(raw_value.get("token") or "").strip()
                if not token:
                    continue
                registry[key] = {
                    "bot_key": key,
                    "token": token,
                    "handle": str(raw_value.get("handle") or "").strip(),
                    "secret": str(raw_value.get("secret") or "").strip(),
                    "default_principal_id": str(raw_value.get("default_principal_id") or "").strip(),
                    "auto_bind_unknown_chat": bool(raw_value.get("auto_bind_unknown_chat")),
                    "preferred_onemin_labels": tuple(
                        str(item or "").strip()
                        for item in list(raw_value.get("preferred_onemin_labels") or [])
                        if str(item or "").strip()
                    ),
                }
    default_token = str(os.getenv("EA_TELEGRAM_BOT_TOKEN") or "").strip()
    if default_token:
        registry.setdefault(
            "default",
            {
                "bot_key": "default",
                "token": default_token,
                "handle": str(os.getenv("EA_TELEGRAM_BOT_HANDLE") or "").strip(),
                "secret": str(os.getenv("EA_TELEGRAM_INGEST_SECRET") or "").strip(),
                "default_principal_id": str(os.getenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID") or "").strip(),
                "auto_bind_unknown_chat": _telegram_auto_bind_unknown_chat_enabled(),
                "preferred_onemin_labels": (),
            },
        )
    return registry


def _telegram_default_preferred_onemin_labels() -> tuple[str, ...]:
    raw = str(os.getenv("EA_TELEGRAM_RESPONSES_PREFERRED_ONEMIN_LABELS") or "").strip()
    if not raw:
        return ()
    labels: list[str] = []
    for item in raw.split(","):
        normalized = str(item or "").strip()
        if normalized and normalized not in labels:
            labels.append(normalized)
    return tuple(labels)


def _resolve_telegram_bot_config(*, bot_key: str = "", provided_secret: str = "", header_secret: str = "") -> dict[str, object]:
    registry = _telegram_bot_registry()
    normalized_key = str(bot_key or "").strip()
    if normalized_key:
        config = dict(registry.get(normalized_key) or {})
        if not config:
            raise HTTPException(status_code=404, detail="telegram_bot_not_found")
        return config
    if not registry:
        return {}
    for config in registry.values():
        secret = str(config.get("secret") or "").strip()
        if not secret:
            continue
        for candidate in (str(header_secret or "").strip(), str(provided_secret or "").strip()):
            if candidate and hmac.compare_digest(candidate, secret):
                return dict(config)
    return dict(registry.get("default") or next(iter(registry.values())))


def _require_telegram_ingest_secret(*, config: dict[str, object], provided: str, header_value: str) -> None:
    expected = str(config.get("secret") or os.getenv("EA_TELEGRAM_INGEST_SECRET") or "").strip()
    if not expected:
        return
    candidates = (str(header_value or "").strip(), str(provided or "").strip())
    for candidate in candidates:
        if candidate and hmac.compare_digest(candidate, expected):
            return
    raise HTTPException(status_code=403, detail="telegram_secret_invalid")


def _resolve_telegram_principal(container: AppContainer, chat_id: str, *, bot_key: str = "", bot_handle: str = "") -> str:
    normalized_chat_id = str(chat_id or "").strip()
    if not normalized_chat_id:
        return ""
    matches: list[str] = []
    for connector_name in (TELEGRAM_OFFICIAL_BOT_CONNECTOR, TELEGRAM_IDENTITY_CONNECTOR):
        for binding in container.tool_runtime.list_connector_bindings_for_connector(connector_name, limit=500):
            normalized_status = str(binding.status or "").strip().lower()
            if normalized_status in {"disabled", "inactive", "archived"}:
                continue
            metadata = dict(binding.auth_metadata_json or {})
            metadata_bot_key = str(metadata.get("bot_key") or "").strip()
            metadata_bot_handle = str(metadata.get("bot_handle") or "").strip()
            if bot_key and metadata_bot_key and metadata_bot_key != bot_key:
                continue
            if bot_handle and metadata_bot_handle and metadata_bot_handle != bot_handle:
                continue
            default_chat_ref = str(metadata.get("default_chat_ref") or "").strip()
            external_account_ref = str(binding.external_account_ref or "").strip()
            if normalized_chat_id in {default_chat_ref, external_account_ref}:
                matches.append(binding.principal_id)
    principals = sorted({principal_id for principal_id in matches if str(principal_id or "").strip()})
    if len(principals) == 1:
        return principals[0]
    if len(principals) > 1:
        raise HTTPException(status_code=409, detail="telegram_binding_ambiguous")
    return ""


def _telegram_principal_is_registered_user(
    container: AppContainer,
    principal_id: str,
    *,
    accepted_default_principal_id: str = "",
) -> bool:
    normalized = str(principal_id or "").strip()
    if not normalized:
        return False
    accepted_default = str(accepted_default_principal_id or _telegram_default_principal_id()).strip()
    if accepted_default and normalized == accepted_default:
        return True
    with contextlib.suppress(Exception):
        status = container.onboarding.status(principal_id=normalized)
        normalized_status = str(status.get("status") or "").strip().lower()
        return bool(normalized_status) and normalized_status != "draft"
    return False


def _telegram_property_render_plan_key(container: AppContainer, principal_id: str) -> str:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        return "free"
    with contextlib.suppress(Exception):
        status = container.onboarding.status(principal_id=normalized_principal)
        preferences = dict(status.get("property_search_preferences") or {})
        snapshot = property_commercial_snapshot(preferences)
        return str(snapshot.get("current_plan_key") or "free").strip().lower() or "free"
    return "free"


def _telegram_property_render_priority(container: AppContainer, principal_id: str) -> str:
    return "paid" if _telegram_property_render_plan_key(container=container, principal_id=principal_id) in {"plus", "agent"} else "free"


def _telegram_default_principal_id() -> str:
    return str(os.getenv("EA_TELEGRAM_DEFAULT_PRINCIPAL_ID") or "").strip()


def _telegram_bot_handle() -> str:
    return str(os.getenv("EA_TELEGRAM_BOT_HANDLE") or "").strip()


def _telegram_auto_bind_unknown_chat_enabled() -> bool:
    normalized = str(os.getenv("EA_TELEGRAM_AUTO_BIND_UNKNOWN_CHAT") or "").strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _telegram_inline_async_accelerator_enabled() -> bool:
    normalized = str(os.getenv("EA_TELEGRAM_INLINE_ASYNC_ACCELERATOR") or "1").strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _auto_bind_telegram_chat(container: AppContainer, chat_id: str, *, config: dict[str, object]) -> str:
    normalized_chat_id = str(chat_id or "").strip()
    principal_id = str(config.get("default_principal_id") or _telegram_default_principal_id() or "").strip()
    auto_bind = config.get("auto_bind_unknown_chat")
    if auto_bind is None:
        auto_bind_enabled = _telegram_auto_bind_unknown_chat_enabled()
    else:
        auto_bind_enabled = bool(auto_bind)
    if not normalized_chat_id or not principal_id or not auto_bind_enabled:
        return ""
    if not _telegram_principal_is_registered_user(
        container=container,
        principal_id=principal_id,
        accepted_default_principal_id=str(config.get("default_principal_id") or "").strip(),
    ):
        return ""
    connector = container.tool_runtime.upsert_connector_binding(
        principal_id=principal_id,
        connector_name=TELEGRAM_IDENTITY_CONNECTOR,
        external_account_ref=normalized_chat_id,
        scope_json={"assistant_surfaces": ["dm"]},
        auth_metadata_json={
            "identity_mode": "bot_webhook",
            "history_mode": "future_only",
            "default_chat_ref": normalized_chat_id,
            "status": "enabled",
            "bot_handle": str(config.get("handle") or _telegram_bot_handle() or "").strip(),
            "bot_key": str(config.get("bot_key") or "").strip(),
            "auto_bound": True,
        },
        status="enabled",
    )
    return str(connector.principal_id or "").strip()


def _telegram_inline_keyboard(button_rows: list[list[tuple[str, str]]]) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": _telegram_visible_button_label(str(label or "")), "callback_data": str(callback_data or "").strip()}
                for label, callback_data in row
                if str(label or "").strip() and str(callback_data or "").strip()
            ]
            for row in button_rows
            if row
        ]
    }


def _telegram_callback_secret(*, bot_config: dict[str, object]) -> str:
    return (
        str(os.getenv("EA_TELEGRAM_CALLBACK_SECRET") or "").strip()
        or str(bot_config.get("secret") or "").strip()
        or str(bot_config.get("token") or "").strip()
    )


def _telegram_callback_ttl_seconds() -> int:
    raw = str(os.getenv("EA_TELEGRAM_CALLBACK_TTL_SECONDS") or "3600").strip()
    try:
        return max(int(float(raw or "3600")), 60)
    except Exception:
        return 3600


def _telegram_callback_signature(
    *,
    secret: str,
    action: str,
    current_message_id: str,
    chat_id: str,
    expires_at: int,
) -> str:
    payload = "|".join(
        (
            str(action or "").strip(),
            str(current_message_id or "").strip(),
            str(chat_id or "").strip(),
            str(int(expires_at)),
        )
    )
    return hmac.new(
        str(secret or "").encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:12]


def _telegram_encode_callback_data(
    *,
    bot_config: dict[str, object],
    action: str,
    current_message_id: str,
    chat_id: str,
) -> str:
    secret = _telegram_callback_secret(bot_config=bot_config)
    normalized_action = str(action or "").strip().lower()
    normalized_message_id = str(current_message_id or "").strip()
    normalized_chat_id = str(chat_id or "").strip()
    if not secret or not normalized_action or not normalized_message_id or not normalized_chat_id:
        return ""
    expires_at = int(time.time()) + _telegram_callback_ttl_seconds()
    signature = _telegram_callback_signature(
        secret=secret,
        action=normalized_action,
        current_message_id=normalized_message_id,
        chat_id=normalized_chat_id,
        expires_at=expires_at,
    )
    return f"ea|{normalized_action}|{normalized_message_id}|{normalized_chat_id}|{expires_at}|{signature}"


def _telegram_decode_callback_data(
    *,
    bot_config: dict[str, object],
    callback_data: str,
    chat_id: str,
) -> dict[str, object]:
    normalized = str(callback_data or "").strip()
    parts = normalized.split("|")
    if len(parts) != 6 or parts[0] != "ea":
        return {"ok": False, "reason": "invalid_format"}
    _, action, current_message_id, encoded_chat_id, expires_at_raw, signature = parts
    if str(encoded_chat_id or "").strip() != str(chat_id or "").strip():
        return {"ok": False, "reason": "chat_mismatch"}
    try:
        expires_at = int(str(expires_at_raw or "").strip())
    except Exception:
        return {"ok": False, "reason": "invalid_expiry"}
    if expires_at < int(time.time()):
        return {"ok": False, "reason": "expired", "action": str(action or "").strip().lower()}
    secret = _telegram_callback_secret(bot_config=bot_config)
    if not secret:
        return {"ok": False, "reason": "missing_secret"}
    expected_signature = _telegram_callback_signature(
        secret=secret,
        action=str(action or "").strip().lower(),
        current_message_id=str(current_message_id or "").strip(),
        chat_id=str(chat_id or "").strip(),
        expires_at=expires_at,
    )
    if not hmac.compare_digest(str(signature or "").strip(), expected_signature):
        return {"ok": False, "reason": "invalid_signature"}
    return {
        "ok": True,
        "action": str(action or "").strip().lower(),
        "current_message_id": str(current_message_id or "").strip(),
        "chat_id": str(chat_id or "").strip(),
        "expires_at": expires_at,
    }


def _telegram_send_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    inline_buttons: list[list[tuple[str, str]]] | None = None,
) -> dict[str, object]:
    normalized_token = str(bot_token or "").strip()
    normalized_chat_id = str(chat_id or "").strip()
    normalized_text = str(text or "").strip()
    if not normalized_token or not normalized_chat_id or not normalized_text:
        return {}
    payload_dict: dict[str, object] = {
        "chat_id": normalized_chat_id,
        "text": _telegram_html_with_titled_links(normalized_text),
        "parse_mode": "HTML",
    }
    if inline_buttons:
        payload_dict["reply_markup"] = _telegram_inline_keyboard(inline_buttons)
    payload = json.dumps(payload_dict).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{normalized_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        timeout_seconds = max(float(str(os.getenv("EA_TELEGRAM_SEND_TIMEOUT_SECONDS") or "10").strip() or "10"), 1.0)
    except Exception:
        timeout_seconds = 10.0
    return _telegram_post_json_with_retries(request=request, timeout_seconds=timeout_seconds)


def _telegram_answer_callback_query(*, bot_token: str, callback_query_id: str, text: str = "") -> None:
    normalized_token = str(bot_token or "").strip()
    normalized_query_id = str(callback_query_id or "").strip()
    if not normalized_token or not normalized_query_id:
        return
    payload = json.dumps(
        {
            "callback_query_id": normalized_query_id,
            "text": str(text or "").strip()[:180],
            "show_alert": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{normalized_token}/answerCallbackQuery",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        timeout_seconds = max(float(str(os.getenv("EA_TELEGRAM_SEND_TIMEOUT_SECONDS") or "10").strip() or "10"), 1.0)
    except Exception:
        timeout_seconds = 10.0
    _telegram_post_json_with_retries(request=request, timeout_seconds=timeout_seconds, expect_json=False)
    return


def _telegram_transport_retry_attempts() -> int:
    raw = str(os.getenv("EA_TELEGRAM_TRANSPORT_RETRY_ATTEMPTS") or "3").strip()
    try:
        return max(int(float(raw or "3")), 1)
    except Exception:
        return 3


def _telegram_transport_retry_backoff_seconds() -> float:
    raw = str(os.getenv("EA_TELEGRAM_TRANSPORT_RETRY_BACKOFF_SECONDS") or "1.0").strip()
    try:
        return max(float(raw or "1.0"), 0.0)
    except Exception:
        return 1.0


def _telegram_property_link_bundle_poll_attempts() -> int:
    raw = str(os.getenv("EA_TELEGRAM_PROPERTY_LINK_BUNDLE_POLL_ATTEMPTS") or "8").strip()
    try:
        return max(int(float(raw or "8")), 0)
    except Exception:
        return 8


def _telegram_property_link_bundle_poll_backoff_seconds() -> float:
    raw = str(os.getenv("EA_TELEGRAM_PROPERTY_LINK_BUNDLE_POLL_BACKOFF_SECONDS") or "6.0").strip()
    try:
        return max(float(raw or "6.0"), 0.0)
    except Exception:
        return 6.0


def _telegram_post_json_with_retries(
    *,
    request: urllib.request.Request,
    timeout_seconds: float,
    expect_json: bool = True,
) -> dict[str, object]:
    attempts = _telegram_transport_retry_attempts()
    backoff_seconds = _telegram_transport_retry_backoff_seconds()
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if not expect_json:
                    return {}
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in {408, 409, 425, 429, 500, 502, 503, 504} or attempt >= attempts:
                raise
            last_error = exc
        except urllib.error.URLError as exc:
            if attempt >= attempts:
                raise
            last_error = exc
        except Exception:
            raise
        if backoff_seconds > 0:
            time.sleep(backoff_seconds * attempt)
    if last_error is not None:
        raise last_error
    return {}


def _safe_math_answer(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    candidate = normalized.replace("?", "").replace("=", "").strip()
    if not candidate:
        return ""
    if not _SAFE_MATH_RE.fullmatch(normalized):
        lowered = " ".join(candidate.lower().split())
        if lowered.startswith("what is "):
            lowered = lowered[8:].strip()
        elif lowered.startswith("what's "):
            lowered = lowered[7:].strip()
        elif lowered.startswith("calculate "):
            lowered = lowered[10:].strip()
        elif lowered.startswith("compute "):
            lowered = lowered[8:].strip()
        for word, value in _MATH_WORD_NUMBERS.items():
            lowered = re.sub(rf"\\b{re.escape(word)}\\b", value, lowered)
        replacements = (
            ("divided by", "/"),
            ("multiplied by", "*"),
            ("times", "*"),
            ("plus", "+"),
            ("minus", "-"),
            ("x", "*"),
        )
        for src, dest in replacements:
            lowered = lowered.replace(src, f" {dest} ")
        lowered = re.sub(r"[^0-9\.\+\-\*\/\(\)\s]", " ", lowered)
        lowered = " ".join(lowered.split())
        if not lowered or not _SAFE_MATH_RE.fullmatch(lowered):
            return ""
        candidate = lowered

    def _eval_node(node):  # type: ignore[no-untyped-def]
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = _eval_node(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left = _eval_node(node.left)
            right = _eval_node(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        raise ValueError("unsupported_math_expression")

    try:
        parsed = ast.parse(candidate, mode="eval")
        value = _eval_node(parsed)
    except Exception:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return f"{candidate} = {value}"


def _recent_telegram_texts(container: AppContainer, *, principal_id: str, limit: int = 12) -> list[str]:
    rows = container.channel_runtime.list_recent_observations(limit=limit, principal_id=principal_id)
    texts: list[str] = []
    for row in rows:
        if str(row.channel or "").strip() != "telegram":
            continue
        payload = dict(row.payload or {})
        text = str(payload.get("text") or "").strip()
        if text:
            texts.append(text)
    return texts


def _recent_telegram_reply_texts(container: AppContainer, *, principal_id: str, limit: int = 12) -> list[str]:
    rows = container.channel_runtime.list_recent_observations(limit=limit, principal_id=principal_id)
    texts: list[str] = []
    for row in rows:
        if str(row.channel or "").strip() != "telegram":
            continue
        if str(row.event_type or "").strip() != "telegram.reply_sent":
            continue
        payload = dict(row.payload or {})
        text = str(payload.get("reply_text") or "").strip()
        if text:
            texts.append(text)
    return texts


def _telegram_reply_marker_dedupe_key(dedupe_key: str) -> str:
    normalized = str(dedupe_key or "").strip()
    return f"{normalized}:reply_sent" if normalized else ""


def _telegram_async_marker_dedupe_key(dedupe_key: str) -> str:
    normalized = str(dedupe_key or "").strip()
    return f"{normalized}:assistant_async_started" if normalized else ""


def _telegram_reply_already_sent(container: AppContainer, *, principal_id: str, dedupe_key: str) -> bool:
    marker = _telegram_reply_marker_dedupe_key(dedupe_key)
    if not marker:
        return False
    return container.channel_runtime.find_observation_by_dedupe(marker, principal_id=principal_id) is not None


def _record_telegram_reply_sent(
    container: AppContainer,
    *,
    principal_id: str,
    chat_id: str,
    dedupe_key: str,
    reply_text: str,
    message_id: str,
    active_object_map: dict[str, str] | None = None,
    intent_state: dict[str, str] | None = None,
    comparison_state: dict[str, str] | None = None,
) -> None:
    marker = _telegram_reply_marker_dedupe_key(dedupe_key)
    if not marker:
        return
    container.channel_runtime.ingest_observation(
        principal_id=principal_id,
        channel="telegram",
        event_type="telegram.reply_sent",
        payload={
            "chat_id": chat_id,
            "reply_text": reply_text,
            "message_id": message_id,
            "dedupe_key": dedupe_key,
            "active_object_map": dict(active_object_map or {}),
            "intent_state": dict(intent_state or {}),
            "comparison_state": dict(comparison_state or {}),
        },
        source_id=f"telegram:{chat_id}" if chat_id else "telegram",
        external_id=str(message_id or "").strip(),
        dedupe_key=marker,
    )


def _telegram_async_already_started(container: AppContainer, *, principal_id: str, dedupe_key: str) -> bool:
    marker = _telegram_async_marker_dedupe_key(dedupe_key)
    if not marker:
        return False
    return container.channel_runtime.find_observation_by_dedupe(marker, principal_id=principal_id) is not None


def _telegram_similar_async_prompt_pending(
    container: AppContainer,
    *,
    principal_id: str,
    chat_id: str,
    text: str,
    window_seconds: int = 120,
) -> bool:
    normalized_text = " ".join(str(text or "").strip().lower().split())
    if not normalized_text:
        return False
    cutoff = datetime.now(ZoneInfo("UTC")).timestamp() - max(int(window_seconds), 1)
    for row in container.channel_runtime.list_recent_observations(limit=80, principal_id=principal_id):
        if str(row.channel or "").strip() != "telegram":
            continue
        if str(row.event_type or "").strip() != "telegram.reply_async_started":
            continue
        payload = dict(row.payload or {})
        if str(payload.get("chat_id") or "").strip() != str(chat_id or "").strip():
            continue
        prompt_text = " ".join(str(payload.get("prompt_text") or "").strip().lower().split())
        if prompt_text != normalized_text:
            continue
        created_at = _parse_isoish_datetime(getattr(row, "created_at", "") or "")
        if created_at is None:
            continue
        if created_at.timestamp() >= cutoff:
            return True
    return False


def _telegram_reply_fingerprint(reply_text: str) -> str:
    normalized = " ".join(str(reply_text or "").strip().split())
    if not normalized:
        return ""
    normalized = re.sub(r"https?://\S+", "<url>", normalized)
    reconnect_markers = (
        "Reconnect here if needed:",
        "Reconnect with Photos Picker here, once per Google account:",
        "Start here:",
    )
    for marker in reconnect_markers:
        if marker in normalized:
            normalized = normalized.split(marker, 1)[0].strip()
    return normalized


def _telegram_is_google_photos_picker_block_reply(reply_text: str) -> bool:
    lowered = _telegram_reply_fingerprint(reply_text).lower()
    return lowered.startswith("google photos picker access is connected for ") and (
        "google is still refusing picker sessions" in lowered
        or "i could not start a picker session right now" in lowered
    )


def _telegram_same_reply_recently_sent(
    container: AppContainer,
    *,
    principal_id: str,
    chat_id: str,
    reply_text: str,
    window_seconds: int = 180,
) -> bool:
    normalized_reply = _telegram_reply_fingerprint(reply_text)
    if not normalized_reply:
        return False
    cutoff = datetime.now(ZoneInfo("UTC")).timestamp() - max(int(window_seconds), 1)
    for row in container.channel_runtime.list_recent_observations(limit=80, principal_id=principal_id):
        if str(row.channel or "").strip() != "telegram":
            continue
        if str(row.event_type or "").strip() != "telegram.reply_sent":
            continue
        payload = dict(row.payload or {})
        if str(payload.get("chat_id") or "").strip() != str(chat_id or "").strip():
            continue
        prior_reply = _telegram_reply_fingerprint(str(payload.get("reply_text") or "").strip())
        if prior_reply != normalized_reply:
            continue
        created_at = _parse_isoish_datetime(getattr(row, "created_at", "") or "")
        if created_at is None:
            continue
        if created_at.timestamp() >= cutoff:
            return True
    return False


def _record_telegram_async_started(
    container: AppContainer,
    *,
    principal_id: str,
    chat_id: str,
    dedupe_key: str,
    prompt_text: str,
    current_message_id: str = "",
    bot_key: str = "",
    bot_handle: str = "",
) -> None:
    marker = _telegram_async_marker_dedupe_key(dedupe_key)
    if not marker:
        return
    container.channel_runtime.ingest_observation(
        principal_id=principal_id,
        channel="telegram",
        event_type="telegram.reply_async_started",
        payload={
            "chat_id": chat_id,
            "prompt_text": prompt_text,
            "dedupe_key": dedupe_key,
            "current_message_id": str(current_message_id or "").strip(),
            "bot_key": str(bot_key or "").strip(),
            "bot_handle": str(bot_handle or "").strip(),
            "turn_state": "queued",
            "delivery_mode": "durable_observation_outbox",
        },
        source_id=f"telegram:{chat_id}" if chat_id else "telegram",
        external_id=str(dedupe_key or "").strip(),
        dedupe_key=marker,
    )


def _record_telegram_async_processing(
    container: AppContainer,
    *,
    principal_id: str,
    chat_id: str,
    current_message_id: str,
    prompt_text: str,
) -> None:
    external_id = str(current_message_id or "").strip()
    if not external_id:
        return
    container.channel_runtime.ingest_observation(
        principal_id=principal_id,
        channel="telegram",
        event_type="telegram.reply_async_processing",
        payload={
            "chat_id": str(chat_id or "").strip(),
            "prompt_text": str(prompt_text or "").strip(),
            "turn_state": "processing",
        },
        source_id=f"telegram:{chat_id}" if chat_id else "telegram",
        external_id=external_id,
        dedupe_key=f"{external_id}:assistant_async_processing",
    )


def _record_telegram_async_failed(
    container: AppContainer,
    *,
    principal_id: str,
    chat_id: str,
    current_message_id: str,
    prompt_text: str,
    stage: str,
    error: str,
) -> None:
    external_id = str(current_message_id or "").strip()
    container.channel_runtime.ingest_observation(
        principal_id=principal_id,
        channel="telegram",
        event_type="telegram.reply_async_failed",
        payload={
            "chat_id": str(chat_id or "").strip(),
            "prompt_text": str(prompt_text or "").strip(),
            "stage": str(stage or "").strip(),
            "error": str(error or "").strip(),
            "turn_state": "failed",
        },
        source_id=f"telegram:{chat_id}" if chat_id else "telegram",
        external_id=external_id,
        dedupe_key=f"{external_id}:assistant_async_failed" if external_id else "",
    )


def _record_telegram_async_sent(
    container: AppContainer,
    *,
    principal_id: str,
    chat_id: str,
    current_message_id: str,
    prompt_text: str,
    reply_text: str,
    used_fallback_only: bool,
) -> None:
    external_id = str(current_message_id or "").strip()
    container.channel_runtime.ingest_observation(
        principal_id=principal_id,
        channel="telegram",
        event_type="telegram.reply_async_sent",
        payload={
            "chat_id": str(chat_id or "").strip(),
            "prompt_text": str(prompt_text or "").strip(),
            "reply_text": str(reply_text or "").strip(),
            "used_fallback_only": bool(used_fallback_only),
            "turn_state": "sent",
        },
        source_id=f"telegram:{chat_id}" if chat_id else "telegram",
        external_id=external_id,
        dedupe_key=f"{external_id}:assistant_async_sent" if external_id else "",
    )


def _telegram_general_reply_text(*, container: AppContainer, principal_id: str, text: str) -> str:
    normalized = str(text or "").strip()
    lower = normalized.lower()
    alpha = "".join(ch for ch in lower if ch.isalpha() or ch.isspace()).strip()
    if alpha in {"again", "repeat", "say that again", "repeat that", "once more"}:
        for previous_reply in _recent_telegram_reply_texts(container, principal_id=principal_id):
            previous_normalized = str(previous_reply or "").strip()
            if not previous_normalized:
                continue
            previous_lower = previous_normalized.lower()
            if previous_lower in {
                "let me check that and get back to you here.",
                "i am still working on that last message.",
            }:
                continue
            if previous_lower.startswith("i got it. i saved this in tibor's assistant flow"):
                continue
            return previous_normalized
        return "I do not have a useful previous answer to repeat yet."
    if lower in {"really", "really?"}:
        for previous in _recent_telegram_texts(container, principal_id=principal_id):
            previous_normalized = str(previous or "").strip()
            if previous_normalized.lower() in {"really", "really?"}:
                continue
            math_answer = _safe_math_answer(previous_normalized)
            if math_answer:
                return f"Yes. {math_answer}"
            if "http://" in previous_normalized or "https://" in previous_normalized:
                return "Yes. I captured the link and kept it in PropertyQuarry."
            break
        return "Yes. I captured your message in PropertyQuarry."
    if ("today" in lower and "day" in lower) or alpha in {"day", "today", "what day", "weekday"}:
        now = datetime.now(ZoneInfo("Europe/Vienna"))
        return f"Today is {now.strftime('%A, %d %B %Y')} in Vienna."
    if ("today" in lower and "date" in lower) or alpha in {"date", "today date", "what date"}:
        now = datetime.now(ZoneInfo("Europe/Vienna"))
        return f"Today's date is {now.strftime('%A, %d %B %Y')} in Vienna."
    if ("time" in lower and "what" in lower) or alpha in {"time", "current time", "what time"}:
        now = datetime.now(ZoneInfo("Europe/Vienna"))
        return f"It is {now.strftime('%H:%M')} in Vienna."
    weather_reply = _telegram_weather_reply_text(text=normalized)
    if weather_reply:
        return weather_reply
    return ""


def _telegram_photo_reply_text(payload: dict[str, object] | None = None) -> str:
    payload_dict = dict(payload or {})
    if str(payload_dict.get("kind") or "").strip().lower() != "photo":
        return ""
    analysis = dict(payload_dict.get("photo_analysis") or {})
    status = str(payload_dict.get("photo_analysis_status") or "").strip().lower()
    summary = str(payload_dict.get("analysis_summary") or analysis.get("summary") or "").strip()
    notable_details = [
        str(value).strip()
        for value in list(analysis.get("notable_details") or [])
        if str(value).strip()
    ]
    suggestions = [
        str(value).strip()
        for value in list(analysis.get("suggestions") or [])
        if str(value).strip()
    ]
    if summary:
        parts = [f"I got the photo. {summary}"]
        if notable_details:
            parts.append("Notable details: " + "; ".join(notable_details[:3]) + ".")
        if suggestions:
            parts.append(suggestions[0].rstrip(".") + ".")
        return " ".join(part.strip() for part in parts if part.strip()).strip()
    if status == "failed":
        return "I got the photo, but the image analysis failed on my side. Send it again or add a short caption and I’ll retry."
    return "I got the photo and saved it, but I do not have enough analyzed detail yet to say something useful about it."


def _telegram_weather_code_label(code: int) -> str:
    mapping = {
        0: "clear",
        1: "mostly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "foggy",
        48: "foggy",
        51: "light drizzle",
        53: "drizzle",
        55: "heavy drizzle",
        61: "light rain",
        63: "rain",
        65: "heavy rain",
        71: "light snow",
        73: "snow",
        75: "heavy snow",
        80: "rain showers",
        81: "rain showers",
        82: "heavy rain showers",
        95: "thunderstorms",
    }
    return mapping.get(int(code), "mixed conditions")


def _telegram_weather_reply_text(*, text: str) -> str:
    normalized = str(text or "").strip()
    lower = normalized.lower()
    if "weather" not in lower:
        return ""
    target_index = 1 if "tomorrow" in lower else 0 if "today" in lower else None
    if target_index is None:
        return ""
    try:
        query = urllib.parse.urlencode(
            {
                "latitude": "48.2082",
                "longitude": "16.3738",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "Europe/Vienna",
                "forecast_days": "2",
            }
        )
        request = urllib.request.Request(f"{_TELEGRAM_WEATHER_URL}?{query}", headers={"User-Agent": "EA-Telegram/1.0"})
        with urllib.request.urlopen(request, timeout=6) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return "I could not fetch the Vienna forecast right now."
    daily = dict(payload.get("daily") or {}) if isinstance(payload, dict) else {}
    dates = list(daily.get("time") or [])
    codes = list(daily.get("weather_code") or [])
    highs = list(daily.get("temperature_2m_max") or [])
    lows = list(daily.get("temperature_2m_min") or [])
    precipitation = list(daily.get("precipitation_probability_max") or [])
    if target_index >= len(dates):
        return "I could not read the forecast for that day."
    label = "Tomorrow" if target_index == 1 else "Today"
    code = int(codes[target_index]) if target_index < len(codes) else 0
    high = highs[target_index] if target_index < len(highs) else None
    low = lows[target_index] if target_index < len(lows) else None
    rain = precipitation[target_index] if target_index < len(precipitation) else None
    parts = [f"{label} in Vienna looks {_telegram_weather_code_label(code)}"]
    if high is not None and low is not None:
        parts.append(f"with about {int(round(low))} to {int(round(high))}°C")
    if rain is not None:
        parts.append(f"and up to {int(round(rain))}% precipitation probability")
    return " ".join(parts) + "."


def _telegram_pocket_audio_query_candidate(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    direct_markers = (
        "pocket",
        "audio",
        "recording",
        "aufnahme",
        "file",
        "datei",
        "hanusch",
        "hospital",
        "krankenhaus",
        "spital",
        "conversation",
        "gespräch",
        "gespraech",
        "transcript",
    )
    if any(marker in normalized for marker in direct_markers):
        return True
    if ("before " in normalized or "after " in normalized) and any(
        marker in normalized
        for marker in ("father", "vater", "mother", "mutter", "brother", "bruder", "family", "familie")
    ):
        return True
    return False


def _telegram_audio_upload_announcement_reply_text(text: str) -> str:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return ""
    upload_verbs = (
        "ich schicke",
        "ich sende",
        "ich lade",
        "i am sending",
        "i'm sending",
        "i will send",
        "i upload",
        "i am uploading",
        "i'm uploading",
    )
    audio_markers = ("audio", "aufnahme", "recording", "voice", "sprachmemo", "sprachnachricht")
    if not any(phrase in normalized for phrase in upload_verbs):
        return ""
    if not any(marker in normalized for marker in audio_markers):
        return ""
    if any(marker in normalized for marker in ("ich ", "schicke", "sende", "aufnahme", "gespräch", "vater")):
        return (
            "Ja, schick die Audioaufnahme hier in Telegram. PropertyQuarry kann sie "
            "entgegennehmen, transkribieren und als private Gesprächsnotiz einordnen."
        )
    return "Yes, send the audio recording here in Telegram. PropertyQuarry can receive it, transcribe it, and file it as a private conversation note."


def _telegram_parse_relative_date_filter(text: str, *, keyword: str) -> str:
    normalized = " ".join(str(text or "").strip().lower().split())
    month_names = "|".join(sorted((re.escape(name) for name in _TELEGRAM_MONTH_ALIASES.keys()), key=len, reverse=True))
    patterns = (
        rf"\b{re.escape(keyword)}\s+(\d{{4}}-\d{{2}}-\d{{2}})\b",
        rf"\b{re.escape(keyword)}\s+({month_names})\s+(\d{{1,2}})(?:,?\s+(\d{{4}}))?\b",
        rf"\b{re.escape(keyword)}\s+(\d{{1,2}})\.(\d{{1,2}})\.(\d{{4}})?\b",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if not match:
            continue
        groups = [str(group or "").strip() for group in match.groups()]
        if len(groups) >= 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", groups[0]):
            return groups[0]
        if len(groups) >= 2 and groups[0].lower() in _TELEGRAM_MONTH_ALIASES:
            month = _TELEGRAM_MONTH_ALIASES[groups[0].lower()]
            day = int(groups[1])
            year = int(groups[2]) if len(groups) >= 3 and groups[2] else datetime.now(ZoneInfo("Europe/Vienna")).year
            try:
                return datetime(year, month, day, tzinfo=ZoneInfo("UTC")).date().isoformat()
            except Exception:
                return ""
        if len(groups) >= 2 and groups[0].isdigit() and groups[1].isdigit():
            day = int(groups[0])
            month = int(groups[1])
            year = int(groups[2]) if len(groups) >= 3 and groups[2] else datetime.now(ZoneInfo("Europe/Vienna")).year
            try:
                return datetime(year, month, day, tzinfo=ZoneInfo("UTC")).date().isoformat()
            except Exception:
                return ""
    return ""


def _telegram_pocket_audio_query_text(text: str) -> str:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized:
        return ""
    stripped = re.sub(
        r"\b(before|after)\s+([A-Za-zÄÖÜäöüß]+|\d{1,2}\.\d{1,2}\.?\d{0,4}|\d{4}-\d{2}-\d{2})(?:\s+\d{1,4})?\b",
        " ",
        normalized,
        flags=re.IGNORECASE,
    )
    fillers = (
        "please",
        "summarize",
        "summary",
        "tell me",
        "why it matches",
        "send me",
        "show me",
        "best",
        "pocket audio",
        "pocket recording",
        "audio file",
        "audio",
        "recording",
        "file",
    )
    lowered = " ".join(stripped.lower().split())
    for filler in fillers:
        lowered = lowered.replace(filler, " ")
    cleaned = re.sub(r"[^a-z0-9äöüß\s\-]", " ", lowered, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


def _telegram_format_pocket_audio_match(item: dict[str, object], *, before: str = "", after: str = "") -> str:
    title = str(item.get("title") or "").strip() or "Pocket recording"
    recorded = str(item.get("recording_at") or "").strip()
    location = str(item.get("location_name") or "").strip() or str(item.get("location_address") or "").strip() or "location unknown"
    summary = str(item.get("summary_markdown") or "").strip() or str(item.get("transcript_excerpt") or "").strip()
    summary = re.sub(r"\s+", " ", summary).strip()
    confidence = float(item.get("location_confidence") or 0.0)
    lines = [f"Best match: {title}."]
    if recorded:
        lines.append(f"Recorded: {recorded}.")
    if before or after:
        window_bits = []
        if before:
            window_bits.append(f"before {before}")
        if after:
            window_bits.append(f"after {after}")
        lines.append(f"Date filter: {' and '.join(window_bits)}.")
    lines.append(f"Place match: {location} (confidence {confidence:.2f}).")
    if summary:
        lines.append(f"Why it matches: {summary[:280]}.")
    return " ".join(lines)


def _telegram_extract_json_object(text: str) -> dict[str, object]:
    normalized = str(text or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    start = normalized.find("{")
    end = normalized.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(normalized[start : end + 1])
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _telegram_recent_pocket_candidate_suggestions(
    container: AppContainer,
    *,
    principal_id: str,
) -> dict[str, object] | None:
    for row in container.channel_runtime.list_recent_observations(limit=60, principal_id=principal_id):
        if str(row.channel or "").strip() != "telegram":
            continue
        if str(row.event_type or "").strip() != "telegram.pocket_candidate_suggestions_sent":
            continue
        return dict(row.payload or {})
    return None


def _telegram_pocket_candidate_selection(text: str) -> int:
    normalized = " ".join(str(text or "").strip().lower().split())
    match = re.fullmatch(r"(?:send|schick|sende|deliver|open|play)\s+(?:candidate\s+|kandidat\s+)?([1-3])", normalized)
    if match is None:
        return 0
    try:
        return int(match.group(1))
    except Exception:
        return 0


def _telegram_record_pocket_candidate_suggestions(
    container: AppContainer,
    *,
    principal_id: str,
    query: str,
    before: str,
    after: str,
    candidates: list[dict[str, object]],
) -> None:
    if not candidates:
        return
    payload = {
        "query": str(query or "").strip(),
        "before": str(before or "").strip(),
        "after": str(after or "").strip(),
        "candidates": [
            {
                "index": index + 1,
                "recording_id": str(item.get("recording_id") or "").strip(),
                "title": str(item.get("title") or "").strip(),
                "recording_at": str(item.get("recording_at") or "").strip(),
                "location_name": str(item.get("location_name") or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
            for index, item in enumerate(candidates[:3])
        ],
    }
    dedupe_material = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    container.channel_runtime.ingest_observation(
        principal_id=principal_id,
        channel="telegram",
        event_type="telegram.pocket_candidate_suggestions_sent",
        payload=payload,
        source_id="telegram-pocket-semantic-fallback",
        dedupe_key=f"{principal_id}|telegram-pocket-candidates|{hashlib.sha256(dedupe_material.encode('utf-8')).hexdigest()}",
    )


def _telegram_pocket_audio_semantic_candidates(
    *,
    container: AppContainer,
    principal_id: str,
    query: str,
    before: str,
    after: str,
) -> list[dict[str, object]]:
    service = build_product_service(container)
    search = service.search_pocket_recordings(
        principal_id=principal_id,
        actor="telegram-semantic-fallback",
        query="",
        before=before,
        after=after,
        limit=18,
    )
    items = list(search.get("items") or [])
    if not items:
        return []
    candidates = [
        {
            "recording_id": str(item.get("recording_id") or "").strip(),
            "title": str(item.get("title") or "").strip(),
            "recording_at": str(item.get("recording_at") or "").strip(),
            "location_name": str(item.get("location_name") or "").strip(),
            "location_address": str(item.get("location_address") or "").strip(),
            "summary_markdown": str(item.get("summary_markdown") or "").strip(),
            "transcript_text": str(item.get("transcript_text") or "").strip(),
            "transcript_excerpt": str(item.get("transcript_excerpt") or "").strip(),
        }
        for item in items[:12]
    ]
    messages = [
        {
            "role": "system",
            "content": (
                "Choose the most likely Pocket audio recordings for the user's memory query. "
                "Use only the provided candidates. "
                "Return strict JSON: {\"candidates\":[{\"recording_id\":\"...\",\"reason\":\"...\"}]}. "
                "Prefer up to 3 candidates. Respect place/date hints in the query."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "query": str(query or "").strip(),
                    "before": str(before or "").strip(),
                    "after": str(after or "").strip(),
                    "candidates": candidates,
                },
                ensure_ascii=False,
            ),
        },
    ]
    try:
        result = _responses_route_module()._generate_upstream_text(
            prompt=str(query or "").strip(),
            messages=messages,
            requested_model=str(os.getenv("EA_TELEGRAM_RESPONSES_MODEL") or "ea-coder-fast").strip() or "ea-coder-fast",
            max_output_tokens=220,
            chatplayground_audit_callback=None,
            chatplayground_audit_callback_only=False,
            chatplayground_audit_principal_id=principal_id,
            preferred_onemin_labels=(),
            request_deadline_monotonic=time.monotonic() + 8.0,
        )
    except Exception:
        return []
    payload = _telegram_extract_json_object(str(getattr(result, "text", "") or ""))
    raw_candidates = list(payload.get("candidates") or []) if isinstance(payload.get("candidates"), list) else []
    candidate_by_id = {str(item.get("recording_id") or "").strip(): item for item in candidates if str(item.get("recording_id") or "").strip()}
    verified: list[dict[str, object]] = []
    for row in raw_candidates:
        if not isinstance(row, dict):
            continue
        recording_id = str(row.get("recording_id") or "").strip()
        if not recording_id or recording_id not in candidate_by_id:
            continue
        verified.append(
            {
                **candidate_by_id[recording_id],
                "reason": str(row.get("reason") or "").strip(),
            }
        )
        if len(verified) >= 3:
            break
    return verified


def _telegram_pocket_audio_reply_text(*, container: AppContainer, principal_id: str, text: str) -> str:
    selection = _telegram_pocket_candidate_selection(text)
    if selection > 0:
        suggestions = _telegram_recent_pocket_candidate_suggestions(container, principal_id=principal_id)
        if not suggestions:
            return "I do not have a recent Pocket candidate list to pick from yet."
        candidates = list(suggestions.get("candidates") or [])
        if selection > len(candidates):
            return f"I only have {len(candidates)} recent Pocket candidates to choose from."
        selected = dict(candidates[selection - 1] or {})
        recording_id = str(selected.get("recording_id") or "").strip()
        if not recording_id:
            return "That Pocket candidate is missing a recording id."
        service = build_product_service(container)
        delivered = service.deliver_pocket_recording_to_telegram(
            principal_id=principal_id,
            actor="telegram",
            recording_id=recording_id,
        )
        return f"Sent: {str(delivered.get('title') or 'Pocket recording').strip()}."
    if not _telegram_pocket_audio_query_candidate(text):
        return ""
    service = build_product_service(container)
    before = _telegram_parse_relative_date_filter(text, keyword="before")
    after = _telegram_parse_relative_date_filter(text, keyword="after")
    query = _telegram_pocket_audio_query_text(text) or str(text or "").strip()
    search = service.search_pocket_recordings(
        principal_id=principal_id,
        actor="telegram",
        query=query,
        before=before,
        after=after,
        limit=3,
    )
    items = list(search.get("items") or [])
    if not items:
        semantic_candidates = _telegram_pocket_audio_semantic_candidates(
            container=container,
            principal_id=principal_id,
            query=query,
            before=before,
            after=after,
        )
        if not semantic_candidates:
            return "I could not find a matching Pocket recording for that place/date query."
        if len(semantic_candidates) == 1:
            return _telegram_format_pocket_audio_match(dict(semantic_candidates[0] or {}), before=before, after=after)
        _telegram_record_pocket_candidate_suggestions(
            container,
            principal_id=principal_id,
            query=query,
            before=before,
            after=after,
            candidates=semantic_candidates,
        )
        lines = ["I found these likely Pocket candidates:"]
        for index, item in enumerate(semantic_candidates[:3], start=1):
            detail = f"{index}. {str(item.get('title') or '').strip()} | {str(item.get('recording_at') or '').strip()}"
            location = str(item.get("location_name") or "").strip()
            if location:
                detail += f" | {location}"
            reason = str(item.get("reason") or "").strip()
            if reason:
                detail += f" | {reason}"
            lines.append(detail)
        lines.append("Reply with `send 1`, `send 2`, or `send 3` to get one on Telegram.")
        return "\n".join(lines)
    return _telegram_format_pocket_audio_match(dict(items[0] or {}), before=before, after=after)


def _telegram_probe_reply_text(text: str) -> str:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized:
        return ""
    if normalized in {"?", "??", "???"}:
        return "Ask directly."
    alpha = "".join(ch for ch in normalized.lower() if ch.isalpha() or ch.isspace()).strip()
    if alpha in {"test", "ping", "hello", "hi", "hey", "are you there", "you there", "check"}:
        return "I'm here. Ask directly."
    return ""


def _telegram_low_signal_followup_cue(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    alpha = "".join(ch for ch in normalized if ch.isalpha() or ch.isspace()).strip()
    alpha_words = [word for word in alpha.split() if word]
    if alpha and all(word in {"done", "finished", "complete", "completed", "ok", "okay"} for word in alpha.split() if word):
        return True
    if alpha_words and len(alpha_words) <= 3 and all(
        word in {"well", "score", "and", "why", "again", "the", "other", "that", "one"}
        for word in alpha_words
    ):
        return True
    return normalized in {
        "again",
        "again?",
        "well",
        "well?",
        "and",
        "and?",
        "why",
        "why?",
        "the other",
        "the other?",
        "that one",
        "that one?",
    }


def _telegram_last_resort_reply_text(text: str) -> str:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized:
        return ""
    probe_reply = _telegram_probe_reply_text(normalized)
    if probe_reply:
        return probe_reply
    alpha = "".join(ch for ch in normalized.lower() if ch.isalpha() or ch.isspace()).strip()
    words = [part for part in alpha.split() if part]
    word_count = len(words)
    if (
        any(marker in words for marker in {"check", "receiver", "working", "alive", "there"})
        or "reply with one short line" in normalized.lower()
    ):
        return "I'm here. Ask directly."
    if word_count <= 2:
        return "Ask directly."
    return "I'm here. Give me a concrete task."


def _telegram_meta_assistant_reply_text(text: str) -> str:
    normalized = " ".join(str(text or "").strip().split())
    if not normalized:
        return ""
    lower = normalized.lower()
    schedule_markers = (
        "next appointment",
        "next meeting",
        "next calendar",
        "my calendar",
        "my schedule",
        "next event",
        "what's next",
        "whats next",
        "what is next",
        "appointment",
    )
    if any(
        phrase in lower
        for phrase in (
            "finally work",
            "are you working",
            "can you work",
            "do you work",
            "work properly",
            "work now",
            "help me",
        )
    ):
        return "I'm here. Give me a concrete task."
    if (
        any(phrase in lower for phrase in ("can u answer everything now", "can you answer everything now"))
        and not any(marker in lower for marker in schedule_markers)
    ):
        return (
            "I can answer from grounded workspace state when PropertyQuarry has the context: schedule, inbox, property scouting, links, and follow-ups."
        )
    if any(
        phrase in lower
        for phrase in (
            "what can you do",
            "what do you do",
            "how can you help",
            "what are you able to do",
        )
    ):
        return "I can help with schedule, inbox, property scouting, links, and grounded workspace follow-ups. Ask directly."
    return ""


def _telegram_recent_messages_include_google_photos_context(
    container: AppContainer,
    *,
    principal_id: str,
    limit: int = 8,
) -> bool:
    messages = _telegram_recent_conversation_messages(
        container,
        principal_id=principal_id,
        current_message_id="",
        limit=limit,
    )
    for item in reversed(messages):
        role = str(item.get("role") or "").strip().lower()
        if role != "user":
            continue
        for part in list(item.get("content") or []):
            if not isinstance(part, dict):
                continue
            text_part = str(part.get("text") or "").strip().lower()
            if not text_part:
                continue
            if "google photos" in text_part or "picture" in text_part or "photo" in text_part:
                return True
    return False


def _telegram_google_photos_accounts(
    container: AppContainer,
    *,
    principal_id: str,
) -> tuple[list[object], list[object], str]:
    try:
        accounts = list(google_oauth_service.list_google_accounts(container=container, principal_id=principal_id))
    except Exception:
        accounts = []
    reconnect_url = ""
    try:
        reconnect_url = str(
            google_oauth_service.build_google_oauth_start(
                principal_id=principal_id,
                scope_bundle="full_workspace_photos",
            ).auth_url
            or ""
        ).strip()
    except Exception:
        reconnect_url = ""
    enabled_accounts = [
        account
        for account in accounts
        if str(account.token_status or "").strip().lower() != "revoked"
        and str(account.binding.status or "").strip().lower() == "enabled"
    ]
    photo_accounts = [
        account
        for account in enabled_accounts
        if google_oauth_service.GOOGLE_SCOPE_PHOTOS_PICKER in set(account.granted_scopes or ())
    ]
    return enabled_accounts, photo_accounts, reconnect_url


def _telegram_google_photos_status_reply_text(
    container: AppContainer,
    *,
    principal_id: str,
    include_next_step: bool = True,
) -> str:
    enabled_accounts, photo_accounts, reconnect_url = _telegram_google_photos_accounts(
        container,
        principal_id=principal_id,
    )
    if not enabled_accounts:
        reply = (
            "Not yet. I do not see a connected Google account for this PropertyQuarry workspace. "
            "And even with Google Photos access, I can only analyze photos you explicitly select through Google Photos Picker."
        )
        if include_next_step and reconnect_url:
            reply += f" Start here: {reconnect_url}"
        return reply
    if not photo_accounts:
        account_labels = ", ".join(
            str(account.google_email or "").strip()
            for account in enabled_accounts[:2]
            if str(account.google_email or "").strip()
        )
        if account_labels:
            reply = (
                f"Not yet. I can see Google connected for {account_labels}, but not with Google Photos Picker access. "
                "I also cannot silently search the whole library; I can only inspect photos you explicitly select."
            )
            if include_next_step and reconnect_url:
                reply += f" Reconnect with Photos Picker here, once per Google account: {reconnect_url}"
            return reply
        reply = (
            "Not yet. Google is connected, but I do not have Google Photos Picker access for this PropertyQuarry workspace. "
            "I can only inspect photos you explicitly select."
        )
        if include_next_step and reconnect_url:
            reply += f" Start here: {reconnect_url}"
        return reply
    account_labels = ", ".join(
        str(account.google_email or "").strip()
        for account in photo_accounts[:2]
        if str(account.google_email or "").strip()
    )
    if not account_labels:
        account_labels = "the connected Google Photos account"
    return (
        f"Partly. I can work with Google Photos for {account_labels}, but only on photos you explicitly select in the picker. "
        "I cannot silently search the whole library yet. If you select likely photos, I can help identify the Noah mattress picture."
    )


def _telegram_google_photos_picker_action_reply_text(
    container: AppContainer,
    *,
    principal_id: str,
) -> str:
    enabled_accounts, photo_accounts, reconnect_url = _telegram_google_photos_accounts(
        container,
        principal_id=principal_id,
    )
    if not enabled_accounts or not photo_accounts:
        return _telegram_google_photos_status_reply_text(
            container,
            principal_id=principal_id,
            include_next_step=True,
        )
    account_email = str(photo_accounts[0].google_email or "").strip()
    product_service = build_product_service(container)
    try:
        session = product_service.create_google_photos_picker_session(
            principal_id=principal_id,
            actor="telegram",
            account_email=account_email,
            max_item_count=50,
            autoclose=True,
        )
    except Exception as exc:
        detail = str(exc or "").strip().lower()
        if detail.startswith("google_photos_service_disabled"):
            activation_url = ""
            raw_detail = str(exc or "").strip()
            if ":" in raw_detail:
                activation_url = raw_detail.split(":", 1)[1].strip()
            reply = (
                f"Google Photos Picker access is connected for {account_email or 'the connected account'}, "
                "but the Google Photos Picker API is disabled in the Google Cloud project for this app."
            )
            if activation_url:
                reply += f" Enable it here: {activation_url}"
            return reply
        if detail == "google_photos_forbidden":
            reply = (
                f"Google Photos Picker access is connected for {account_email or 'the connected account'}, "
                "but Google is still refusing picker sessions for this app with a 403."
            )
        else:
            reply = (
                f"Google Photos Picker access is connected for {account_email or 'the connected account'}, "
                "but I could not start a picker session right now."
            )
        if reconnect_url:
            reply += f" Reconnect here if needed: {reconnect_url}"
        return reply
    picker_uri = str(session.get("picker_uri") or "").strip()
    if not picker_uri:
        return (
            f"Google Photos Picker access is connected for {account_email or 'the connected account'}, "
            "but Google did not return a picker link right now."
        )
    return (
        f"Google Photos Picker is ready for {account_email or 'the connected account'}. "
        f"Open this picker link and select the likely photos: {picker_uri}"
    )


def _telegram_google_photos_reply_text(
    container: AppContainer,
    *,
    principal_id: str,
    text: str,
) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    lower = normalized.lower()
    if "google photos" not in lower and "picture" not in lower and "photo" not in lower:
        return ""
    discovery_markers = (
        "find me",
        "find ",
        "can you find",
        "look for",
        "search",
        "where is",
        "do you have access",
        "you should have access",
    )
    if not any(marker in lower for marker in discovery_markers):
        return ""
    return _telegram_google_photos_status_reply_text(
        container,
        principal_id=principal_id,
        include_next_step=True,
    )


def _answerly_document_qa_configs() -> list[dict[str, str]]:
    shared_base_url = str(os.getenv("EA_ANSWERLY_BASE_URL") or "https://ai.api.answerly.io").strip().rstrip("/")
    configs: list[dict[str, str]] = []

    def _append_config(scope: str, *, api_key_env: str, agent_id_env: str, label_env: str, default_label: str) -> None:
        api_key = str(os.getenv(api_key_env) or "").strip()
        agent_id = str(os.getenv(agent_id_env) or "").strip()
        if not api_key or not agent_id or not shared_base_url:
            return
        configs.append(
            {
                "scope": scope,
                "api_key": api_key,
                "agent_id": agent_id,
                "label": str(os.getenv(label_env) or default_label).strip(),
                "base_url": shared_base_url,
            }
        )

    _append_config(
        "onedrive",
        api_key_env="EA_ANSWERLY_ONEDRIVE_API_KEY",
        agent_id_env="EA_ANSWERLY_ONEDRIVE_AGENT_ID",
        label_env="EA_ANSWERLY_ONEDRIVE_LABEL",
        default_label="OneDrive documents",
    )
    _append_config(
        "shareone",
        api_key_env="EA_ANSWERLY_SHAREONE_API_KEY",
        agent_id_env="EA_ANSWERLY_SHAREONE_AGENT_ID",
        label_env="EA_ANSWERLY_SHAREONE_LABEL",
        default_label="ShareOne documents",
    )
    if not configs:
        api_key = str(os.getenv("EA_ANSWERLY_API_KEY") or "").strip()
        agent_id = str(os.getenv("EA_ANSWERLY_AGENT_ID") or "").strip()
        if api_key and agent_id and shared_base_url:
            configs.append(
                {
                    "scope": "generic",
                    "api_key": api_key,
                    "agent_id": agent_id,
                    "label": str(os.getenv("EA_ANSWERLY_DOCUMENT_QA_LABEL") or "Document knowledge").strip(),
                    "base_url": shared_base_url,
                }
            )
    return configs


def _answerly_document_qa_ready() -> bool:
    return bool(_answerly_document_qa_configs())


def _telegram_answerly_document_query_candidate(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    explicit_markers = (
        "answerly",
        "document qa",
        "document q&a",
        "use the documents",
        "search the documents",
        "search the scans",
        "search scanned documents",
        "look in the documents",
        "look in scanned documents",
        "what does the latest",
        "what does the letter say",
        "what does the report say",
        "what does the pdf say",
        "what does the document say",
        "find the document",
        "find that document",
        "find the letter",
        "find the report",
        "find the scan",
        "find the pdf",
        "send me the birth certificate",
        "schick mir",
        "schicke mir",
        "sende mir",
        "where is my medication",
    )
    if any(marker in normalized for marker in explicit_markers):
        return True
    doc_nouns = (
        "document",
        "documents",
        "scan",
        "scans",
        "pdf",
        "letter",
        "report",
        "approval",
        "brief",
        "arztbrief",
        "befund",
        "rechnung",
        "statement",
        "passport",
        "patientsbrief",
        "certificate",
        "birth certificate",
        "medication",
        "medicine",
    )
    ask_markers = (
        "what does",
        "where is",
        "find",
        "look for",
        "search",
        "do we have",
        "can you find",
        "can you check",
        "send me",
        "schick mir",
        "schicke mir",
        "sende mir",
        "show me",
        "get me",
    )
    return any(noun in normalized for noun in doc_nouns) and any(marker in normalized for marker in ask_markers)


def _answerly_chat(
    *,
    config: dict[str, str],
    message: str,
    conversation_id: str = "",
) -> dict[str, object]:
    payload = json.dumps(
        {
            "APIKey": config["api_key"],
            "agentId": config["agent_id"],
            "conversationId": str(conversation_id or "").strip(),
            "message": str(message or "").strip(),
            "channel": "web",
            "responseStyle": "plaintext",
            "actionRequest": {"name": "conversational"},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{config['base_url']}/chat/",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    timeout_seconds = 20.0
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def _telegram_answerly_scope_for_text(text: str) -> str:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return ""
    if "onewife" in normalized:
        return "onedrive"
    if "shareone" in normalized or "share one" in normalized:
        return "shareone"
    if "onedrive" in normalized or "one drive" in normalized:
        return "onedrive"
    onedrive_markers = (
        "birth certificate",
        "certificate",
        "passport",
        "medication",
        "medicine",
    )
    shareone_markers = (
        "share packet",
        "workspace",
        "team doc",
        "share folder",
    )
    if any(marker in normalized for marker in onedrive_markers):
        return "onedrive"
    if any(marker in normalized for marker in shareone_markers):
        return "shareone"
    return ""


def _telegram_answerly_document_send_request(text: str) -> bool:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return False
    send_markers = ("send me", "schick mir", "schicke mir", "sende mir", "send the", "send that")
    document_markers = ("pdf", "document", "scan", "scanned", "letter", "report", "birth certificate", "certificate")
    return any(marker in normalized for marker in send_markers) and any(marker in normalized for marker in document_markers)


def _telegram_answerly_document_reply_text(
    *,
    container: AppContainer,
    principal_id: str,
    text: str,
) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    if not _telegram_answerly_document_query_candidate(normalized):
        return ""
    configs = _answerly_document_qa_configs()
    if not configs:
        if "answerly" in normalized.lower():
            return "Answerly document Q&A is not configured yet in PropertyQuarry."
        return ""
    requested_scope = _telegram_answerly_scope_for_text(normalized)
    if requested_scope:
        for candidate in configs:
            if str(candidate.get("scope") or "").strip() == requested_scope:
                configs = [candidate]
                break
        else:
            label = "ShareOne" if requested_scope == "shareone" else "OneDrive"
            return f"{label} document Q&A is not configured yet in PropertyQuarry."
    elif len(configs) > 1 and all(str(candidate.get("scope") or "").strip() in {"onedrive", "shareone"} for candidate in configs):
        labels = [str(candidate.get("label") or "").strip() for candidate in configs if str(candidate.get("label") or "").strip()]
        joined = " or ".join(labels[:2]) if labels else "OneDrive or ShareOne"
        return f"Your document backends stay separated. Please say whether to search {joined}."
    config = configs[0]
    try:
        response = _answerly_chat(config=config, message=normalized)
    except Exception as exc:
        return f"{config['label']} document Q&A is configured, but the lookup failed: {str(exc or '').strip() or 'answerly_request_failed'}."
    if not bool(response.get("status")):
        detail = str(response.get("data") or "").strip() or "answerly_request_failed"
        return f"{config['label']} document Q&A could not answer that request: {detail}."
    data = response.get("data")
    if not isinstance(data, dict):
        return "Answerly document Q&A returned an invalid response."
    messages = [
        " ".join(str(item or "").strip().split())
        for item in list(data.get("messages") or [])
        if str(item or "").strip()
    ]
    action_response = data.get("actionResponse")
    action_name = ""
    if isinstance(action_response, dict):
        action_name = str(action_response.get("name") or "").strip().lower()
    elif isinstance(action_response, str):
        action_name = str(action_response or "").strip().lower()
    if action_name in {"hallucination", "unrelated-query"} and not messages:
        return f"I checked {config['label']} in Answerly, but it did not find a grounded document answer for that yet."
    answer = " ".join(messages).strip()
    if not answer:
        if action_name in {"hallucination", "unrelated-query"}:
            return f"I checked {config['label']} in Answerly, but it did not find a grounded document answer for that yet."
        return "Answerly document Q&A did not return a usable answer."
    source_rows = data.get("meta", {}).get("source", []) if isinstance(data.get("meta"), dict) else []
    source_ids = [
        str(row.get("dataItemId") or "").strip()
        for row in list(source_rows or [])
        if isinstance(row, dict) and str(row.get("dataItemId") or "").strip()
    ]
    if source_ids and _telegram_answerly_document_send_request(normalized):
        service = build_product_service(container)
        try:
            delivered = service.deliver_onedrive_document_search_to_telegram(
                principal_id=principal_id,
                actor="telegram_local_assistant",
                query=normalized,
                answerly_source_ids=tuple(source_ids),
                limit=10,
            )
            filename = str(delivered.get("filename") or "document").strip()
            answer += f" Sent {filename} on Telegram."
        except RuntimeError as exc:
            answer += f" I matched the document, but Telegram delivery failed: {str(exc or '').strip() or 'onedrive_document_delivery_failed'}."
    if source_ids:
        answer += f" Matched {config['label']} Answerly items: {', '.join(source_ids[:3])}."
    return answer


def _telegram_ltd_runtime_profiles(container: AppContainer) -> list[object]:
    try:
        catalog = LtdRuntimeCatalogService(provider_registry=container.provider_registry)
        return list(catalog.list_profiles())
    except Exception:
        return []


def _telegram_first_url(text: str) -> str:
    match = re.search(r"https?://\S+", str(text or "").strip())
    if not match:
        return ""
    return str(match.group(0) or "").strip().rstrip(").,;")


def _telegram_try_execute_ltd_action(
    container: AppContainer,
    *,
    principal_id: str,
    service_name: str,
    action: object,
    text: str,
) -> str:
    action_key = str(getattr(action, "action_key", "") or "").strip()
    tool_name = str(getattr(action, "tool_name", "") or "").strip()
    action_kind = str(getattr(action, "action_kind", "") or "").strip()
    route_path = str(getattr(action, "route_path", "") or "").strip()
    if tool_name != "provider.onemin.media_transform":
        return ""
    image_url = _telegram_first_url(text)
    if not image_url:
        return ""
    feature_type = ""
    if action_key == "background_remove":
        feature_type = "BACKGROUND_REMOVER"
    elif action_key == "image_upscale":
        feature_type = "IMAGE_UPSCALER"
    else:
        return ""
    request = ToolInvocationRequest(
        session_id=f"telegram-ltd:{uuid.uuid4()}",
        step_id=f"telegram-ltd-step:{uuid.uuid4()}",
        tool_name=tool_name,
        action_kind=action_kind,
        payload_json={
            "action_key": action_key,
            "feature_type": feature_type,
            "image_url": image_url,
        },
        context_json={"principal_id": principal_id},
    )
    try:
        result = container.tool_execution.execute_invocation(request)
    except Exception as exc:
        return f"I would use {service_name} {action_key}, but execution failed: {str(exc or '').strip() or 'tool_execution_failed'}."
    receipt = dict(getattr(result, "receipt_json", {}) or {})
    output = dict(getattr(result, "output_json", {}) or {})
    target_ref = str(getattr(result, "target_ref", "") or "").strip()
    asset_urls = [
        str(value or "").strip()
        for value in list(output.get("asset_urls") or [])
        if str(value or "").strip()
    ]
    receipt_verified = bool(
        receipt.get("principal_id") == principal_id
        and receipt.get("handler_key") == tool_name
        and receipt.get("invocation_contract") == "tool.v1"
        and receipt.get("provider_key") == "onemin"
        and receipt.get("provider_backend") == "1min"
        and receipt.get("feature_type") == feature_type
        and str(receipt.get("model") or "").strip()
        and target_ref.startswith("onemin:")
        and output.get("provider_backend") == "1min"
        and asset_urls
    )
    if not receipt_verified:
        return (
            f"{service_name} returned without a principal-bound provider "
            "receipt and output asset, so execution is not proven."
        )
    answer = f"Executed {service_name} {action_key} with a principal-bound provider receipt."
    if target_ref:
        answer += f" Target: {target_ref}."
    if route_path:
        answer += f" Route: {route_path}."
    return answer


def _telegram_ltd_reply_text(
    container: AppContainer,
    *,
    principal_id: str,
    text: str,
) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    lowered = " ".join(normalized.lower().split())
    profiles = _telegram_ltd_runtime_profiles(container)
    if not profiles:
        return ""
    runtime_catalog = LtdRuntimeCatalogService(provider_registry=container.provider_registry)
    wants_catalog = any(
        phrase in lowered
        for phrase in (
            "what ltd",
            "which ltd",
            "which tool",
            "which service",
            "what service should",
            "what can ea use",
            "what can you use",
            "available ltd",
            "runtime catalog",
        )
    )
    matched_profile = None
    for profile in profiles:
        service_name = str(getattr(profile, "service_name", "") or "").strip()
        aliases = [str(item or "").strip() for item in list(getattr(profile, "aliases", ()) or ()) if str(item or "").strip()]
        tokens = [service_name.lower(), *(alias.lower() for alias in aliases)]
        if any(token and token in lowered for token in tokens):
            matched_profile = profile
            break
    if matched_profile is not None:
        service_name = str(getattr(matched_profile, "service_name", "") or "").strip()
        runtime_state = str(getattr(matched_profile, "runtime_state", "") or "").strip()
        tier = str(getattr(matched_profile, "workspace_integration_tier", "") or "").strip()
        actions = list(getattr(matched_profile, "actions", ()) or ())
        explicit_use_request = any(
            phrase in lowered
            for phrase in (
                "use ",
                "run ",
                "open ",
                "inspect ",
                "read ",
                "create ",
                "send ",
                "generate ",
                "remove the background",
                "upscale ",
            )
        )
        if explicit_use_request:
            inferred_task_key = projected_task_key_for_request(
                goal=normalized,
                input_json={"service_name": service_name},
                catalog=runtime_catalog,
            )
            if inferred_task_key:
                for action in actions:
                    action_task_key = projected_task_key(service_name, str(getattr(action, "action_key", "") or "").strip())
                    if inferred_task_key != action_task_key:
                        continue
                    action_key = str(getattr(action, "action_key", "") or "").strip()
                    if action_key == "discover_account" and any(
                        str(getattr(candidate, "action_key", "") or "").strip() not in {"", "discover_account"}
                        for candidate in actions
                    ):
                        break
                    executed_reply = _telegram_try_execute_ltd_action(
                        container,
                        principal_id=principal_id,
                        service_name=service_name,
                        action=action,
                        text=normalized,
                    )
                    if executed_reply:
                        return executed_reply
                    route_path = str(getattr(action, "route_path", "") or "").strip()
                    description = str(getattr(action, "description", "") or "").strip()
                    answer = f"For {service_name}, the catalog route is {action_key}."
                    if description:
                        answer += f" {description}"
                    if route_path:
                        answer += f" Route: {route_path}."
                    answer += (
                        " Live execution is unverified until this principal "
                        "receives a provider-call receipt."
                    )
                    return answer
        action_labels = [
            str(getattr(action, "action_key", "") or "").strip()
            for action in actions
            if str(getattr(action, "action_key", "") or "").strip()
        ]
        action_text = ", ".join(action_labels[:4]) if action_labels else "no runtime actions"
        return (
            f"{service_name} is cataloged in PropertyQuarry as "
            f"{runtime_state} ({tier}). Actions: {action_text}. Catalog state "
            "does not prove credentials, provider health, or a live call."
        )
    if not wants_catalog:
        return ""
    actionable = []
    for profile in profiles:
        actions = [action for action in list(getattr(profile, "actions", ()) or ()) if str(getattr(action, "action_key", "") or "").strip()]
        if not actions:
            continue
        actionable.append(
            (
                str(getattr(profile, "workspace_integration_tier", "") or "").strip().lower(),
                str(getattr(profile, "service_name", "") or "").strip(),
                profile,
            )
        )
    actionable.sort(key=lambda item: item[1].lower())
    if not actionable:
        return ""
    top = [row[2] for row in actionable[:5]]
    summary = []
    for profile in top:
        service_name = str(getattr(profile, "service_name", "") or "").strip()
        runtime_state = str(getattr(profile, "runtime_state", "") or "").strip()
        actions = list(getattr(profile, "actions", ()) or ())
        first_action = str(getattr(actions[0], "action_key", "") or "").strip() if actions else ""
        if service_name:
            chunk = service_name
            if runtime_state:
                chunk += f" ({runtime_state})"
            if first_action:
                chunk += f" -> {first_action}"
            summary.append(chunk)
    if not summary:
        return ""
    return (
        "PropertyQuarry catalogs these LTD/runtime lanes: "
        + " | ".join(summary[:5])
        + ". This inventory does not prove principal credentials, provider "
        "health, or a live call."
    )


def _telegram_google_photos_context_reply(
    container: AppContainer,
    *,
    principal_id: str,
    normalized: str,
    lower: str,
    alpha_words: list[str],
) -> str:
    if any(
        phrase in lower
        for phrase in (
            "start google photos picker",
            "start the google photos picker",
            "open google photos picker",
            "open the google photos picker",
            "start photo picker",
            "start the photo picker",
            "open photo picker",
            "open the photo picker",
            "start picker",
            "open picker",
        )
    ):
        return _telegram_google_photos_picker_action_reply_text(
            container,
            principal_id=principal_id,
        )
    if alpha_words and all(word in {"done", "finished", "complete", "completed", "ok", "okay"} for word in alpha_words):
        if _telegram_recent_messages_include_google_photos_context(
            container,
            principal_id=principal_id,
        ):
            return _telegram_google_photos_picker_action_reply_text(
                container,
                principal_id=principal_id,
            )
    if alpha_words and all(word in {"voice", "message"} for word in alpha_words):
        if _telegram_recent_messages_include_google_photos_context(
            container,
            principal_id=principal_id,
        ):
            return _telegram_google_photos_picker_action_reply_text(
                container,
                principal_id=principal_id,
            )
    return _telegram_google_photos_reply_text(
        container,
        principal_id=principal_id,
        text=normalized,
    )


def _telegram_property_alert_policy_reply(
    container: AppContainer,
    *,
    principal_id: str,
    lower: str,
) -> str:
    if not (
        ("do all of that by itself" in lower or "do that by itself" in lower or "handle property alerts by itself" in lower)
        or ("if it's good" in lower and "notification here" in lower)
    ):
        return ""
    product_service = build_product_service(container)
    policy = product_service.update_property_alert_policy(
        principal_id=principal_id,
        actor="telegram",
        auto_score=True,
        auto_compare=True,
        auto_generate_tour_for_good_fit=False,
        notify_only_if_good=True,
        good_fit_min_score=80.0,
        good_fit_recommendations=("shortlist",),
        source_id="telegram:property-alert-policy",
    )
    threshold = int(float(policy.get("good_fit_min_score") or 80.0))
    return (
        "Understood. PropertyQuarry will now score and rank property alerts automatically, "
        f"and only notify you here when the fit looks genuinely good, around {threshold}/100 or shortlist-level."
    )


def _telegram_is_low_signal_summary(value: object) -> bool:
    summary = str(value or "").strip().lower()
    if not summary:
        return True
    low_signal_markers = (
        "signal from ",
        "signal sync completed",
        "workspace signal sync completed",
        "google workspace signal sync completed",
        "sync completed",
        "google sync completed",
        "office signal ingested",
    )
    return any(marker in summary for marker in low_signal_markers)


def _telegram_is_actionable_focus_summary(value: object) -> bool:
    summary = str(value or "").strip().lower()
    if not summary or _telegram_is_low_signal_summary(summary):
        return False
    actionable_markers = (
        "approve",
        "review",
        "reply",
        "follow up",
        "follow-up",
        "send",
        "book",
        "call",
        "prepare",
        "shortlist",
        "property",
        "apartment",
        "tour",
    )
    return any(marker in summary for marker in actionable_markers)


def _telegram_compact_focus_text(value: object, *, limit: int = 120) -> str:
    text_value = " ".join(str(value or "").strip().split())
    if not text_value:
        return ""
    if text_value.lower().startswith("review apartment alert:"):
        suffix = text_value.split(":", 1)[1].strip()
        suffix = suffix.strip("\"")
        text_value = f"Apartment alert: {suffix}" if suffix else "Apartment alert"
    if text_value.lower().startswith("apartment alert:"):
        prefix, _, suffix = text_value.partition(":")
        cleaned_suffix = suffix.strip().replace('"', "").replace("“", "").replace("”", "")
        listing_match = re.match(r"^(?P<label>.+?) hat (?P<count>\d+) neue Anzeige(?:n)? für dich gefunden\.?$", cleaned_suffix, re.IGNORECASE)
        if listing_match:
            label = " ".join(str(listing_match.group("label") or "").split()).strip(" ,;:")
            count = int(str(listing_match.group("count") or "0") or "0")
            noun = "listing" if count == 1 else "listings"
            cleaned_suffix = f"{label} ({count} new {noun})"
        text_value = f"{prefix}: {cleaned_suffix}".strip()
    if text_value.lower().startswith("reply to ") and " | " in text_value:
        _, _, remainder = text_value.partition(" | ")
        if remainder.strip():
            text_value = remainder.strip()
    if text_value.lower().startswith("re:"):
        text_value = text_value[3:].strip()
    lowered = text_value.lower()
    noisy_markers = (
        "stage 1 commitment candidate.",
        "prepare a reply draft for approval before send.",
        "no additional ltd lane is recommended",
        "office signal ingested.",
        "recent mail from",
        " hi ",
    )
    for marker in noisy_markers:
        idx = lowered.find(marker)
        if idx >= 0:
            text_value = text_value[:idx].strip()
            lowered = text_value.lower()
    greeting_idx = lowered.find(". hi ")
    if greeting_idx >= 0:
        text_value = text_value[: greeting_idx + 1].strip()
    if ". " in text_value:
        first_sentence = text_value.split(". ", 1)[0].strip()
        if 0 < len(first_sentence) <= limit:
            text_value = first_sentence.rstrip(".") + "."
    while ".." in text_value:
        text_value = text_value.replace("..", ".")
    if len(text_value) <= limit:
        return text_value
    clipped = text_value[: limit - 1].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" ,;:.") + "..."


def _telegram_tomorrow_focus_reply(
    container: AppContainer,
    *,
    principal_id: str,
    lower: str,
) -> str:
    if not any(
        phrase in lower
        for phrase in (
            "focus on tomorrow",
            "what should i focus on tomorrow",
            "what should i focus on",
            "what should i do tomorrow",
            "what is tomorrow like",
        )
    ):
        return ""
    now_vienna = datetime.now(ZoneInfo("Europe/Vienna"))
    tomorrow_date = now_vienna.date() + timedelta(days=1)
    upcoming = _telegram_upcoming_calendar_events(container, principal_id=principal_id, limit=6)
    tomorrow_events = [
        event
        for event in upcoming
        if event["start_at"].astimezone(ZoneInfo("Europe/Vienna")).date() == tomorrow_date
    ]
    parts: list[str] = []
    if tomorrow_events:
        first = tomorrow_events[0]
        start_text = first["start_at"].astimezone(ZoneInfo("Europe/Vienna")).strftime("%H:%M")
        parts.append(f"Tomorrow, focus first on {first['title']} at {start_text}.")
        if str(first.get("location") or "").strip():
            parts.append(f"Location: {str(first.get('location') or '').strip()}.")
    product_service = build_product_service(container)
    events = list(product_service.list_office_events(principal_id=principal_id, limit=20))
    recent_summaries = [
        str(row.get("summary") or "").strip()
        for row in events
        if str(row.get("channel") or "").strip() in {"gmail", "product", "pocket"}
        and str(row.get("summary") or "").strip()
        and _telegram_is_actionable_focus_summary(row.get("summary"))
    ][:2]
    try:
        queue_items = list(product_service.list_queue(principal_id=principal_id, limit=3))
    except Exception:
        queue_items = []
    if recent_summaries:
        compact_summaries = [_telegram_compact_focus_text(item, limit=90) for item in recent_summaries]
        compact_summaries = [item for item in compact_summaries if item]
        if compact_summaries:
            joined = " | ".join(compact_summaries[:2]).rstrip(". ")
            parts.append("Recent follow-up context: " + joined + ".")
    if queue_items:
        first = queue_items[0]
        first_title = _telegram_compact_focus_text(getattr(first, "title", ""), limit=90)
        first_summary = _telegram_compact_focus_text(getattr(first, "summary", ""), limit=80)
        if first_title:
            sentence = f"Top priority looks like {first_title}."
            if first_summary and first_summary.lower() not in {"operator · pending", "unassigned · normal · pending"}:
                sentence += f" {first_summary}"
            parts.append(sentence)
        if len(queue_items) > 1:
            next_titles = [
                _telegram_compact_focus_text(getattr(item, "title", ""), limit=80)
                for item in queue_items[1:]
                if _telegram_compact_focus_text(getattr(item, "title", ""), limit=80)
            ]
            if next_titles:
                parts.append("After that: " + " | ".join(next_titles[:2]) + ".")
    profile_lines = _telegram_profile_admin_lines(container, principal_id=principal_id, limit=2)
    if profile_lines:
        parts.append("Profile-based focus: " + " | ".join(line.rstrip(". ") for line in profile_lines if line.strip()) + ".")
    if parts:
        return " ".join(parts)
    if profile_lines:
        return "I do not see a concrete appointment for tomorrow yet. " + " ".join(line.rstrip(". ") + "." for line in profile_lines[:2] if line.strip())
    return "I do not see a concrete appointment for tomorrow yet. Focus on clearing the most important inbox and property follow-ups first."


def _telegram_summary_reply(
    container: AppContainer,
    *,
    principal_id: str,
    lower: str,
) -> str:
    if not any(phrase in lower for phrase in ("summarize", "summary", "recap", "catch me up")):
        return ""
    upcoming = _telegram_upcoming_calendar_events(container, principal_id=principal_id, limit=2)
    product_service = build_product_service(container)
    events = list(product_service.list_office_events(principal_id=principal_id, limit=12))
    recent_signals = [
        row
        for row in events
        if str(row.get("channel") or "").strip() in {"gmail", "calendar", "pocket", "product"}
        and not _telegram_is_low_signal_summary(row.get("summary"))
    ][:4]
    parts: list[str] = []
    if upcoming:
        first = upcoming[0]
        starts = first["start_at"].astimezone(ZoneInfo("Europe/Vienna")).strftime("%A at %H:%M")
        parts.append(f"Next up: {first['title']} on {starts}.")
    if recent_signals:
        summaries = [str(row.get("summary") or "").strip() for row in recent_signals if str(row.get("summary") or "").strip()]
        if summaries:
            parts.append("Recent activity: " + " | ".join(summaries[:3]))
    if parts:
        return " ".join(parts)
    return "I do not have enough recent PropertyQuarry state to summarize anything useful right now."


def _telegram_email_summary_reply(
    container: AppContainer,
    *,
    principal_id: str,
    lower: str,
) -> str:
    if not any(phrase in lower for phrase in ("email", "emails", "inbox", "mail")):
        return ""
    product_service = build_product_service(container)
    events = list(product_service.list_office_events(principal_id=principal_id, limit=20))
    gmail_events = [row for row in events if str(row.get("channel") or "").strip() == "gmail"][:3]
    if gmail_events:
        summaries = [str(row.get("summary") or "").strip() for row in gmail_events if str(row.get("summary") or "").strip()]
        if summaries:
            return "Recent email signals: " + " | ".join(summaries)
    return "I do not see a recent Gmail signal I can summarize right now."


def _telegram_local_assistant_reply_text(
    container: AppContainer,
    *,
    principal_id: str,
    text: str,
) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    lower = normalized.lower()
    alpha = "".join(ch for ch in lower if ch.isalpha() or ch.isspace()).strip()
    alpha_words = [part for part in alpha.split() if part]
    return run_local_resolvers(
        _telegram_local_resolvers(
            container=container,
            principal_id=principal_id,
            normalized=normalized,
            lower=lower,
            alpha_words=alpha_words,
        )
    )


def _telegram_local_resolvers(
    *,
    container: AppContainer,
    principal_id: str,
    normalized: str,
    lower: str,
    alpha_words: list[str],
) -> list[TelegramLocalResolver]:
    return [
        TelegramLocalResolver(name="meta", resolve=lambda: _telegram_meta_assistant_reply_text(normalized)),
        TelegramLocalResolver(
            name="google_photos",
            resolve=lambda: _telegram_google_photos_context_reply(
                container,
                principal_id=principal_id,
                normalized=normalized,
                lower=lower,
                alpha_words=alpha_words,
            ),
        ),
        TelegramLocalResolver(
            name="answerly_documents",
            resolve=lambda: _telegram_answerly_document_reply_text(
                container=container,
                principal_id=principal_id,
                text=normalized,
            ),
        ),
        TelegramLocalResolver(
            name="ltd_runtime",
            resolve=lambda: _telegram_ltd_reply_text(
                container,
                principal_id=principal_id,
                text=normalized,
            ),
        ),
        TelegramLocalResolver(
            name="admin_followup",
            resolve=lambda: _telegram_profile_followup_reply_text(
                container,
                principal_id=principal_id,
                text=normalized,
            ),
        ),
        TelegramLocalResolver(
            name="property_alert_policy",
            resolve=lambda: _telegram_property_alert_policy_reply(
                container,
                principal_id=principal_id,
                lower=lower,
            ),
        ),
        TelegramLocalResolver(
            name="tomorrow_focus",
            resolve=lambda: _telegram_tomorrow_focus_reply(
                container,
                principal_id=principal_id,
                lower=lower,
            ),
        ),
        TelegramLocalResolver(
            name="summary",
            resolve=lambda: _telegram_summary_reply(
                container,
                principal_id=principal_id,
                lower=lower,
            ),
        ),
        TelegramLocalResolver(
            name="email_summary",
            resolve=lambda: _telegram_email_summary_reply(
                container,
                principal_id=principal_id,
                lower=lower,
            ),
        ),
    ]


def _parse_isoish_datetime(value: object) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        parsed = datetime.fromisoformat(normalized)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def _telegram_upcoming_calendar_events(
    container: AppContainer,
    *,
    principal_id: str,
    limit: int = 4,
) -> list[dict[str, object]]:
    product_service = build_product_service(container)
    rows = list(
        product_service.list_office_events(
            principal_id=principal_id,
            limit=max(limit * 8, 20),
            event_type="office_signal_calendar_note",
        )
    )
    if not rows:
        fallback_rows: list[dict[str, object]] = []
        for observation in container.channel_runtime.list_recent_observations(limit=max(limit * 8, 20), principal_id=principal_id):
            if str(observation.channel or "").strip() != "calendar":
                continue
            if str(observation.event_type or "").strip() != "office_signal_calendar_note":
                continue
            fallback_rows.append(
                {
                    "summary": str(getattr(observation, "event_type", "") or "").strip(),
                    "created_at": str(getattr(observation, "created_at", "") or "").strip(),
                    "payload": dict(getattr(observation, "payload", {}) or {}),
                }
            )
        rows = fallback_rows
    upcoming: list[dict[str, object]] = []
    now = datetime.now(ZoneInfo("UTC"))
    for row in rows:
        payload = dict(row.get("payload") or {})
        start_at = (
            str(payload.get("start_at") or "").strip()
            or str(payload.get("due_at") or "").strip()
            or str(row.get("created_at") or "").strip()
        )
        parsed_start = _parse_isoish_datetime(start_at)
        if parsed_start is None:
            continue
        if parsed_start.astimezone(ZoneInfo("UTC")) < now:
            continue
        title = str(payload.get("title") or row.get("summary") or "").strip() or "Upcoming meeting"
        attendees = [str(item or "").strip() for item in list(payload.get("attendees") or []) if str(item or "").strip()]
        location = str(payload.get("location") or "").strip()
        upcoming.append(
            {
                "title": title,
                "start_at": parsed_start,
                "location": location,
                "attendees": attendees[:4],
                "summary": str(row.get("summary") or "").strip(),
            }
        )
    upcoming.sort(key=lambda item: item["start_at"])
    return upcoming[:limit]


def _telegram_direct_calendar_reply_text(*, container: AppContainer, principal_id: str, text: str) -> str:
    normalized = str(text or "").strip()
    lower = normalized.lower()
    if not normalized:
        return ""
    schedule_markers = (
        "next appointment",
        "next meeting",
        "next calendar",
        "my calendar",
        "my schedule",
        "next event",
        "what's next",
        "whats next",
        "what is next",
        "appointment",
    )
    if not any(marker in lower for marker in schedule_markers):
        return ""
    events = _telegram_upcoming_calendar_events(container, principal_id=principal_id, limit=3)
    if not events:
        return "I do not see an upcoming calendar appointment in PropertyQuarry right now."
    first = events[0]
    starts = first["start_at"].astimezone(ZoneInfo("Europe/Vienna")).strftime("%A at %H:%M")
    prefix = "Yes. " if any(marker in lower for marker in ("can u", "can you")) else ""
    detail_parts = [f"{prefix}Your next appointment is {first['title']} on {starts}."]
    location = str(first.get("location") or "").strip()
    if location:
        detail_parts.append(f"Location: {location}.")
    attendees = [str(item or "").strip() for item in list(first.get("attendees") or []) if str(item or "").strip()]
    if attendees:
        detail_parts.append(f"With {', '.join(attendees[:3])}.")
    return " ".join(detail_parts)


def _telegram_admin_focus_lines_from_profile_refs(refs: list[str], *, limit: int = 4) -> list[str]:
    ref_map = {
        "profile_followup:insurance_admin:rehab_authorization_management": "Insurance admin is a real theme: watch rehab approvals, KfA authorizations, and follow-ups.",
        "profile_followup:insurance_admin:insurance_and_lab_followthrough": "Insurance and lab follow-through matter: keep questionnaires, lab results, and benefit paperwork current.",
        "profile_followup:medical_admin:proactive_case_management": "Medical admin remains active: keep rehab, neurology, and care paperwork moving.",
        "profile_followup:medical_admin:official_followup_management": "Official medical follow-ups matter: stay ahead of Amtsarzt controls and medical forms.",
        "profile_followup:school_admin:school_and_kindergarten_coordination": "School and kindergarten coordination is active: keep Noah enrollment, attendance, and planning paperwork in order.",
        "profile_followup:care_admin:care_leave_management": "Care-leave admin is active: track Pflegefreistellung and child-related schedule disruptions.",
        "profile_followup:utilities_admin:utility_and_provider_account_management": "Utility admin is active: keep Wiener Netze, Wiener Wohnen, and provider-account tasks under control.",
        "profile_followup:housing_admin:rental_and_utilities_admin": "Housing admin matters: watch rent, utilities, mandates, and landlord or provider paperwork.",
        "profile_followup:financial_admin:banking_card_admin": "Banking and card admin matters: keep Easybank, bank99, and Visa tasks tidy.",
        "profile_followup:travel_admin:family_passport_document_management": "Travel-document admin is active: keep passports and family identity documents current.",
    }
    lines: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        line = ref_map.get(str(ref or "").strip())
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _telegram_profile_admin_lines(
    container: AppContainer,
    *,
    principal_id: str,
    limit: int = 4,
) -> list[str]:
    product_service = build_product_service(container)
    try:
        bundle = product_service.get_preference_profile(principal_id=principal_id, person_id="self")
    except Exception:
        return []
    nodes = list(bundle.get("preference_nodes") or [])
    prioritized: list[tuple[str, float]] = []
    for row in nodes:
        if str(row.get("status") or "").strip().lower() != "active":
            continue
        domain = str(row.get("domain") or "").strip().lower()
        category = str(row.get("category") or "").strip().lower()
        key = str(row.get("key") or "").strip().lower()
        confidence = float(row.get("confidence") or 0.0)
        if domain == "willhaben":
            continue
        line = ""
        if category == "medical_admin" and key == "proactive_case_management":
            line = "Medical admin remains active: keep rehab, neurology, and care paperwork moving."
        elif category == "medical_admin" and key == "official_followup_management":
            line = "Official medical follow-ups matter: stay ahead of Amtsarzt controls and medical forms."
        elif category == "insurance_admin" and key == "rehab_authorization_management":
            line = "Insurance admin is a real theme: watch rehab approvals, KfA authorizations, and follow-ups."
        elif category == "insurance_admin" and key == "insurance_and_lab_followthrough":
            line = "Insurance and lab follow-through matter: keep questionnaires, lab results, and benefit paperwork current."
        elif category == "school_admin" and key == "school_and_kindergarten_coordination":
            line = "School and kindergarten coordination is active: keep Noah enrollment, attendance, and planning paperwork in order."
        elif category == "care_admin" and key == "care_leave_management":
            line = "Care-leave admin is active: track Pflegefreistellung and child-related schedule disruptions."
        elif category == "utilities_admin" and key == "utility_and_provider_account_management":
            line = "Utility admin is active: keep Wiener Netze, Wiener Wohnen, and provider-account tasks under control."
        elif category == "housing_admin" and key == "rental_and_utilities_admin":
            line = "Housing admin matters: watch rent, utilities, mandates, and landlord or provider paperwork."
        elif category == "financial_admin" and key == "banking_card_admin":
            line = "Banking and card admin matters: keep Easybank, bank99, and Visa tasks tidy."
        elif category == "workflow" and key == "prefers_proactive_deadline_tracking":
            line = "You tend to need proactive deadline tracking: clear dated admin tasks early."
        elif category == "household" and key in {"shared_family_admin_involvement", "child_related_admin"}:
            line = "Family admin is active: child, travel, and shared household paperwork deserves attention."
        elif category == "travel_admin" and key == "family_passport_document_management":
            line = "Travel-document admin is active: keep passports and family identity documents current."
        if line:
            prioritized.append((line, confidence))
    seen: set[str] = set()
    result: list[str] = []
    for line, _confidence in sorted(prioritized, key=lambda item: item[1], reverse=True):
        if line in seen:
            continue
        seen.add(line)
        result.append(line)
        if len(result) >= limit:
            break
    return result


def _telegram_admin_followup_candidates(
    brief_items: list[object],
    queue_items: list[object],
    *,
    theme_refs: list[str] | None = None,
) -> list[object]:
    theme_refs = [str(item or "").strip() for item in list(theme_refs or []) if str(item or "").strip()]
    direct_admin_markers = (
        "rehab",
        "kfa",
        "bewilligung",
        "amtsarzt",
        "wiederbestellung",
        "school",
        "schule",
        "kindergarten",
        "pflegefreistellung",
        "insurance",
        "utility",
        "paperwork",
        "follow-up",
        "follow up",
        "admin",
    )

    def _matches_theme(item: object) -> bool:
        refs = [
            str(ref or "").strip()
            for ref in list(getattr(item, "profile_followup_refs", ()) or ())
            if str(ref or "").strip()
        ]
        object_ref = str(getattr(item, "object_ref", "") or "").strip()
        if object_ref:
            refs.append(object_ref)
        if theme_refs and any(ref in theme_refs for ref in refs):
            return True
        haystack = " ".join(
            part
            for part in (
                str(getattr(item, "title", "") or "").strip().lower(),
                str(getattr(item, "summary", "") or "").strip().lower(),
                str(getattr(item, "why_now", "") or "").strip().lower(),
                str(getattr(item, "recommended_action", "") or "").strip().lower(),
                object_ref.lower(),
            )
            if part
        )
        return any(marker in haystack for marker in direct_admin_markers)

    candidates: list[object] = []
    seen_keys: set[str] = set()
    ordered_queue = list(queue_items)
    ordered_queue.sort(
        key=lambda row: (
            str(getattr(row, "priority", "") or "").strip().lower() != "high",
            -float(getattr(row, "rank_score", 0.0) or 0.0),
            str(getattr(row, "title", "") or "").strip().lower(),
        )
    )
    ordered_briefs = list(brief_items)
    ordered_briefs.sort(key=lambda row: -float(getattr(row, "score", 0.0) or 0.0))
    for item in ordered_queue + ordered_briefs:
        if not _matches_theme(item):
            continue
        key = str(getattr(item, "id", "") or getattr(item, "object_ref", "") or getattr(item, "title", "") or "").strip()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(item)
    return candidates


def _telegram_profile_followup_reply_text(
    container: AppContainer,
    *,
    principal_id: str,
    text: str,
) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    lowered = " ".join(normalized.lower().split())
    persisted_intent_state = _telegram_recent_persisted_intent_state(container, principal_id=principal_id)
    persisted_object_map = _telegram_recent_persisted_object_map(container, principal_id=principal_id)
    theme_refs: list[str] = []
    raw_themes = str(persisted_intent_state.get("active_profile_themes") or "").strip()
    for item in raw_themes.split(","):
        normalized_ref = str(item or "").strip()
        if normalized_ref and normalized_ref not in theme_refs:
            theme_refs.append(normalized_ref)
    for key in ("active_queue_profile_refs", "active_property_profile_refs"):
        raw = str(persisted_object_map.get(key) or "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            normalized_ref = str(item or "").strip()
            if normalized_ref and normalized_ref not in theme_refs:
                theme_refs.append(normalized_ref)
    intent = str(persisted_intent_state.get("active_intent") or "").strip().lower()
    direct_admin_markers = (
        "rehab",
        "kfa",
        "bewilligung",
        "amtsarzt",
        "wiederbestellung",
        "school",
        "schule",
        "kindergarten",
        "pflegefreistellung",
        "insurance",
        "utility",
        "paperwork",
        "follow-up",
        "follow up",
        "admin",
    )
    wants_secondary = any(
        marker in lowered
        for marker in (
            "after that",
            "and after that",
            "afterwards",
            "what next",
            "what after that",
            "next one",
            "the other one",
            "the other",
        )
    )
    wants_reason = any(
        marker in lowered
        for marker in (
            "why that one",
            "why this one",
            "why that",
            "why this",
            "why first",
            "why that one?",
            "why the other one",
            "why the other",
        )
    )
    is_followup_prompt = (
        intent == "admin_followup"
        and theme_refs
        and (
            _telegram_low_signal_followup_cue(normalized)
            or wants_secondary
            or wants_reason
            or any(marker in lowered for marker in ("paperwork", "follow-up", "follow up", "admin", "rehab", "kfa"))
        )
    )
    if not is_followup_prompt and not any(marker in lowered for marker in direct_admin_markers):
        return ""
    product_service = build_product_service(container)
    try:
        brief_items = list(product_service.list_brief_items(principal_id=principal_id, limit=8))
    except Exception:
        brief_items = []
    try:
        queue_items = list(product_service.list_queue(principal_id=principal_id, limit=8))
    except Exception:
        queue_items = []

    matching_candidates = _telegram_admin_followup_candidates(
        brief_items,
        queue_items,
        theme_refs=theme_refs,
    )
    if matching_candidates:
        persisted_primary = str(persisted_intent_state.get("active_admin_primary") or "").strip()
        persisted_secondary = str(persisted_intent_state.get("active_admin_secondary") or "").strip()
        index = 1 if wants_secondary and len(matching_candidates) > 1 else 0
        if wants_reason and persisted_primary:
            for candidate_index, candidate in enumerate(matching_candidates[:2]):
                candidate_ref = str(getattr(candidate, "object_ref", "") or getattr(candidate, "id", "") or "").strip()
                if candidate_ref == persisted_primary:
                    index = candidate_index
                    break
        if wants_reason and wants_secondary and persisted_secondary:
            for candidate_index, candidate in enumerate(matching_candidates[:2]):
                candidate_ref = str(getattr(candidate, "object_ref", "") or getattr(candidate, "id", "") or "").strip()
                if candidate_ref == persisted_secondary:
                    index = candidate_index
                    break
        top = matching_candidates[index]
        title = str(getattr(top, "title", "") or "").strip()
        summary = " ".join(
            (
                str(getattr(top, "summary", "") or "").strip()
                or str(getattr(top, "why_now", "") or "").strip()
            ).split()
        )
        recommended_action = " ".join(str(getattr(top, "recommended_action", "") or "").strip().split())
        if wants_reason:
            prefix = "That one leads because" if index == 0 else "The other one matters because"
            answer = f"{prefix} {summary or title}."
            if recommended_action:
                answer += f" Next: {recommended_action}."
            return answer.strip()
        prefix = "After that, focus on" if index == 1 else "Top admin follow-up is"
        answer = f"{prefix} {title}."
        if summary:
            answer += f" {summary}"
        if recommended_action:
            answer += f" Next: {recommended_action}."
        return answer.strip()
    admin_lines = _telegram_profile_admin_lines(container, principal_id=principal_id, limit=2)
    if admin_lines:
        return admin_lines[0]
    return ""


def _telegram_recent_conversation_messages(
    container: AppContainer,
    *,
    principal_id: str,
    current_message_id: str = "",
    limit: int = 8,
) -> list[dict[str, object]]:
    rows = list(container.channel_runtime.list_recent_observations(limit=60, principal_id=principal_id))
    rows.sort(key=lambda row: (str(row.created_at or ""), str(row.observation_id or "")))
    messages: list[dict[str, object]] = []
    seen_reply_texts: set[str] = set()
    current_external_id = str(current_message_id or "").strip()
    for row in rows:
        if str(row.channel or "").strip() != "telegram":
            continue
        payload = dict(row.payload or {})
        event_type = str(row.event_type or "").strip().lower()
        if event_type == "telegram.message":
            if current_external_id and str(row.external_id or "").strip() == current_external_id:
                continue
            text = str(payload.get("text") or "").strip()
            if text:
                messages.append({"role": "user", "content": [{"type": "input_text", "text": text}]})
        elif event_type in {"telegram.reply_sent", "telegram.reply_async_sent"}:
            reply_text = str(payload.get("reply_text") or "").strip()
            if reply_text and reply_text not in seen_reply_texts:
                seen_reply_texts.add(reply_text)
                messages.append({"role": "assistant", "content": [{"type": "output_text", "text": reply_text}]})
    return messages[-limit:]


def _telegram_recent_conversation_focus_lines(
    container: AppContainer,
    *,
    principal_id: str,
    limit: int = 4,
) -> list[str]:
    messages = _telegram_recent_conversation_messages(
        container,
        principal_id=principal_id,
        current_message_id="",
        limit=max(limit * 2, 8),
    )
    rows: list[str] = []
    seen: set[str] = set()
    for item in reversed(messages):
        role = str(item.get("role") or "").strip() or "user"
        content_parts = list(item.get("content") or [])
        text_part = ""
        for part in content_parts:
            if not isinstance(part, dict):
                continue
            text_part = str(part.get("text") or "").strip()
            if text_part:
                break
        if not text_part:
            continue
        normalized = " ".join(text_part.split())
        if normalized in seen:
            continue
        seen.add(normalized)
        rows.append(f"- {role}: {normalized}")
        if len(rows) >= limit:
            break
    rows.reverse()
    return rows


def _telegram_recent_subject_hint_lines(
    container: AppContainer,
    *,
    principal_id: str,
    limit: int = 3,
) -> list[str]:
    messages = _telegram_recent_conversation_messages(
        container,
        principal_id=principal_id,
        current_message_id="",
        limit=12,
    )
    followup_markers = {
        "well?",
        "and?",
        "again?",
        "why?",
        "that one?",
        "the other?",
        "well",
        "and",
        "again",
        "why",
    }
    prefix_patterns = [
        r"^top priority is\s+",
        r"^after that:\s*",
        r"^title:\s*",
        r"^summary:\s*",
        r"^recommendation:\s*",
        r"^next:\s*",
        r"^source:\s*",
    ]
    rows: list[str] = []
    seen: set[str] = set()
    for item in reversed(messages):
        role = str(item.get("role") or "").strip().lower() or "user"
        content_parts = list(item.get("content") or [])
        text_part = ""
        for part in content_parts:
            if not isinstance(part, dict):
                continue
            text_part = str(part.get("text") or "").strip()
            if text_part:
                break
        if not text_part:
            continue
        normalized = " ".join(text_part.split())
        if normalized.lower() in followup_markers:
            continue
        subject = normalized
        if role == "assistant":
            for pattern in prefix_patterns:
                subject = re.sub(pattern, "", subject, flags=re.IGNORECASE).strip()
        subject = subject.strip(" .")
        if len(subject) < 12:
            continue
        if len(subject) > 120:
            subject = subject[:117].rstrip() + "..."
        lowered = subject.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        rows.append(f"- {subject}")
        if len(rows) >= limit:
            break
    rows.reverse()
    return rows


def _telegram_compact_reference_tokens(*values: object) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = str(value or "").strip()
        if not token or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _telegram_brief_reference_suffix(item: object) -> str:
    tokens = _telegram_compact_reference_tokens(
        getattr(item, "id", ""),
        getattr(item, "object_ref", ""),
    )
    evidence_refs = list(getattr(item, "evidence_refs", ()) or ())
    for ref in evidence_refs[:2]:
        tokens.extend(
            _telegram_compact_reference_tokens(
                getattr(ref, "ref_id", ""),
                getattr(ref, "href", ""),
            )
        )
    suffix = ""
    if tokens:
        suffix += " | refs: " + ", ".join(tokens[:4])
    profile_refs = [str(ref or "").strip() for ref in list(getattr(item, "profile_followup_refs", ()) or ()) if str(ref or "").strip()]
    if profile_refs:
        suffix += " | profile refs: " + ", ".join(profile_refs[:3])
    return suffix


def _telegram_queue_reference_suffix(item: object) -> str:
    tokens = _telegram_compact_reference_tokens(
        getattr(item, "id", ""),
    )
    evidence_refs = list(getattr(item, "evidence_refs", ()) or ())
    for ref in evidence_refs[:3]:
        tokens.extend(
            _telegram_compact_reference_tokens(
                getattr(ref, "ref_id", ""),
                getattr(ref, "href", ""),
            )
        )
    suffix = ""
    if tokens:
        suffix += " | refs: " + ", ".join(tokens[:5])
    profile_refs = [str(ref or "").strip() for ref in list(getattr(item, "profile_followup_refs", ()) or ()) if str(ref or "").strip()]
    if profile_refs:
        suffix += " | profile refs: " + ", ".join(profile_refs[:3])
    return suffix


def _telegram_profile_followup_refs_text(item: object) -> str:
    refs = [str(ref or "").strip() for ref in list(getattr(item, "profile_followup_refs", ()) or ()) if str(ref or "").strip()]
    if not refs:
        return ""
    return ", ".join(refs[:3])


def _telegram_property_comparison_lines(brief_items: list[object], *, limit: int = 2) -> list[str]:
    candidates: list[object] = []
    for item in brief_items:
        title = str(getattr(item, "title", "") or "").strip()
        object_ref = str(getattr(item, "object_ref", "") or "").strip().lower()
        why_now = str(getattr(item, "why_now", "") or "").strip()
        recommended_action = str(getattr(item, "recommended_action", "") or "").strip()
        score = float(getattr(item, "score", 0.0) or 0.0)
        if score <= 0.0:
            continue
        title_lower = title.lower()
        looks_property = (
            object_ref.startswith("willhaben:")
            or "listing" in title_lower
            or "apartment" in title_lower
            or "wohnung" in title_lower
            or "property" in title_lower
        )
        if not looks_property:
            continue
        if not why_now and not recommended_action:
            continue
        candidates.append(item)
    if len(candidates) < 2:
        return []
    candidates.sort(key=lambda row: float(getattr(row, "score", 0.0) or 0.0), reverse=True)
    lines: list[str] = []
    top = candidates[:limit]
    for index, item in enumerate(top, start=1):
        title = str(getattr(item, "title", "") or "").strip()
        score = int(round(float(getattr(item, "score", 0.0) or 0.0)))
        why_now = str(getattr(item, "why_now", "") or "").strip()
        recommended_action = str(getattr(item, "recommended_action", "") or "").strip()
        detail = f"- option {index}: {title} (score {score})"
        if why_now:
            detail += f": {why_now}"
        if recommended_action:
            detail += f" | next: {recommended_action}"
        detail += _telegram_brief_reference_suffix(item)
        lines.append(detail)
    return lines


def _telegram_property_candidates(brief_items: list[object]) -> list[object]:
    candidates: list[object] = []
    for item in brief_items:
        title = str(getattr(item, "title", "") or "").strip()
        object_ref = str(getattr(item, "object_ref", "") or "").strip().lower()
        why_now = str(getattr(item, "why_now", "") or "").strip()
        recommended_action = str(getattr(item, "recommended_action", "") or "").strip()
        score = float(getattr(item, "score", 0.0) or 0.0)
        if score <= 0.0:
            continue
        title_lower = title.lower()
        looks_property = (
            object_ref.startswith("willhaben:")
            or "listing" in title_lower
            or "apartment" in title_lower
            or "wohnung" in title_lower
            or "property" in title_lower
        )
        if not looks_property:
            continue
        if not why_now and not recommended_action:
            continue
        candidates.append(item)
    candidates.sort(key=lambda row: float(getattr(row, "score", 0.0) or 0.0), reverse=True)
    return candidates


def _telegram_build_intent_state(
    *,
    text: str = "",
    reply_text: str = "",
    active_object_map: dict[str, str] | None = None,
) -> dict[str, str]:
    active_object_map = dict(active_object_map or {})
    lowered = " ".join((str(text or "") + " " + str(reply_text or "")).lower().split())
    existing_theme_refs: list[str] = []
    for key in ("active_property_profile_refs", "active_queue_profile_refs"):
        raw = str(active_object_map.get(key) or "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            normalized = str(item or "").strip()
            if normalized and normalized not in existing_theme_refs:
                existing_theme_refs.append(normalized)
    intent = ""
    if any(marker in lowered for marker in ("compare", "better", "other one", "that one", "which one", "vs ")):
        if active_object_map.get("active_property_candidate"):
            intent = "property_compare"
    if not intent and any(
        marker in lowered
        for marker in (
            "rehab",
            "kfa",
            "bewilligung",
            "amtsarzt",
            "wiederbestellung",
            "school",
            "schule",
            "kindergarten",
            "pflegefreistellung",
            "insurance",
            "utility",
            "paperwork",
            "follow-up",
            "follow up",
        )
    ):
        intent = "admin_followup"
    if not intent and existing_theme_refs and any(
        marker in lowered for marker in ("paperwork", "that", "that one", "the follow-up", "the paperwork", "the admin")
    ):
        intent = "admin_followup"
    if not intent and any(
        marker in lowered
        for marker in (
            "property",
            "listing",
            "apartment",
            "wohnung",
            "tour",
            "shortlist",
            "willhaben",
        )
    ):
        if active_object_map.get("active_property_candidate") or active_object_map.get("active_queue_item"):
            intent = "property_review"
    if not intent and any(marker in lowered for marker in ("approve", "approval", "reply", "draft", "email thread")):
        if active_object_map.get("active_email_thread") or active_object_map.get("active_queue_item"):
            intent = "email_approval"
    if not intent and any(marker in lowered for marker in ("calendar", "schedule", "appointment", "tomorrow", "focus")):
        intent = "planning"
    if not intent:
        return {}
    result = {"active_intent": intent}
    if existing_theme_refs:
        result["active_profile_themes"] = ", ".join(existing_theme_refs[:4])
    return result


def _telegram_reinforced_profile_themes_from_reply(
    *,
    brief_items: list[object],
    queue_items: list[object],
    reply_text: str,
    active_object_map: dict[str, str] | None = None,
) -> str:
    theme_refs: list[str] = []
    active_object_map = dict(active_object_map or {})
    for key in ("active_property_profile_refs", "active_queue_profile_refs"):
        raw = str(active_object_map.get(key) or "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            normalized = str(item or "").strip()
            if normalized and normalized not in theme_refs:
                theme_refs.append(normalized)
    lowered_reply = " ".join(str(reply_text or "").lower().split())
    if lowered_reply:
        for item in list(brief_items) + list(queue_items):
            title = str(getattr(item, "title", "") or "").strip()
            summary = str(getattr(item, "summary", "") or "").strip()
            object_ref = str(getattr(item, "object_ref", "") or getattr(item, "id", "") or "").strip()
            matches_reply = False
            if title and title.lower() in lowered_reply:
                matches_reply = True
            elif object_ref and object_ref.lower() in lowered_reply:
                matches_reply = True
            elif summary:
                summary_words = [word for word in re.split(r"\W+", summary.lower()) if len(word) >= 5]
                matches_reply = any(word in lowered_reply for word in summary_words[:6])
            if not matches_reply:
                continue
            for ref in list(getattr(item, "profile_followup_refs", ()) or ()):
                normalized = str(ref or "").strip()
                if normalized and normalized not in theme_refs:
                    theme_refs.append(normalized)
    return ", ".join(theme_refs[:4])


def _telegram_reinforced_intent_state_from_reply(
    intent_state: dict[str, str],
    *,
    brief_items: list[object],
    queue_items: list[object],
    reply_text: str,
    active_object_map: dict[str, str] | None = None,
) -> dict[str, str]:
    reinforced = {
        str(key or "").strip(): str(value or "").strip()
        for key, value in dict(intent_state or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }
    lowered_reply = " ".join(str(reply_text or "").lower().split())
    if not lowered_reply:
        return reinforced
    direct_admin_markers = (
        "rehab",
        "kfa",
        "bewilligung",
        "amtsarzt",
        "wiederbestellung",
        "school",
        "schule",
        "kindergarten",
        "pflegefreistellung",
        "insurance",
        "utility",
        "paperwork",
        "follow-up",
        "follow up",
        "care paperwork",
        "authorization",
        "authorisation",
    )
    if any(marker in lowered_reply for marker in direct_admin_markers):
        reinforced["active_intent"] = "admin_followup"
        return reinforced
    theme_refs = [
        str(item or "").strip()
        for item in str(reinforced.get("active_profile_themes") or "").split(",")
        if str(item or "").strip()
    ]
    active_object_map = dict(active_object_map or {})
    for key in ("active_property_profile_refs", "active_queue_profile_refs"):
        raw = str(active_object_map.get(key) or "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            normalized = str(item or "").strip()
            if normalized and normalized not in theme_refs:
                theme_refs.append(normalized)
    if not theme_refs:
        return reinforced
    for item in list(brief_items) + list(queue_items):
        refs = [str(ref or "").strip() for ref in list(getattr(item, "profile_followup_refs", ()) or ()) if str(ref or "").strip()]
        object_ref = str(getattr(item, "object_ref", "") or "").strip()
        if object_ref:
            refs.append(object_ref)
        if not any(ref in theme_refs for ref in refs):
            continue
        title = str(getattr(item, "title", "") or "").strip().lower()
        summary = str(getattr(item, "summary", "") or "").strip().lower()
        why_now = str(getattr(item, "why_now", "") or "").strip().lower()
        recommended_action = str(getattr(item, "recommended_action", "") or "").strip().lower()
        if any(fragment and fragment in lowered_reply for fragment in (title, summary, why_now, recommended_action)):
            reinforced["active_intent"] = "admin_followup"
            return reinforced
    return reinforced


def _telegram_with_admin_followup_state(
    intent_state: dict[str, str],
    *,
    brief_items: list[object],
    queue_items: list[object],
    active_object_map: dict[str, str] | None = None,
) -> dict[str, str]:
    enriched = {
        str(key or "").strip(): str(value or "").strip()
        for key, value in dict(intent_state or {}).items()
        if str(key or "").strip() and str(value or "").strip()
    }
    if str(enriched.get("active_intent") or "").strip().lower() != "admin_followup":
        return enriched
    theme_refs: list[str] = []
    for item in str(enriched.get("active_profile_themes") or "").split(","):
        normalized = str(item or "").strip()
        if normalized and normalized not in theme_refs:
            theme_refs.append(normalized)
    active_object_map = dict(active_object_map or {})
    for key in ("active_property_profile_refs", "active_queue_profile_refs"):
        raw = str(active_object_map.get(key) or "").strip()
        if not raw:
            continue
        for item in raw.split(","):
            normalized = str(item or "").strip()
            if normalized and normalized not in theme_refs:
                theme_refs.append(normalized)
    candidates = _telegram_admin_followup_candidates(
        brief_items,
        queue_items,
        theme_refs=theme_refs,
    )
    if not candidates:
        return enriched
    primary = candidates[0]
    primary_ref = str(getattr(primary, "object_ref", "") or getattr(primary, "id", "") or "").strip()
    primary_title = str(getattr(primary, "title", "") or "").strip()
    if primary_ref:
        enriched["active_admin_primary"] = primary_ref
    if primary_title:
        enriched["active_admin_primary_title"] = primary_title
    if len(candidates) > 1:
        secondary = candidates[1]
        secondary_ref = str(getattr(secondary, "object_ref", "") or getattr(secondary, "id", "") or "").strip()
        secondary_title = str(getattr(secondary, "title", "") or "").strip()
        if secondary_ref:
            enriched["active_admin_secondary"] = secondary_ref
        if secondary_title:
            enriched["active_admin_secondary_title"] = secondary_title
    return enriched


def _telegram_build_active_object_map(
    brief_items: list[object],
    queue_items: list[object],
) -> dict[str, str]:
    result: dict[str, str] = {}

    def _set(label: str, value: str) -> None:
        normalized_label = str(label or "").strip()
        normalized_value = str(value or "").strip()
        if not normalized_label or not normalized_value or normalized_label in result:
            return
        result[normalized_label] = normalized_value

    property_briefs = _telegram_property_candidates(brief_items)
    if property_briefs:
        top_property = property_briefs[0]
        _set(
            "active_property_candidate",
            f"{str(getattr(top_property, 'title', '') or '').strip()} | "
            f"{str(getattr(top_property, 'object_ref', '') or '').strip()}",
        )
        refs = _telegram_brief_reference_suffix(top_property).replace(" | refs: ", "", 1).strip()
        if refs:
            _set("active_property_refs", refs)
        profile_refs = _telegram_profile_followup_refs_text(top_property)
        if profile_refs:
            _set("active_property_profile_refs", profile_refs)

    queue_items_sorted = list(queue_items)
    queue_items_sorted.sort(
        key=lambda row: (
            str(getattr(row, "priority", "") or "").strip().lower() != "high",
            -float(getattr(row, "rank_score", 0.0) or 0.0),
            str(getattr(row, "title", "") or "").strip().lower(),
        )
    )
    if queue_items_sorted:
        top_queue = queue_items_sorted[0]
        _set(
            "active_queue_item",
            f"{str(getattr(top_queue, 'title', '') or '').strip()} | "
            f"{str(getattr(top_queue, 'id', '') or '').strip()}",
        )
        refs = _telegram_queue_reference_suffix(top_queue).replace(" | refs: ", "", 1).strip()
        if refs:
            _set("active_queue_refs", refs)
        profile_refs = _telegram_profile_followup_refs_text(top_queue)
        if profile_refs:
            _set("active_queue_profile_refs", profile_refs)

    email_thread_refs: list[str] = []
    for item in list(queue_items) + list(brief_items):
        evidence_refs = list(getattr(item, "evidence_refs", ()) or ())
        for ref in evidence_refs:
            ref_id = str(getattr(ref, "ref_id", "") or "").strip()
            if ref_id.startswith("gmail-thread:") and ref_id not in email_thread_refs:
                email_thread_refs.append(ref_id)
    if email_thread_refs:
        _set("active_email_thread", email_thread_refs[0])

    return result


def _telegram_build_comparison_state(brief_items: list[object]) -> dict[str, str]:
    candidates = _telegram_property_candidates(brief_items)
    if len(candidates) < 2:
        return {}
    first = candidates[0]
    second = candidates[1]
    first_title = str(getattr(first, "title", "") or "").strip()
    first_ref = str(getattr(first, "object_ref", "") or "").strip()
    first_reason = str(getattr(first, "why_now", "") or "").strip()
    first_action = str(getattr(first, "recommended_action", "") or "").strip()
    first_score = float(getattr(first, "score", 0.0) or 0.0)
    second_title = str(getattr(second, "title", "") or "").strip()
    second_ref = str(getattr(second, "object_ref", "") or "").strip()
    second_reason = str(getattr(second, "why_now", "") or "").strip()
    second_action = str(getattr(second, "recommended_action", "") or "").strip()
    second_score = float(getattr(second, "score", 0.0) or 0.0)
    if not first_title or not second_title:
        return {}
    result = {
        "comparison_primary": f"{first_title} | {first_ref}".strip(),
        "comparison_secondary": f"{second_title} | {second_ref}".strip(),
        "comparison_pair": f"{first_title} | {first_ref} || {second_title} | {second_ref}".strip(),
        "comparison_pair_refs": " ; ".join(
            part for part in (
                _telegram_brief_reference_suffix(first).replace(" | refs: ", "", 1).strip(),
                _telegram_brief_reference_suffix(second).replace(" | refs: ", "", 1).strip(),
            ) if part
        ),
    }
    if first_reason:
        result["comparison_primary_reason"] = first_reason
    if first_action:
        result["comparison_primary_action"] = first_action
    if first_score > 0.0:
        result["comparison_primary_score"] = str(int(round(first_score)))
    if second_reason:
        result["comparison_secondary_reason"] = second_reason
    if second_action:
        result["comparison_secondary_action"] = second_action
    if second_score > 0.0:
        result["comparison_secondary_score"] = str(int(round(second_score)))
    return result


def _telegram_reinforce_comparison_state_from_reply(
    comparison_state: dict[str, str],
    *,
    brief_items: list[object],
    reply_text: str,
) -> dict[str, str]:
    reinforced = dict(comparison_state or {})
    lowered_reply = " ".join(str(reply_text or "").lower().split())
    if not lowered_reply:
        return reinforced
    candidates = _telegram_property_candidates(brief_items)
    if len(candidates) < 2:
        return reinforced
    matched: list[object] = []
    for item in candidates:
        title = str(getattr(item, "title", "") or "").strip()
        object_ref = str(getattr(item, "object_ref", "") or "").strip()
        if not title:
            continue
        if title.lower() in lowered_reply or (object_ref and object_ref.lower() in lowered_reply):
            matched.append(item)
    if not matched:
        return reinforced
    ordered: list[object] = []
    seen_ids: set[str] = set()
    for item in matched + candidates:
        key = str(getattr(item, "id", "") or getattr(item, "object_ref", "") or "").strip()
        if not key or key in seen_ids:
            continue
        seen_ids.add(key)
        ordered.append(item)
        if len(ordered) >= 2:
            break
    if len(ordered) < 2:
        return reinforced
    first = ordered[0]
    second = ordered[1]
    first_title = str(getattr(first, "title", "") or "").strip()
    first_ref = str(getattr(first, "object_ref", "") or "").strip()
    first_reason = str(getattr(first, "why_now", "") or "").strip()
    first_action = str(getattr(first, "recommended_action", "") or "").strip()
    first_score = float(getattr(first, "score", 0.0) or 0.0)
    second_title = str(getattr(second, "title", "") or "").strip()
    second_ref = str(getattr(second, "object_ref", "") or "").strip()
    second_reason = str(getattr(second, "why_now", "") or "").strip()
    second_action = str(getattr(second, "recommended_action", "") or "").strip()
    second_score = float(getattr(second, "score", 0.0) or 0.0)
    if first_title and second_title:
        reinforced["comparison_primary"] = f"{first_title} | {first_ref}".strip()
        reinforced["comparison_secondary"] = f"{second_title} | {second_ref}".strip()
        reinforced["comparison_pair"] = f"{first_title} | {first_ref} || {second_title} | {second_ref}".strip()
        reinforced["comparison_pair_refs"] = " ; ".join(
            part for part in (
                _telegram_brief_reference_suffix(first).replace(" | refs: ", "", 1).strip(),
                _telegram_brief_reference_suffix(second).replace(" | refs: ", "", 1).strip(),
            ) if part
        )
        if first_reason:
            reinforced["comparison_primary_reason"] = first_reason
        if first_action:
            reinforced["comparison_primary_action"] = first_action
        if first_score > 0.0:
            reinforced["comparison_primary_score"] = str(int(round(first_score)))
        if second_reason:
            reinforced["comparison_secondary_reason"] = second_reason
        if second_action:
            reinforced["comparison_secondary_action"] = second_action
        if second_score > 0.0:
            reinforced["comparison_secondary_score"] = str(int(round(second_score)))
    return reinforced


def _telegram_reinforce_active_object_map_from_reply(
    active_object_map: dict[str, str],
    *,
    brief_items: list[object],
    queue_items: list[object],
    reply_text: str,
) -> dict[str, str]:
    reinforced = dict(active_object_map or {})
    lowered_reply = " ".join(str(reply_text or "").lower().split())
    if not lowered_reply:
        return reinforced

    property_briefs = sorted(
        list(brief_items),
        key=lambda row: float(getattr(row, "score", 0.0) or 0.0),
        reverse=True,
    )
    for item in property_briefs:
        title = str(getattr(item, "title", "") or "").strip()
        object_ref = str(getattr(item, "object_ref", "") or "").strip()
        title_lower = title.lower()
        object_ref_lower = object_ref.lower()
        if title and title_lower in lowered_reply or (object_ref and object_ref_lower in lowered_reply):
            reinforced["active_property_candidate"] = f"{title} | {object_ref}".strip(" |")
            refs = _telegram_brief_reference_suffix(item).replace(" | refs: ", "", 1).strip()
            if refs:
                reinforced["active_property_refs"] = refs
            profile_refs = _telegram_profile_followup_refs_text(item)
            if profile_refs:
                reinforced["active_property_profile_refs"] = profile_refs
            break

    queue_candidates = sorted(
        list(queue_items),
        key=lambda row: (
            str(getattr(row, "priority", "") or "").strip().lower() != "high",
            -float(getattr(row, "rank_score", 0.0) or 0.0),
        ),
    )
    for item in queue_candidates:
        title = str(getattr(item, "title", "") or "").strip()
        if title and title.lower() in lowered_reply:
            reinforced["active_queue_item"] = f"{title} | {str(getattr(item, 'id', '') or '').strip()}".strip(" |")
            refs = _telegram_queue_reference_suffix(item).replace(" | refs: ", "", 1).strip()
            if refs:
                reinforced["active_queue_refs"] = refs
            profile_refs = _telegram_profile_followup_refs_text(item)
            if profile_refs:
                reinforced["active_queue_profile_refs"] = profile_refs
            evidence_refs = list(getattr(item, "evidence_refs", ()) or ())
            for ref in evidence_refs:
                ref_id = str(getattr(ref, "ref_id", "") or "").strip()
                if ref_id.startswith("gmail-thread:"):
                    reinforced["active_email_thread"] = ref_id
                    break
            break

    return reinforced


def _telegram_active_object_map_lines(active_object_map: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for key in (
        "active_property_candidate",
        "active_property_refs",
        "active_property_profile_refs",
        "active_queue_item",
        "active_queue_refs",
        "active_queue_profile_refs",
        "active_email_thread",
    ):
        value = str(active_object_map.get(key) or "").strip()
        if value:
            lines.append(f"- {key}: {value}")
    return lines


def _telegram_recent_persisted_comparison_state(
    container: AppContainer,
    *,
    principal_id: str,
) -> dict[str, str]:
    rows = list(container.channel_runtime.list_recent_observations(limit=40, principal_id=principal_id))
    rows.sort(key=lambda row: (str(row.created_at or ""), str(row.observation_id or "")), reverse=True)
    for row in rows:
        if str(row.channel or "").strip() != "telegram":
            continue
        event_type = str(row.event_type or "").strip().lower()
        if event_type not in {"telegram.reply_sent", "telegram.reply_async_sent"}:
            continue
        payload = dict(row.payload or {})
        comparison_state = payload.get("comparison_state")
        if isinstance(comparison_state, dict) and comparison_state:
            return {
                str(key or "").strip(): str(value or "").strip()
                for key, value in comparison_state.items()
                if str(key or "").strip() and str(value or "").strip()
            }
    return {}


def _telegram_recent_persisted_intent_state(
    container: AppContainer,
    *,
    principal_id: str,
) -> dict[str, str]:
    rows = list(container.channel_runtime.list_recent_observations(limit=40, principal_id=principal_id))
    rows.sort(key=lambda row: (str(row.created_at or ""), str(row.observation_id or "")), reverse=True)
    for row in rows:
        if str(row.channel or "").strip() != "telegram":
            continue
        event_type = str(row.event_type or "").strip().lower()
        if event_type not in {"telegram.reply_sent", "telegram.reply_async_sent"}:
            continue
        payload = dict(row.payload or {})
        intent_state = payload.get("intent_state")
        if isinstance(intent_state, dict) and intent_state:
            return {
                str(key or "").strip(): str(value or "").strip()
                for key, value in intent_state.items()
                if str(key or "").strip() and str(value or "").strip()
            }
    return {}


def _telegram_recent_persisted_object_map(
    container: AppContainer,
    *,
    principal_id: str,
) -> dict[str, str]:
    rows = list(container.channel_runtime.list_recent_observations(limit=40, principal_id=principal_id))
    rows.sort(key=lambda row: (str(row.created_at or ""), str(row.observation_id or "")), reverse=True)
    for row in rows:
        if str(row.channel or "").strip() != "telegram":
            continue
        event_type = str(row.event_type or "").strip().lower()
        if event_type not in {"telegram.reply_sent", "telegram.reply_async_sent"}:
            continue
        payload = dict(row.payload or {})
        active_object_map = payload.get("active_object_map")
        if isinstance(active_object_map, dict) and active_object_map:
            return {
                str(key or "").strip(): str(value or "").strip()
                for key, value in active_object_map.items()
                if str(key or "").strip() and str(value or "").strip()
            }
    return {}


def _telegram_office_grounding_text(container: AppContainer, *, principal_id: str) -> str:
    product_service = build_product_service(container)
    events = list(product_service.list_office_events(principal_id=principal_id, limit=12))
    upcoming_calendar = _telegram_upcoming_calendar_events(container, principal_id=principal_id, limit=4)
    ltd_profiles = _telegram_ltd_runtime_profiles(container)
    brief_items = []
    queue_items = []
    preference_lines: list[str] = []
    try:
        bundle = product_service.get_preference_profile(principal_id=principal_id, person_id="self")
        nodes = list(bundle.get("preference_nodes") or [])
        active_nodes = [
            row
            for row in nodes
            if str(row.get("domain") or "").strip().lower() == "willhaben"
            and str(row.get("status") or "").strip().lower() == "active"
        ]
        for row in active_nodes[:8]:
            key = str(row.get("key") or "").strip()
            value = row.get("value_json")
            confidence = float(row.get("confidence") or 0.0)
            if isinstance(value, list):
                rendered = ", ".join(str(item or "").strip() for item in value if str(item or "").strip())
            else:
                rendered = str(value).strip()
            if key and rendered:
                preference_lines.append(f"- {key}: {rendered} (confidence {confidence:.2f})")
    except Exception:
        preference_lines = []
    admin_focus_lines = _telegram_profile_admin_lines(container, principal_id=principal_id, limit=4)
    try:
        brief_items = list(product_service.list_brief_items(principal_id=principal_id, limit=5))
    except Exception:
        brief_items = []
    try:
        queue_items = list(product_service.list_queue(principal_id=principal_id, limit=5))
    except Exception:
        queue_items = []
    lines = [
        "Surface: Telegram chat with the principal.",
        "Use this grounding for personal schedule, inbox, property, and assistant-state questions.",
    ]
    recent_focus_lines = _telegram_recent_conversation_focus_lines(container, principal_id=principal_id, limit=4)
    if recent_focus_lines:
        lines.append("Recent conversation focus:")
        lines.extend(recent_focus_lines)
    recent_subject_lines = _telegram_recent_subject_hint_lines(container, principal_id=principal_id, limit=3)
    if recent_subject_lines:
        lines.append("Likely active subjects for short follow-ups:")
        lines.extend(recent_subject_lines)
    active_object_map = _telegram_build_active_object_map(brief_items, queue_items)
    comparison_state = _telegram_build_comparison_state(brief_items)
    persisted_comparison_state = _telegram_recent_persisted_comparison_state(container, principal_id=principal_id)
    persisted_object_map = _telegram_recent_persisted_object_map(container, principal_id=principal_id)
    persisted_intent_state = _telegram_recent_persisted_intent_state(container, principal_id=principal_id)
    derived_admin_refs: list[str] = []
    for item in list(brief_items) + list(queue_items):
        for ref in list(getattr(item, "profile_followup_refs", ()) or ()):
            normalized_ref = str(ref or "").strip()
            if normalized_ref and normalized_ref not in derived_admin_refs:
                derived_admin_refs.append(normalized_ref)
    for raw in (
        str(persisted_intent_state.get("active_profile_themes") or "").strip(),
        str(active_object_map.get("active_property_profile_refs") or "").strip(),
        str(active_object_map.get("active_queue_profile_refs") or "").strip(),
        str(persisted_object_map.get("active_property_profile_refs") or "").strip(),
        str(persisted_object_map.get("active_queue_profile_refs") or "").strip(),
    ):
        if not raw:
            continue
        for ref in raw.split(","):
            normalized_ref = str(ref or "").strip()
            if normalized_ref and normalized_ref not in derived_admin_refs:
                derived_admin_refs.append(normalized_ref)
    for line in _telegram_admin_focus_lines_from_profile_refs(derived_admin_refs, limit=4):
        if line not in admin_focus_lines:
            admin_focus_lines.append(line)
    merged_object_map = dict(active_object_map)
    for key, value in persisted_object_map.items():
        merged_object_map.setdefault(key, value)
    active_object_map_lines = _telegram_active_object_map_lines(merged_object_map)
    if active_object_map_lines:
        lines.append("Last active object map:")
        lines.extend(active_object_map_lines)
    merged_comparison_state = dict(comparison_state)
    for key, value in persisted_comparison_state.items():
        merged_comparison_state.setdefault(key, value)
    comparison_pair = str(merged_comparison_state.get("comparison_pair") or "").strip()
    if comparison_pair:
        lines.append("Last comparison pair:")
        comparison_primary = str(merged_comparison_state.get("comparison_primary") or "").strip()
        if comparison_primary:
            lines.append(f"- comparison_primary: {comparison_primary}")
        comparison_primary_reason = str(merged_comparison_state.get("comparison_primary_reason") or "").strip()
        if comparison_primary_reason:
            lines.append(f"- comparison_primary_reason: {comparison_primary_reason}")
        comparison_primary_action = str(merged_comparison_state.get("comparison_primary_action") or "").strip()
        if comparison_primary_action:
            lines.append(f"- comparison_primary_action: {comparison_primary_action}")
        comparison_primary_score = str(merged_comparison_state.get("comparison_primary_score") or "").strip()
        if comparison_primary_score:
            lines.append(f"- comparison_primary_score: {comparison_primary_score}")
        comparison_secondary = str(merged_comparison_state.get("comparison_secondary") or "").strip()
        if comparison_secondary:
            lines.append(f"- comparison_secondary: {comparison_secondary}")
        comparison_secondary_reason = str(merged_comparison_state.get("comparison_secondary_reason") or "").strip()
        if comparison_secondary_reason:
            lines.append(f"- comparison_secondary_reason: {comparison_secondary_reason}")
        comparison_secondary_action = str(merged_comparison_state.get("comparison_secondary_action") or "").strip()
        if comparison_secondary_action:
            lines.append(f"- comparison_secondary_action: {comparison_secondary_action}")
        comparison_secondary_score = str(merged_comparison_state.get("comparison_secondary_score") or "").strip()
        if comparison_secondary_score:
            lines.append(f"- comparison_secondary_score: {comparison_secondary_score}")
        lines.append(f"- comparison_pair: {comparison_pair}")
        comparison_pair_refs = str(merged_comparison_state.get("comparison_pair_refs") or "").strip()
        if comparison_pair_refs:
            lines.append(f"- comparison_pair_refs: {comparison_pair_refs}")
    active_intent = str(persisted_intent_state.get("active_intent") or "").strip()
    if active_intent:
        lines.append("Last active intent:")
        lines.append(f"- active_intent: {active_intent}")
        active_profile_themes = str(persisted_intent_state.get("active_profile_themes") or "").strip()
        if active_profile_themes:
            lines.append(f"- active_profile_themes: {active_profile_themes}")
        active_admin_primary = str(persisted_intent_state.get("active_admin_primary") or "").strip()
        if active_admin_primary:
            lines.append(f"- active_admin_primary: {active_admin_primary}")
        active_admin_primary_title = str(persisted_intent_state.get("active_admin_primary_title") or "").strip()
        if active_admin_primary_title:
            lines.append(f"- active_admin_primary_title: {active_admin_primary_title}")
        active_admin_secondary = str(persisted_intent_state.get("active_admin_secondary") or "").strip()
        if active_admin_secondary:
            lines.append(f"- active_admin_secondary: {active_admin_secondary}")
        active_admin_secondary_title = str(persisted_intent_state.get("active_admin_secondary_title") or "").strip()
        if active_admin_secondary_title:
            lines.append(f"- active_admin_secondary_title: {active_admin_secondary_title}")
    if preference_lines:
        lines.append("Active housing preferences:")
        lines.extend(preference_lines)
    if admin_focus_lines:
        lines.append("Active admin focus:")
        lines.extend(f"- {line}" for line in admin_focus_lines)
    if ltd_profiles:
        lines.append("Available LTD runtime lanes:")
        for profile in ltd_profiles[:6]:
            service_name = str(getattr(profile, "service_name", "") or "").strip()
            runtime_state = str(getattr(profile, "runtime_state", "") or "").strip()
            tier = str(getattr(profile, "workspace_integration_tier", "") or "").strip()
            actions = [
                str(getattr(action, "action_key", "") or "").strip()
                for action in list(getattr(profile, "actions", ()) or ())
                if str(getattr(action, "action_key", "") or "").strip()
            ]
            detail = f"- {service_name}"
            if runtime_state:
                detail += f" [{runtime_state}]"
            if tier:
                detail += f" {tier}"
            if actions:
                detail += f" | actions: {', '.join(actions[:4])}"
            lines.append(detail)
    answerly_configs = _answerly_document_qa_configs()
    if answerly_configs:
        lines.append("Document Q&A backend:")
        for config in answerly_configs[:3]:
            scope = str(config.get("scope") or "").strip()
            label = str(config.get("label") or "").strip()
            scope_hint = f" [{scope}]" if scope and scope != "generic" else ""
            lines.append(
                f"- Answerly connected for {label}{scope_hint}. Keep this corpus separate and use it only when the user explicitly asks about that document source or when the active context clearly matches it."
            )
    if upcoming_calendar:
        lines.append("Upcoming calendar events:")
        for event in upcoming_calendar:
            start_text = event["start_at"].astimezone(ZoneInfo("Europe/Vienna")).strftime("%Y-%m-%d %H:%M")
            detail = f"- {start_text}: {event['title']}"
            if str(event.get("location") or "").strip():
                detail += f" @ {str(event.get('location') or '').strip()}"
            attendees = [str(item or "").strip() for item in list(event.get("attendees") or []) if str(item or "").strip()]
            if attendees:
                detail += f" with {', '.join(attendees[:3])}"
            lines.append(detail)
    else:
        lines.append("Upcoming calendar events: none visible in stored workspace signals.")
    recent_product_events = [row for row in events if str(row.get("channel") or "").strip() == "product"][:4]
    if recent_product_events:
        lines.append("Recent PropertyQuarry product events:")
        for row in recent_product_events:
            lines.append(f"- {str(row.get('event_type') or '').strip()}: {str(row.get('summary') or '').strip()}")
    if brief_items:
        lines.append("Top brief items:")
        for item in brief_items:
            score = float(getattr(item, "score", 0.0) or 0.0)
            title = str(getattr(item, "title", "") or "").strip()
            why_now = str(getattr(item, "why_now", "") or "").strip()
            recommended_action = str(getattr(item, "recommended_action", "") or "").strip()
            detail = f"- {title}"
            if score > 0.0:
                detail += f" (score {int(round(score)):d})"
            if why_now:
                detail += f": {why_now}"
            if recommended_action:
                detail += f" | next: {recommended_action}"
            detail += _telegram_brief_reference_suffix(item)
            lines.append(detail)
        comparison_lines = _telegram_property_comparison_lines(brief_items)
        if comparison_lines:
            lines.append("Top property comparisons:")
            lines.extend(comparison_lines)
    if queue_items:
        lines.append("Top queue items:")
        for item in queue_items:
            rank_score = float(getattr(item, "rank_score", 0.0) or 0.0)
            priority = str(getattr(item, "priority", "") or "").strip() or "normal"
            title = str(getattr(item, "title", "") or "").strip()
            summary = str(getattr(item, "summary", "") or "").strip()
            detail = f"- [{priority}] {title}"
            if rank_score > 0.0:
                detail += f" (rank {int(round(rank_score)):d})"
            if summary:
                detail += f": {summary}"
            detail += _telegram_queue_reference_suffix(item)
            lines.append(detail)
    recent_signal_events = [row for row in events if str(row.get("channel") or "").strip() in {"gmail", "calendar", "pocket"}][:6]
    if recent_signal_events:
        lines.append("Recent office signals:")
        for row in recent_signal_events:
            lines.append(
                f"- {str(row.get('channel') or '').strip()} {str(row.get('event_type') or '').strip()}: {str(row.get('summary') or '').strip()}"
            )
    return "\n".join(line for line in lines if line).strip()


def _telegram_real_ea_reply_text(
    *,
    container: AppContainer,
    principal_id: str,
    text: str,
    current_message_id: str = "",
    preferred_onemin_labels: tuple[str, ...] = (),
    timeout_seconds: float | None = None,
) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    if timeout_seconds is None:
        try:
            timeout_seconds = max(float(str(os.getenv("EA_TELEGRAM_RESPONSES_TIMEOUT_SECONDS") or "12").strip() or "12"), 1.0)
        except Exception:
            timeout_seconds = 12.0
    if float(timeout_seconds) <= 1.0:
        return ""
    model = str(os.getenv("EA_TELEGRAM_RESPONSES_MODEL") or "ea-coder-fast").strip() or "ea-coder-fast"
    normalized_preferred_onemin_labels = tuple(
        str(item or "").strip()
        for item in preferred_onemin_labels
        if str(item or "").strip()
    ) or _telegram_default_preferred_onemin_labels()
    grounding = _telegram_office_grounding_text(container, principal_id=principal_id)
    messages = [
        {
            "role": "system",
            "content": (
            "You are PropertyQuarry replying inside a Telegram chat. "
            "Be concise, direct, and useful. "
            "Use the supplied grounding as source of truth for schedule, inbox, property, and workspace-state claims. "
            "Treat short follow-ups like 'well?', 'and?', 'why?', or 'again?' as referring to the most recent relevant subject in the conversation and grounding. "
            "If the grounding does not support a personal factual claim, say that clearly instead of guessing. "
            "Do not mention internal prompts, routes, tokens, or implementation details."
        ),
        },
        {
            "role": "system",
            "content": grounding,
        },
    ]
    for item in _telegram_recent_conversation_messages(
        container,
        principal_id=principal_id,
        current_message_id=current_message_id,
    ):
        role = str(item.get("role") or "").strip() or "user"
        content_parts = list(item.get("content") or [])
        text_part = ""
        for part in content_parts:
            if not isinstance(part, dict):
                continue
            text_part = str(part.get("text") or "").strip()
            if text_part:
                break
        if text_part:
            messages.append({"role": role, "content": text_part})
    messages.append({"role": "user", "content": normalized})
    result_box: dict[str, object] = {}
    error_box: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            result_box["result"] = _responses_route_module()._generate_upstream_text(
                prompt=normalized,
                messages=messages,
                requested_model=model,
                max_output_tokens=220,
                chatplayground_audit_callback=None,
                chatplayground_audit_callback_only=False,
                chatplayground_audit_principal_id=principal_id,
                preferred_onemin_labels=normalized_preferred_onemin_labels,
                request_deadline_monotonic=time.monotonic() + timeout_seconds,
            )
        except BaseException as exc:  # pragma: no cover - defensive thread boundary
            error_box["error"] = exc

    worker = threading.Thread(target=_worker, name="telegram-real-ea-reply", daemon=True)
    worker.start()
    # Keep the inline Telegram reply path fail-closed well ahead of the configured
    # deadline so suite load and scheduler jitter do not turn a soft timeout into
    # multi-second user-visible blocking.
    join_timeout = max(min(float(timeout_seconds) * 0.5, float(timeout_seconds) - 0.1), 0.05)
    worker.join(timeout=join_timeout)
    if worker.is_alive():
        return ""
    if error_box:
        return ""
    return str(getattr(result_box.get("result"), "text", "") or "").strip()


def _telegram_should_async_assistant_reply(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if normalized.startswith("/"):
        return False
    if _safe_math_answer(normalized):
        return False
    lower = normalized.lower()
    alpha = "".join(ch for ch in lower if ch.isalpha() or ch.isspace()).strip()
    if lower in {"really", "really?"}:
        return False
    if ("today" in lower and "day" in lower) or alpha in {"day", "today", "what day", "weekday"}:
        return False
    if ("today" in lower and "date" in lower) or alpha in {"date", "today date", "what date"}:
        return False
    if ("time" in lower and "what" in lower) or alpha in {"time", "current time", "what time"}:
        return False
    schedule_markers = (
        "next appointment",
        "next meeting",
        "next calendar",
        "my calendar",
        "my schedule",
        "next event",
        "what's next",
        "whats next",
        "what is next",
        "appointment",
    )
    if any(marker in lower for marker in schedule_markers):
        return False
    if "http://" in normalized or "https://" in normalized:
        return False
    return True


def _telegram_prefers_local_grounded_reply(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    lower = normalized.lower()
    alpha = "".join(ch for ch in lower if ch.isalpha() or ch.isspace()).strip()
    if _telegram_meta_assistant_reply_text(normalized):
        return True
    if any(
        phrase in lower
        for phrase in (
            "do all of that by itself",
            "do that by itself",
            "handle property alerts by itself",
            "notification here",
        )
    ):
        return True
    if ("today" in lower and "day" in lower) or alpha in {"day", "today", "what day", "weekday"}:
        return True
    if ("today" in lower and "date" in lower) or alpha in {"date", "today date", "what date"}:
        return True
    if ("time" in lower and "what" in lower) or alpha in {"time", "current time", "what time"}:
        return True
    if "weather" in lower:
        return True
    schedule_markers = (
        "next appointment",
        "next meeting",
        "next calendar",
        "my calendar",
        "my schedule",
        "next event",
        "what's next",
        "whats next",
        "what is next",
        "appointment",
    )
    if any(marker in lower for marker in schedule_markers):
        return True
    if "google photos" in lower or "picture" in lower or "photo" in lower:
        return True
    return False


def _telegram_prefers_async_codex_chat(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if normalized.startswith("/"):
        return False
    if _telegram_probe_reply_text(normalized):
        return False
    if _safe_math_answer(normalized):
        return False
    if "http://" in normalized or "https://" in normalized:
        return False
    if _telegram_meta_assistant_reply_text(normalized):
        return False
    if _telegram_prefers_local_grounded_reply(normalized):
        return False
    return True


def _telegram_should_persist_chat_memory(*, text: str, reply_text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    if normalized.startswith("/"):
        return False
    if _telegram_probe_reply_text(normalized):
        return False
    if _telegram_meta_assistant_reply_text(normalized):
        return False
    if _safe_math_answer(normalized):
        return False
    if _telegram_prefers_local_grounded_reply(normalized):
        lower = normalized.lower()
        if "weather" in lower or "google photos" in lower or "picture" in lower or "photo" in lower:
            return False
        alpha = "".join(ch for ch in lower if ch.isalpha() or ch.isspace()).strip()
        if ("today" in lower and "day" in lower) or alpha in {"day", "today", "what day", "weekday"}:
            return False
        if ("today" in lower and "date" in lower) or alpha in {"date", "today date", "what date"}:
            return False
        if ("time" in lower and "what" in lower) or alpha in {"time", "current time", "what time"}:
            return False
    return bool(str(reply_text or "").strip())


def _telegram_compute_reply_memory_state(
    *,
    container: AppContainer,
    principal_id: str,
    text: str,
    reply_text: str,
    used_fallback_only: bool = False,
    probe_reply: str = "",
    last_resort_reply: str = "",
) -> TelegramReplyMemoryState:
    persist_memory = _telegram_should_persist_chat_memory(text=text, reply_text=reply_text)
    fallback_only_without_context = used_fallback_only and reply_text in {probe_reply, last_resort_reply}
    if not persist_memory or fallback_only_without_context:
        return TelegramReplyMemoryState(active_object_map={}, intent_state={}, comparison_state={})
    try:
        product_service = build_product_service(container)
        brief_items = list(product_service.list_brief_items(principal_id=principal_id, limit=5))
        queue_items = list(product_service.list_queue(principal_id=principal_id, limit=5))
        active_object_map = _telegram_reinforce_active_object_map_from_reply(
            _telegram_build_active_object_map(brief_items, queue_items),
            brief_items=brief_items,
            queue_items=queue_items,
            reply_text=reply_text,
        )
        comparison_state = _telegram_reinforce_comparison_state_from_reply(
            _telegram_build_comparison_state(brief_items),
            brief_items=brief_items,
            reply_text=reply_text,
        )
        intent_state = _telegram_build_intent_state(
            text=text,
            reply_text=reply_text,
            active_object_map=active_object_map,
        )
        active_profile_themes = _telegram_reinforced_profile_themes_from_reply(
            brief_items=brief_items,
            queue_items=queue_items,
            reply_text=reply_text,
            active_object_map=active_object_map,
        )
        if active_profile_themes:
            intent_state["active_profile_themes"] = active_profile_themes
        intent_state = _telegram_reinforced_intent_state_from_reply(
            intent_state,
            brief_items=brief_items,
            queue_items=queue_items,
            reply_text=reply_text,
            active_object_map=active_object_map,
        )
        intent_state = _telegram_with_admin_followup_state(
            intent_state,
            brief_items=brief_items,
            queue_items=queue_items,
            active_object_map=active_object_map,
        )
        return TelegramReplyMemoryState(
            active_object_map=active_object_map,
            intent_state=intent_state,
            comparison_state=comparison_state,
        )
    except Exception:
        return TelegramReplyMemoryState(active_object_map={}, intent_state={}, comparison_state={})


def _telegram_send_and_record_reply(
    *,
    container: AppContainer,
    principal_id: str,
    bot_config: dict[str, object],
    chat_id: str,
    dedupe_key: str,
    reply_text: str,
    source_text: str,
    async_mode: bool = False,
    current_message_id: str = "",
    used_fallback_only: bool = False,
    probe_reply: str = "",
    last_resort_reply: str = "",
) -> bool:
    if not reply_text or not chat_id:
        return False
    memory_state = _telegram_compute_reply_memory_state(
        container=container,
        principal_id=principal_id,
        text=source_text,
        reply_text=reply_text,
        used_fallback_only=used_fallback_only,
        probe_reply=probe_reply,
        last_resort_reply=last_resort_reply,
    )
    try:
        receipt = _telegram_send_message(
            bot_token=str(bot_config.get("token") or "").strip(),
            chat_id=chat_id,
            text=reply_text,
        )
    except Exception as exc:
        if async_mode:
            _record_telegram_async_failed(
                container,
                principal_id=principal_id,
                chat_id=chat_id,
                current_message_id=current_message_id,
                prompt_text=source_text,
                stage="send_message",
                error=str(exc),
            )
        return False
    reply_sent = bool(receipt.get("ok"))
    if not reply_sent:
        return False
    result = dict(receipt.get("result") or {})
    if async_mode:
        container.channel_runtime.ingest_observation(
            principal_id=principal_id,
            channel="telegram",
            event_type="telegram.reply_async_sent",
            payload={
                "chat_id": chat_id,
                "reply_text": reply_text,
                "active_object_map": memory_state.active_object_map,
                "intent_state": memory_state.intent_state,
                "comparison_state": memory_state.comparison_state,
                "turn_state": "sent",
            },
            source_id=f"telegram:{chat_id}" if chat_id else "telegram",
            external_id=str(current_message_id or "").strip(),
            dedupe_key=f"{str(current_message_id or '').strip()}:assistant_async_sent" if str(current_message_id or '').strip() else "",
        )
        return True
    _record_telegram_reply_sent(
        container,
        principal_id=principal_id,
        chat_id=chat_id,
        dedupe_key=dedupe_key,
        reply_text=reply_text,
        message_id=str(result.get("message_id") or "").strip(),
        active_object_map=memory_state.active_object_map,
        intent_state=memory_state.intent_state,
        comparison_state=memory_state.comparison_state,
    )
    return True


def _telegram_turn_context(
    *,
    container: AppContainer,
    principal_id: str,
    text: str,
    payload: dict[str, object] | None = None,
    bot_handle: str,
    preferred_onemin_labels: tuple[str, ...] = (),
    current_message_id: str = "",
    chat_id: str = "",
) -> TelegramTurnContext:
    return build_turn_context(
        container=container,
        principal_id=principal_id,
        text=text,
        payload=dict(payload or {}),
        bot_handle=bot_handle,
        preferred_onemin_labels=preferred_onemin_labels,
        current_message_id=current_message_id,
        chat_id=chat_id,
        completion_cue_predicate=_telegram_low_signal_followup_cue,
    )


def _telegram_command_turn_decision(ctx: TelegramTurnContext) -> TelegramTurnDecision:
    command = ctx.normalized.split()[0].split("@", 1)[0].lower() if ctx.normalized else ""
    handle = str(ctx.bot_handle or "").strip() or "this bot"
    if command == "/start":
        return TelegramTurnDecision(
            reply_text=(
                f"{handle} is connected to PropertyQuarry.\n\n"
                "Send property links, notes, and follow-up requests here. "
                "Strong matches and review actions can arrive in this chat when Telegram is selected in Account."
            )
        )
    if command == "/help":
        return TelegramTurnDecision(
            reply_text=(
                "Available commands:\n"
                "/start - connect this chat to PropertyQuarry\n"
                "/help - show this help text\n"
                "/status - check bot and routing status\n\n"
                "/scout_update /scout-update /scoutupdate - generate Scout bundle from a listing link\n"
                "You can also send property links, notes, or requests in plain text."
            )
        )
    if command == "/status":
        return TelegramTurnDecision(
            reply_text=(
                "PropertyQuarry is online.\n"
                "Telegram messages are connected.\n"
                "Email delivery is available.\n"
                "Saved preferences are available."
            )
        )
    return TelegramTurnDecision()


def _telegram_callback_turn_decision(ctx: TelegramTurnContext) -> TelegramTurnDecision:
    if str(ctx.payload.get("kind") or "").strip().lower() != "callback_query":
        return TelegramTurnDecision()
    callback_data = str(ctx.payload.get("callback_data") or "").strip()
    if callback_data.startswith("fb|"):
        callback_packet = decode_telegram_feedback_callback_data(
            bot_token=str(dict(ctx.payload.get("_bot_config") or {}).get("token") or "").strip(),
            callback_data=callback_data,
            chat_id=ctx.chat_id,
        )
        if not bool(callback_packet.get("ok")):
            reason = str(callback_packet.get("reason") or "").strip().lower()
            if reason == "expired":
                return TelegramTurnDecision(reply_text="That feedback button expired. Send a fresh request if you want to tune this again.")
            return TelegramTurnDecision(reply_text="That feedback button is no longer valid.")
        service = build_product_service(ctx.container)
        result = service.record_notification_feedback(
            principal_id=ctx.principal_id,
            notification_key=str(callback_packet.get("notification_key") or "").strip(),
            feedback_key=str(callback_packet.get("feedback_key") or "").strip(),
            actor="telegram_feedback",
            chat_id=ctx.chat_id,
        )
        return TelegramTurnDecision(reply_text=str(result.get("reply_text") or "Noted.").strip() or "Noted.")
    callback_packet = _telegram_decode_callback_data(
        bot_config=dict(ctx.payload.get("_bot_config") or {}),
        callback_data=callback_data,
        chat_id=ctx.chat_id,
    )
    if not bool(callback_packet.get("ok")):
        reason = str(callback_packet.get("reason") or "").strip().lower()
        if reason == "expired":
            return TelegramTurnDecision(reply_text="That button expired. Send the request again if you still want PropertyQuarry to work on it.")
        return TelegramTurnDecision(reply_text="That Telegram action is no longer valid. Send the request again if needed.")
    action = str(callback_packet.get("action") or "").strip().lower()
    current_message_id = str(callback_packet.get("current_message_id") or "").strip()
    snapshot = _telegram_async_turn_snapshot(
        ctx.container,
        principal_id=ctx.principal_id,
        current_message_id=current_message_id,
        chat_id=ctx.chat_id,
    )
    if action == "status":
        status = str(snapshot.get("status") or "").strip().lower()
        failure_reason = str(snapshot.get("error") or "").strip()
        if status == "sent":
            return TelegramTurnDecision(reply_text="PropertyQuarry already finished that request and sent the reply here.")
        if status == "failed":
            if failure_reason:
                return TelegramTurnDecision(
                    reply_text=(
                        "That request failed after processing. "
                        f"Last status: {failure_reason}. "
                        "Tap Retry to try again."
                    )
                )
            return TelegramTurnDecision(reply_text="That request failed after processing. Tap Retry to try again.")
        return TelegramTurnDecision(
            reply_text=(
                "PropertyQuarry is still processing that request.\n"
                "The message is persisted, deduped, and running off the webhook path."
            )
        )
    if action == "help":
        return TelegramTurnDecision(
            reply_text=(
                "Use plain language here.\n"
                "For deterministic things PropertyQuarry answers directly.\n"
                "For heavier requests PropertyQuarry acknowledges first and finishes the work asynchronously."
            )
        )
    if action == "retry":
        status = str(snapshot.get("status") or "").strip().lower()
        if status in {"queued", "processing"}:
            return TelegramTurnDecision(reply_text="PropertyQuarry is already working on that request.")
        if status == "sent":
            return TelegramTurnDecision(reply_text="PropertyQuarry already answered that request here. Send a new message if you want a fresh run.")
        if not str(snapshot.get("prompt_text") or "").strip():
            return TelegramTurnDecision(reply_text="PropertyQuarry could not recover the original request text for that button. Send the request again.")
        retry_message_id = f"{current_message_id}:retry:{int(time.time())}" if current_message_id else f"retry:{int(time.time())}"
        return TelegramTurnDecision(
            schedule_async=True,
            async_text=str(snapshot.get("prompt_text") or "").strip(),
            async_message_id=retry_message_id,
            retry_budget=2,
        )
    return TelegramTurnDecision()


def _telegram_notification_feedback_followup_turn_decision(ctx: TelegramTurnContext) -> TelegramTurnDecision:
    if str(ctx.payload.get("kind") or "").strip().lower() == "callback_query":
        return TelegramTurnDecision()
    normalized = str(ctx.normalized or "").strip()
    if not normalized:
        return TelegramTurnDecision()
    service = build_product_service(ctx.container)
    recorder = getattr(service, "record_notification_feedback_followup_response", None)
    if not callable(recorder):
        return TelegramTurnDecision()
    result = recorder(
        principal_id=ctx.principal_id,
        chat_id=ctx.chat_id,
        text=normalized,
        actor="telegram_feedback_followup",
    )
    if str(result.get("status") or "").strip().lower() in {"recorded", "duplicate", "empty"}:
        return TelegramTurnDecision(reply_text=str(result.get("reply_text") or "").strip() or "Noted.")
    return TelegramTurnDecision()


def _telegram_latest_supported_property_link_in_telegram_chat(
    container: AppContainer,
    *,
    principal_id: str,
    chat_id: str,
) -> str:
    normalized_chat_id = str(chat_id or "").strip()
    for row in container.channel_runtime.list_recent_observations(limit=80, principal_id=principal_id):
        if str(row.channel or "").strip().lower() != "telegram":
            continue
        payload = dict(row.payload or {})
        if normalized_chat_id and str(payload.get("chat_id") or "").strip() != normalized_chat_id:
            continue
        event_type = str(row.event_type or "").strip().lower()
        text = ""
        if event_type == "telegram.message":
            text = str(payload.get("text") or "").strip()
        elif event_type in {"telegram.reply_sent", "telegram.reply_async_sent", "telegram.reply_async_started", "telegram.reply_async_failed"}:
            text = str(payload.get("reply_text") or payload.get("prompt_text") or "").strip()
        if not text:
            continue
        candidate = _telegram_supported_property_link(text)
        if candidate:
            return candidate
    return ""


def _telegram_scout_update_turn_decision(ctx: TelegramTurnContext) -> TelegramTurnDecision:
    normalized = str(ctx.normalized or "").strip()
    if not normalized:
        return TelegramTurnDecision()
    lowered = " ".join(normalized.lower().split())
    if not (
        "/scout_update" in lowered
        or "/scoutupdate" in lowered
        or "/scout-update" in lowered
        or "scout update" in lowered
    ):
        return TelegramTurnDecision()
    property_url = _telegram_supported_property_link(normalized)
    if not property_url and ctx.chat_id:
        property_url = _telegram_latest_supported_property_link_in_telegram_chat(
            ctx.container,
            principal_id=ctx.principal_id,
            chat_id=ctx.chat_id,
        )
    if not property_url:
        return TelegramTurnDecision(
            reply_text=(
                "Scout update needs the listing link. Send: /scout_update <property link> and "
                "I will prepare the review, dossier, preview, and the buttons to request 3D or walkthrough media."
            )
        )
    async_text = normalized
    if property_url not in async_text:
        async_text = f"{async_text} {property_url}"
    return TelegramTurnDecision(
        schedule_async=True,
        async_text=async_text,
        async_message_id=ctx.current_message_id,
        suppress_async_ack=True,
    )


def _telegram_link_turn_decision(ctx: TelegramTurnContext) -> TelegramTurnDecision:
    if "http://" not in ctx.normalized and "https://" not in ctx.normalized:
        return TelegramTurnDecision()
    property_url = _telegram_supported_property_link(ctx.normalized)
    if property_url:
        return TelegramTurnDecision(
            schedule_async=True,
            async_text=ctx.normalized,
            async_message_id=ctx.current_message_id,
            suppress_async_ack=True,
        )
    broker_portal_url = _telegram_login_walled_property_link(ctx.normalized)
    if broker_portal_url:
        return TelegramTurnDecision(
            reply_text=(
                "Link received. This broker portal is behind an authenticated service-portal session, "
                "so PropertyQuarry cannot truthfully build the 3D tour, flythrough, or dossier from the raw link alone. "
                "Send a public expose link, the broker PDF, or the listing photos/screenshots here and PropertyQuarry can continue from that."
            )
        )
    local_assistant_reply = _telegram_local_assistant_reply_text(
        ctx.container,
        principal_id=ctx.principal_id,
        text=ctx.normalized,
    )
    if local_assistant_reply:
        return TelegramTurnDecision(reply_text=local_assistant_reply)
    return TelegramTurnDecision(
        reply_text="Link received. PropertyQuarry captured it for review."
    )


def _telegram_supported_property_link(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    for raw in _URL_RE.findall(normalized):
        candidate = str(raw or "").strip().rstrip(").,!?]}>")
        if candidate and product_service_module._property_scout_is_supported_listing_url(candidate):
            return candidate
    return ""


def _telegram_property_link_bundle_retryable_pending_reason(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    if not normalized:
        return False
    return (
        "3d tour missing" in normalized
        or "flythrough video missing" in normalized
        or "dossier not rendered" in normalized
    )


def _telegram_property_link_bundle_retryable_failed_reason(reason: str) -> bool:
    normalized = str(reason or "").strip().lower()
    if not normalized:
        return False
    return (
        "timeout" in normalized
        or "rate limit" in normalized
        or "temporar" in normalized
        or "429" in normalized
        or "502" in normalized
        or "503" in normalized
        or "504" in normalized
        or "connection" in normalized
        or "network" in normalized
        or "upstream" in normalized
        or "service unavailable" in normalized
        or "internal server error" in normalized
    )


def _telegram_property_tour_upgrade_hint(reason: str) -> str:
    normalized = str(reason or "").strip().lower()
    if "property_tour_upgrade_required:plus" in normalized:
        return (
            "You reached your Free tier tour limit (1/day). "
            "Upgrade to Plus for 3 property dossiers per day."
        )
    if "property_tour_upgrade_required:agent" in normalized:
        return (
            "You reached your Plus daily tour limit (3/day). "
            "Upgrade to Agent for unlimited artifact generation."
        )
    return ""


def _telegram_property_link_bundle_error_reply(reason: str, *, prompt_text: str = "") -> str:
    upgrade_hint = _telegram_property_tour_upgrade_hint(str(reason or ""))
    if upgrade_hint:
        return upgrade_hint
    normalized_reason = str(reason or "an internal error").strip()
    if len(normalized_reason) > 180:
        normalized_reason = f"{normalized_reason[:177].rstrip()}..."
    if not normalized_reason:
        normalized_reason = "an internal error"
    if prompt_text:
        return (
            "I couldn’t build the full property package right now. "
            f"Error: {normalized_reason}. Send this once more if you want me to retry."
        )
    return (
        "I couldn’t build the full property package right now. "
        f"Error: {normalized_reason}."
    )


def _compact_telegram_style_hint(value: str, *, max_length: int = 140) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    compact = re.sub(r"\s+", " ", raw).strip(" -_:;,.()[]{}\"'")
    if not compact:
        return ""
    if len(compact) <= max_length:
        return compact
    truncated = compact[:max_length].rstrip(" -_:;,.")
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    return truncated.strip(" -_:;,.")


def _telegram_property_link_diorama_style_hint(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    without_links = re.sub(r"\s+", " ", _URL_RE.sub(" ", normalized)).strip()
    if not without_links:
        return ""

    explicit_patterns = (
        r"(?i)(?:diorama|preview|flythrough|image)\s+(?:style|theme|look|aesthetic|vibe|mood)\s*[:\-]\s*([^.;\n]{4,140})",
        r"(?i)(?:style|theme|aesthetic|look|vibe|mood)\s*[:\-]\s*([^.;\n]{4,140})",
        r"(?i)(?:style|theme|aesthetic|look|vibe|mood)\s+(?:should|must|needs|would|want)\s+be\s+([^.;\n]{4,140})",
        r"(?i)(?:render|design|interior|ambiance|ambience|interiority|diorama)\s+(?:in|as)\s+([^.;\n]{4,140})",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, without_links)
        if match:
            extracted = _compact_telegram_style_hint(match.group(1), max_length=140)
            if extracted:
                return extracted

    lower_without_links = without_links.lower()
    for token in (
        "scandinavian",
        "scandi",
        "nordic",
        "minimalist",
        "minimal",
        "industrial",
        "boho",
        "bohemian",
        "mid-century",
        "contemporary",
        "modern",
        "vintage",
        "retro",
        "cozy",
        "warm",
        "cool",
        "dark",
        "monochrome",
        "loft",
        "elegant",
        "luxury",
        "earthy",
        "neutral",
    ):
        token_normalized = f" {token} "
        if token_normalized in f" {lower_without_links} ":
            return "scandinavian" if token == "scandi" else token
    return ""


def _telegram_property_link_birthday_party_request(text: str) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    without_links = re.sub(r"\s+", " ", _URL_RE.sub(" ", normalized)).strip().lower()
    if not without_links:
        return False
    return any(
        token in without_links
        for token in (
            "birthday party",
            "birthday flythrough",
            "birthday video",
            "geburtstag",
            "geburtstagsfeier",
            "geburtstagsparty",
            "kindergeburtstag",
            "party flythrough",
            "party video",
            "our son",
            "unser sohn",
        )
    )


def _telegram_property_pdf_document_payload(ctx: TelegramTurnContext) -> dict[str, object]:
    payload = dict(ctx.payload or {})
    if str(payload.get("kind") or "").strip().lower() != "document":
        return {}
    metadata = dict(payload.get("message_metadata") or {})
    filename = str(metadata.get("file_name") or "").strip()
    download_url = str(metadata.get("download_url") or "").strip()
    if not filename.lower().endswith(".pdf") or not download_url:
        return {}
    signal_text = " ".join(
        part
        for part in (
            str(ctx.normalized or "").strip(),
            filename,
            str(metadata.get("caption") or "").strip(),
        )
        if part
    ).lower()
    if not any(
        marker in signal_text
        for marker in (
            "property",
            "propertyquarry",
            "scout",
            "dossier",
            "wohnung",
            "immobil",
            "expose",
            "exposé",
            "grundriss",
            "floorplan",
            "makler",
            "real estate",
        )
    ):
        return {}
    return {
        "kind": "property_pdf_document",
        "source_pdf_url": download_url,
        "source_pdf_filename": filename,
        "caption": str(metadata.get("caption") or ctx.normalized or "").strip(),
        "telegram_file_id": str(metadata.get("file_id") or "").strip(),
    }


def _telegram_property_pdf_turn_decision(ctx: TelegramTurnContext) -> TelegramTurnDecision:
    pdf_payload = _telegram_property_pdf_document_payload(ctx)
    if not pdf_payload:
        return TelegramTurnDecision()
    filename = str(pdf_payload.get("source_pdf_filename") or "property.pdf").strip() or "property.pdf"
    return TelegramTurnDecision(
        schedule_async=True,
        async_text=f"Property PDF upload: {filename}",
        async_message_id=ctx.current_message_id,
        async_payload=pdf_payload,
        suppress_async_ack=True,
    )


def _telegram_login_walled_property_link(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        return ""
    for raw in _URL_RE.findall(normalized):
        candidate = str(raw or "").strip().rstrip(").,!?]}>")
        lowered = candidate.lower()
        if any(marker in lowered for marker in ("service.immo/objekt/", "service.immo/login/generate_link")):
            return candidate
    return ""


def _telegram_local_tool_priority(ctx: TelegramTurnContext) -> bool:
    persisted_intent_state = _telegram_recent_persisted_intent_state(
        ctx.container,
        principal_id=ctx.principal_id,
    )
    if _telegram_pocket_candidate_selection(ctx.normalized) > 0:
        return True
    if _telegram_audio_upload_announcement_reply_text(ctx.normalized):
        return True
    if _telegram_pocket_audio_query_candidate(ctx.normalized):
        return True
    if _telegram_answerly_document_query_candidate(ctx.normalized):
        return True
    if any(marker in ctx.lower for marker in ("google photos", "photo picker", "picture", "photo")):
        return True
    if any(
        phrase in ctx.lower
        for phrase in (
            "next appointment",
            "next meeting",
            "next calendar",
            "my calendar",
            "what is my next appointment",
            "what's my next appointment",
            "focus on tomorrow",
            "what should i focus on tomorrow",
            "what should i focus on",
            "what should i do tomorrow",
            "what is tomorrow like",
        )
    ):
        return True
    if ctx.alpha_words and all(word in {"voice", "message", "done", "finished", "complete", "completed", "ok", "okay"} for word in ctx.alpha_words):
        return True
    return str(persisted_intent_state.get("active_intent") or "").strip().lower() == "admin_followup"


def _telegram_force_async_path(ctx: TelegramTurnContext) -> bool:
    if str(os.getenv("EA_TELEGRAM_STRICT_DECOUPLED_MODE") or "1").strip().lower() in {"0", "false", "no", "off"}:
        return False
    if str(ctx.payload.get("kind") or "").strip().lower() == "callback_query":
        return False
    if not ctx.normalized:
        return False
    if ctx.normalized.startswith("/"):
        return False
    if "http://" in ctx.lower or "https://" in ctx.lower:
        return False
    if _telegram_answerly_document_query_candidate(ctx.normalized):
        return False
    if _safe_math_answer(ctx.normalized):
        return False
    if "audit plan" in ctx.lower:
        return True
    if ctx.lower in {"really", "really?"}:
        return False
    if _telegram_low_signal_followup_cue(ctx.normalized):
        return False
    if _telegram_meta_assistant_reply_text(ctx.normalized):
        return False
    if _telegram_prefers_local_grounded_reply(ctx.normalized):
        return False
    if len(ctx.alpha_words) <= 2 and not any(marker in ctx.lower for marker in ("?", "why", "what", "when", "where", "how", "which")):
        return False
    return True


def _telegram_async_turn_snapshot(
    container: AppContainer,
    *,
    principal_id: str,
    current_message_id: str,
    chat_id: str,
) -> dict[str, str]:
    normalized_message_id = str(current_message_id or "").strip()
    normalized_chat_id = str(chat_id or "").strip()
    if not normalized_message_id:
        return {"status": "unknown", "prompt_text": ""}
    if container.channel_runtime.find_observation_by_dedupe(
        f"{normalized_message_id}:assistant_async_sent",
        principal_id=principal_id,
    ) is not None:
        return {"status": "sent", "prompt_text": ""}
    if container.channel_runtime.find_observation_by_dedupe(
        f"{normalized_message_id}:assistant_async_failed",
        principal_id=principal_id,
    ) is not None:
        for row in container.channel_runtime.list_recent_observations(limit=200, principal_id=principal_id):
            if str(row.channel or "").strip() != "telegram":
                continue
            if str(row.event_type or "").strip() != "telegram.reply_async_failed":
                continue
            if str(getattr(row, "external_id", "") or "").strip() != normalized_message_id:
                continue
            payload = dict(row.payload or {})
            if normalized_chat_id and str(payload.get("chat_id") or "").strip() != normalized_chat_id:
                continue
            return {
                "status": "failed",
                "prompt_text": str(payload.get("prompt_text") or "").strip(),
                "error": str(payload.get("error") or "").strip(),
            }
        return {"status": "failed", "prompt_text": "", "error": ""}
    processing = container.channel_runtime.find_observation_by_dedupe(
        f"{normalized_message_id}:assistant_async_processing",
        principal_id=principal_id,
    )
    if processing is not None:
        payload = dict(processing.payload or {})
        return {"status": "processing", "prompt_text": str(payload.get("prompt_text") or "").strip()}
    for row in container.channel_runtime.list_recent_observations(limit=400, principal_id=principal_id):
        if str(row.channel or "").strip() != "telegram":
            continue
        if str(row.event_type or "").strip() != "telegram.reply_async_started":
            continue
        payload = dict(row.payload or {})
        message_id = str(payload.get("current_message_id") or "").strip()
        if not message_id:
            message_id = str(payload.get("dedupe_key") or "").strip().split(":")[-1].strip()
        if message_id != normalized_message_id:
            continue
        if normalized_chat_id and str(payload.get("chat_id") or "").strip() != normalized_chat_id:
            continue
        return {"status": "queued", "prompt_text": str(payload.get("prompt_text") or "").strip()}
    return {"status": "unknown", "prompt_text": ""}


def _telegram_local_reply_allowed(ctx: TelegramTurnContext, reply_text: str) -> bool:
    if not reply_text:
        return False
    if (
        ctx.is_completion_cue
        and ctx.chat_id
        and _telegram_is_google_photos_picker_block_reply(reply_text)
        and _telegram_recent_messages_include_google_photos_context(
            ctx.container,
            principal_id=ctx.principal_id,
        )
        and _telegram_same_reply_recently_sent(
            ctx.container,
            principal_id=ctx.principal_id,
            chat_id=ctx.chat_id,
            reply_text=reply_text,
        )
    ):
        return False
    if ctx.is_completion_cue and ctx.chat_id and _telegram_same_reply_recently_sent(
        ctx.container,
        principal_id=ctx.principal_id,
        chat_id=ctx.chat_id,
        reply_text=reply_text,
    ):
        return False
    return True


def _telegram_local_turn_decision(ctx: TelegramTurnContext) -> TelegramTurnDecision:
    audio_upload_reply = _telegram_audio_upload_announcement_reply_text(ctx.normalized)
    if audio_upload_reply:
        return TelegramTurnDecision(reply_text=audio_upload_reply)
    pocket_audio_reply = _telegram_pocket_audio_reply_text(
        container=ctx.container,
        principal_id=ctx.principal_id,
        text=ctx.normalized,
    )
    if pocket_audio_reply:
        return TelegramTurnDecision(reply_text=pocket_audio_reply)
    local_assistant_reply = _telegram_local_assistant_reply_text(
        ctx.container,
        principal_id=ctx.principal_id,
        text=ctx.normalized,
    )
    if _telegram_local_reply_allowed(ctx, local_assistant_reply):
        return TelegramTurnDecision(reply_text=local_assistant_reply)
    calendar_reply = _telegram_direct_calendar_reply_text(
        container=ctx.container,
        principal_id=ctx.principal_id,
        text=ctx.normalized,
    )
    if calendar_reply:
        return TelegramTurnDecision(reply_text=calendar_reply)
    return TelegramTurnDecision()


def _telegram_async_assistant_reply_worker(
    *,
    container: AppContainer,
    principal_id: str,
    bot_config: dict[str, object],
    chat_id: str,
    text: str,
    current_message_id: str,
    retry_budget: int = 2,
    async_payload: dict[str, object] | None = None,
) -> None:
    _record_telegram_async_processing(
        container,
        principal_id=principal_id,
        chat_id=chat_id,
        current_message_id=current_message_id,
        prompt_text=text,
    )
    remaining_retries = max(0, int(retry_budget or 0)) + _telegram_property_link_bundle_poll_attempts()
    poll_backoff_seconds = _telegram_property_link_bundle_poll_backoff_seconds()
    payload = dict(async_payload or {})
    if str(payload.get("kind") or "").strip().lower() == "property_pdf_document":
        try:
            service = build_product_service(container)
            result = service.deliver_telegram_property_pdf_bundle(
                principal_id=principal_id,
                source_pdf_url=str(payload.get("source_pdf_url") or "").strip(),
                source_pdf_filename=str(payload.get("source_pdf_filename") or "").strip(),
                caption=str(payload.get("caption") or "").strip(),
                actor="telegram_property_pdf",
                source_ref=f"telegram:{chat_id}:{current_message_id or hashlib.sha256(str(payload.get('source_pdf_url') or '').encode('utf-8')).hexdigest()[:16]}",
                external_id=str(payload.get("telegram_file_id") or payload.get("source_pdf_url") or "").strip(),
            )
        except Exception as exc:
            failure_reason = str(exc)
            _record_telegram_async_failed(
                container,
                principal_id=principal_id,
                chat_id=chat_id,
                current_message_id=current_message_id,
                prompt_text=text,
                stage="property_pdf_bundle",
                error=failure_reason,
            )
            if chat_id:
                _telegram_send_and_record_reply(
                    container=container,
                    principal_id=principal_id,
                    bot_config=bot_config,
                    chat_id=chat_id,
                    dedupe_key="",
                    reply_text=(
                        "I couldn't build the property PDF packet from that upload right now. "
                        f"Error: {compact_text(failure_reason, fallback='property_pdf_bundle_failed', limit=160)}."
                    ),
                    source_text=text,
                    async_mode=True,
                    current_message_id=current_message_id,
                    used_fallback_only=True,
                    probe_reply=_telegram_probe_reply_text(text),
                    last_resort_reply=_telegram_last_resort_reply_text(text),
                )
            return
        status = str(result.get("status") or "").strip()
        if status == "sent":
            _record_telegram_async_sent(
                container,
                principal_id=principal_id,
                chat_id=chat_id,
                current_message_id=current_message_id,
                prompt_text=text,
                reply_text=f"property_pdf_bundle_sent:{str(result.get('source_pdf_filename') or '').strip()}",
                used_fallback_only=False,
            )
            return
        reason = str(result.get("reason") or result.get("error") or status or "property_pdf_bundle_failed").strip()
        _record_telegram_async_failed(
            container,
            principal_id=principal_id,
            chat_id=chat_id,
            current_message_id=current_message_id,
            prompt_text=text,
            stage="property_pdf_bundle_status",
            error=reason,
        )
        if chat_id:
            _telegram_send_and_record_reply(
                container=container,
                principal_id=principal_id,
                bot_config=bot_config,
                chat_id=chat_id,
                dedupe_key="",
                reply_text=(
                    "I couldn't build the property PDF packet from that upload right now. "
                    f"Current status: {compact_text(reason, fallback='property_pdf_bundle_failed', limit=160)}."
                ),
                source_text=text,
                async_mode=True,
                current_message_id=current_message_id,
                used_fallback_only=True,
                probe_reply=_telegram_probe_reply_text(text),
                last_resort_reply=_telegram_last_resort_reply_text(text),
            )
        return
    property_url = _telegram_supported_property_link(text)
    if property_url:
        style_hint = _telegram_property_link_diorama_style_hint(text)
        birthday_party_request = _telegram_property_link_birthday_party_request(text)
        while True:
            try:
                service = build_product_service(container)
                result = service.deliver_telegram_property_link_bundle(
                    principal_id=principal_id,
                    property_url=property_url,
                    actor="telegram_property_link",
                    source_ref=f"telegram:{chat_id}:{current_message_id or hashlib.sha256(property_url.encode('utf-8')).hexdigest()[:16]}",
                    external_id=property_url,
                    preference_person_id="self",
                    style_hint=style_hint,
                    birthday_party_request=birthday_party_request,
                )
            except Exception as exc:
                failure_reason = str(exc)
                reply_text = _telegram_property_link_bundle_error_reply(failure_reason, prompt_text=text)
                _record_telegram_async_failed(
                    container,
                    principal_id=principal_id,
                    chat_id=chat_id,
                    current_message_id=current_message_id,
                    prompt_text=text,
                    stage="property_link_bundle",
                    error=failure_reason,
                )
                if chat_id:
                    _telegram_send_and_record_reply(
                        container=container,
                        principal_id=principal_id,
                        bot_config=bot_config,
                        chat_id=chat_id,
                        dedupe_key="",
                        reply_text=reply_text,
                        source_text=text,
                        async_mode=True,
                        current_message_id=current_message_id,
                        used_fallback_only=True,
                        probe_reply=_telegram_probe_reply_text(text),
                        last_resort_reply=_telegram_last_resort_reply_text(text),
                    )
                return
            status = str(result.get("status") or "").strip()
            if status == "sent":
                _record_telegram_async_sent(
                    container,
                    principal_id=principal_id,
                    chat_id=chat_id,
                    current_message_id=current_message_id,
                    prompt_text=text,
                    reply_text=f"property_link_bundle_sent:{property_url}",
                    used_fallback_only=False,
                )
                return
            pending_reasons = [str(item or "").strip() for item in list(result.get("pending_reasons") or []) if str(item or "").strip()]
            if status in {"pending", "failed", "blocked"}:
                failure_reason = str(result.get("reason") or result.get("error") or "").strip()
                if status == "failed" and not pending_reasons and failure_reason:
                    pending_reasons = [failure_reason]
                if status == "blocked":
                    blocked_reason = str(result.get("blocked_reason") or failure_reason or "").strip()
                    if blocked_reason:
                        normalized_blocked_reason = blocked_reason.replace("_", " ").strip()
                        if normalized_blocked_reason and not any(
                            str(item or "").strip() == normalized_blocked_reason for item in pending_reasons
                        ):
                            if "media missing" in normalized_blocked_reason:
                                pending_reasons.append(f"3D tour missing ({normalized_blocked_reason})")
                            else:
                                pending_reasons.append(normalized_blocked_reason)
                can_retry = False
                if status in {"pending", "blocked"}:
                    can_retry = all(
                        _telegram_property_link_bundle_retryable_pending_reason(item) for item in pending_reasons
                    ) if pending_reasons else False
                else:
                    can_retry = all(
                        _telegram_property_link_bundle_retryable_failed_reason(item) for item in pending_reasons
                    ) if pending_reasons else False
                upgrade_hint = ""
                for reason in pending_reasons:
                    upgrade_hint = _telegram_property_tour_upgrade_hint(reason)
                    if upgrade_hint:
                        break
                if can_retry and remaining_retries > 0:
                    remaining_retries -= 1
                    if poll_backoff_seconds > 0:
                        time.sleep(poll_backoff_seconds)
                    continue
                if pending_reasons:
                    reason_text = "; ".join(pending_reasons)
                else:
                    reason_text = str(result.get("status") or result.get("reason") or "property_link_bundle_processing")
                if upgrade_hint:
                    reason_text = f"{reason_text}. {upgrade_hint}"
                reply_text = (
                    "I still can’t build the full property package right now. "
                    f"Current status: {reason_text}. "
                    "I’ll try again if you send this once more."
                )
                _record_telegram_async_failed(
                    container,
                    principal_id=principal_id,
                    chat_id=chat_id,
                    current_message_id=current_message_id,
                    prompt_text=text,
                    stage="property_link_bundle_status",
                    error=f"{status}:{reason_text}",
                )
                _telegram_send_and_record_reply(
                    container=container,
                    principal_id=principal_id,
                    bot_config=bot_config,
                    chat_id=chat_id,
                    dedupe_key="",
                    reply_text=reply_text,
                    source_text=text,
                    async_mode=True,
                    current_message_id=current_message_id,
                    used_fallback_only=True,
                    probe_reply=_telegram_probe_reply_text(text),
                    last_resort_reply=_telegram_last_resort_reply_text(text),
                )
                return
            _record_telegram_async_failed(
                container,
                principal_id=principal_id,
                chat_id=chat_id,
                current_message_id=current_message_id,
                prompt_text=text,
                stage="property_link_bundle_status",
                error=str(result.get("reason") or result.get("status") or "property_link_bundle_failed"),
            )
            break
    probe_reply = _telegram_probe_reply_text(text)
    last_resort_reply = _telegram_last_resort_reply_text(text)
    reply_text = _telegram_pocket_audio_reply_text(
        container=container,
        principal_id=principal_id,
        text=text,
    ).strip()
    used_fallback_only = False
    if not reply_text and not probe_reply:
        try:
            async_timeout = None
            try:
                async_timeout = max(
                    float(str(os.getenv("EA_TELEGRAM_ASYNC_REAL_REPLY_TIMEOUT_SECONDS") or "18").strip() or "18"),
                    2.0,
                )
            except Exception:
                async_timeout = 18.0
            reply_text = _telegram_real_ea_reply_text(
                container=container,
                principal_id=principal_id,
                text=text,
                current_message_id=current_message_id,
                preferred_onemin_labels=tuple(
                    str(item or "").strip()
                    for item in list(bot_config.get("preferred_onemin_labels") or ())
                    if str(item or "").strip()
                ),
                timeout_seconds=async_timeout,
            ).strip()
        except Exception as exc:
            _record_telegram_async_failed(
                container,
                principal_id=principal_id,
                chat_id=chat_id,
                current_message_id=current_message_id,
                prompt_text=text,
                stage="real_reply",
                error=str(exc),
            )
            reply_text = ""
    if not reply_text:
        reply_text = (
            probe_reply
            or
            _telegram_local_assistant_reply_text(container, principal_id=principal_id, text=text).strip()
            or _telegram_general_reply_text(container=container, principal_id=principal_id, text=text).strip()
            or last_resort_reply
        )
        used_fallback_only = bool(reply_text)
    if not reply_text:
        _record_telegram_async_failed(
            container,
            principal_id=principal_id,
            chat_id=chat_id,
            current_message_id=current_message_id,
            prompt_text=text,
            stage="empty_reply",
            error="no_reply_text",
        )
        return
    _telegram_send_and_record_reply(
        container=container,
        principal_id=principal_id,
        bot_config=bot_config,
        chat_id=chat_id,
        dedupe_key="",
        reply_text=reply_text,
        source_text=text,
        async_mode=True,
        current_message_id=current_message_id,
        used_fallback_only=used_fallback_only,
        probe_reply=probe_reply,
        last_resort_reply=last_resort_reply,
    )


def _telegram_processing_ack_buttons(
    *,
    bot_config: dict[str, object],
    current_message_id: str,
    chat_id: str,
) -> list[list[tuple[str, str]]]:
    status_packet = _telegram_encode_callback_data(
        bot_config=bot_config,
        action="status",
        current_message_id=current_message_id,
        chat_id=chat_id,
    )
    retry_packet = _telegram_encode_callback_data(
        bot_config=bot_config,
        action="retry",
        current_message_id=current_message_id,
        chat_id=chat_id,
    )
    help_packet = _telegram_encode_callback_data(
        bot_config=bot_config,
        action="help",
        current_message_id=current_message_id,
        chat_id=chat_id,
    )
    buttons: list[list[tuple[str, str]]] = []
    first_row = [(label, value) for label, value in (("Status", status_packet), ("Retry", retry_packet)) if value]
    second_row = [(label, value) for label, value in (("Help", help_packet),) if value]
    if first_row:
        buttons.append(first_row)
    if second_row:
        buttons.append(second_row)
    return buttons


def _telegram_processing_ack_buttons_payload(
    *,
    bot_config: dict[str, object],
    current_message_id: str,
    chat_id: str,
) -> list[list[str]]:
    return [
        [value for _, value in row]
        for row in _telegram_processing_ack_buttons(
            bot_config=bot_config,
            current_message_id=current_message_id,
            chat_id=chat_id,
        )
    ]


def _telegram_processing_ack_text(text: str, *, render_priority: str = "") -> str:
    normalized = str(text or "").strip()
    base = "Saved. PropertyQuarry is processing this asynchronously now."
    if _telegram_supported_property_link(normalized):
        base = "Saved. PropertyQuarry is processing this listing now."
        if str(render_priority or "").strip().lower() == "paid":
            return f"{base} Your render request is in the paid-priority lane."
        return f"{base} Free-tier requests are processed after paid render requests."
    if "?" in normalized or any(marker in normalized.lower() for marker in ("what", "why", "how", "where", "when", "which")):
        return "Working on it. PropertyQuarry saved your request and is processing it asynchronously."
    return base


def _telegram_send_processing_ack(
    *,
    container: AppContainer,
    principal_id: str,
    bot_config: dict[str, object],
    chat_id: str,
    dedupe_key: str,
    source_text: str,
    current_message_id: str,
    render_priority: str = "",
) -> bool:
    marker = f"{str(dedupe_key or '').strip()}:processing_ack_sent" if str(dedupe_key or '').strip() else ""
    if marker and container.channel_runtime.find_observation_by_dedupe(marker, principal_id=principal_id) is not None:
        return False
    buttons = _telegram_processing_ack_buttons(
        bot_config=bot_config,
        current_message_id=current_message_id,
        chat_id=chat_id,
    )
    receipt = _telegram_send_message(
        bot_token=str(bot_config.get("token") or "").strip(),
        chat_id=chat_id,
        text=_telegram_processing_ack_text(source_text, render_priority=render_priority),
        inline_buttons=buttons,
    )
    if not bool(receipt.get("ok")):
        return False
    container.channel_runtime.ingest_observation(
        principal_id=principal_id,
        channel="telegram",
        event_type="telegram.processing_ack_sent",
        payload={
            "chat_id": chat_id,
            "reply_text": _telegram_processing_ack_text(source_text, render_priority=render_priority),
            "source_text": source_text,
            "buttons": _telegram_processing_ack_buttons_payload(
                bot_config=bot_config,
                current_message_id=current_message_id,
                chat_id=chat_id,
            ),
            "message_id": str(dict(receipt.get("result") or {}).get("message_id") or "").strip(),
            "current_message_id": str(current_message_id or "").strip(),
        },
        source_id=f"telegram:{chat_id}" if chat_id else "telegram",
        external_id=str(dict(receipt.get("result") or {}).get("message_id") or "").strip(),
        dedupe_key=marker,
    )
    return True


def _telegram_schedule_async_assistant_reply(
    *,
    container: AppContainer,
    principal_id: str,
    bot_config: dict[str, object],
    chat_id: str,
    dedupe_key: str,
    text: str,
    current_message_id: str,
    retry_budget: int = 2,
    async_payload: dict[str, object] | None = None,
) -> None:
    if not chat_id or not dedupe_key:
        return
    if _telegram_async_already_started(container, principal_id=principal_id, dedupe_key=dedupe_key):
        return
    property_url = _telegram_supported_property_link(text)
    render_priority = _telegram_property_render_priority(container=container, principal_id=principal_id) if property_url else "free"
    render_executor = _TELEGRAM_PAID_RENDER_EXECUTOR if render_priority == "paid" else _TELEGRAM_FREE_RENDER_EXECUTOR
    _record_telegram_async_started(
        container,
        principal_id=principal_id,
        chat_id=chat_id,
        dedupe_key=dedupe_key,
        prompt_text=text,
        current_message_id=current_message_id,
        bot_key=str(bot_config.get("bot_key") or "").strip(),
        bot_handle=str(bot_config.get("handle") or "").strip(),
    )
    if _telegram_inline_async_accelerator_enabled():
        if not property_url:
            render_executor = _TELEGRAM_ASYNC_EXECUTOR
        render_executor.submit(
            _telegram_async_assistant_reply_worker,
            container=container,
            principal_id=principal_id,
            bot_config=dict(bot_config),
            chat_id=chat_id,
            text=text,
            current_message_id=current_message_id,
            retry_budget=retry_budget,
            async_payload=dict(async_payload or {}),
        )


def _telegram_command_reply_text(
    *,
    container: AppContainer,
    principal_id: str,
    text: str,
    payload: dict[str, object] | None = None,
    bot_handle: str,
    preferred_onemin_labels: tuple[str, ...] = (),
    current_message_id: str = "",
    chat_id: str = "",
) -> tuple[str, bool, int, bool]:
    fallback_retry_budget = TelegramTurnDecision().retry_budget
    ctx = _telegram_turn_context(
        container=container,
        principal_id=principal_id,
        text=text,
        payload=payload,
        bot_handle=bot_handle,
        preferred_onemin_labels=preferred_onemin_labels,
        current_message_id=current_message_id,
        chat_id=chat_id,
    )
    command_decision = _telegram_command_turn_decision(ctx)
    if command_decision.reply_text or command_decision.schedule_async:
        return (
            command_decision.reply_text,
            command_decision.schedule_async,
            command_decision.retry_budget,
            bool(command_decision.suppress_async_ack),
        )
    scout_update_decision = _telegram_scout_update_turn_decision(ctx)
    if scout_update_decision.reply_text or scout_update_decision.schedule_async:
        return (
            scout_update_decision.reply_text,
            scout_update_decision.schedule_async,
            scout_update_decision.retry_budget,
            bool(scout_update_decision.suppress_async_ack),
        )
    link_decision = _telegram_link_turn_decision(ctx)
    if link_decision.reply_text or link_decision.schedule_async:
        return (
            link_decision.reply_text,
            link_decision.schedule_async,
            link_decision.retry_budget,
            bool(link_decision.suppress_async_ack),
        )
    photo_reply = _telegram_photo_reply_text(ctx.payload)
    if photo_reply:
        return photo_reply, False, fallback_retry_budget, False
    if ctx.normalized:
        probe_reply = _telegram_probe_reply_text(ctx.normalized)
        if probe_reply:
            return probe_reply, False, fallback_retry_budget, False
        if _telegram_local_tool_priority(ctx):
            local_decision = _telegram_local_turn_decision(ctx)
            if local_decision.reply_text or local_decision.schedule_async:
                return local_decision.reply_text, local_decision.schedule_async, local_decision.retry_budget, bool(local_decision.suppress_async_ack)
        math_reply = _safe_math_answer(ctx.normalized)
        if math_reply:
            return math_reply, False, fallback_retry_budget, False
        if _telegram_force_async_path(ctx):
            if _telegram_similar_async_prompt_pending(
                container,
                principal_id=principal_id,
                chat_id=ctx.chat_id,
                text=ctx.normalized,
            ):
                return "", False, fallback_retry_budget, False
            return "", True, fallback_retry_budget, False
        general_reply = _telegram_general_reply_text(container=container, principal_id=principal_id, text=ctx.normalized)
        if (
            ctx.is_completion_cue
            and ctx.chat_id
            and _telegram_is_google_photos_picker_block_reply(general_reply)
            and _telegram_recent_messages_include_google_photos_context(
                container,
                principal_id=principal_id,
            )
            and _telegram_same_reply_recently_sent(
                container,
                principal_id=principal_id,
                chat_id=ctx.chat_id,
                reply_text=general_reply,
                )
            ):
                return "", False, fallback_retry_budget, False
        if general_reply and not general_reply.startswith("I got it. I saved this in Tibor's assistant flow"):
            return general_reply, False, fallback_retry_budget, False
        if _telegram_prefers_async_codex_chat(ctx.normalized):
            if _telegram_low_signal_followup_cue(ctx.normalized):
                sync_timeout = 0.0
                try:
                    sync_timeout = max(
                        float(str(os.getenv("EA_TELEGRAM_SYNC_REAL_REPLY_TIMEOUT_SECONDS") or "6").strip() or "6"),
                        1.0,
                    )
                except Exception:
                    sync_timeout = 6.0
                real_reply = _telegram_real_ea_reply_text(
                    container=container,
                    principal_id=principal_id,
                    text=ctx.normalized,
                    current_message_id=current_message_id,
                    preferred_onemin_labels=preferred_onemin_labels,
                    timeout_seconds=sync_timeout,
                ).strip()
                if real_reply:
                    return real_reply, False, fallback_retry_budget, False
            if _telegram_similar_async_prompt_pending(
                container,
                principal_id=principal_id,
                chat_id=ctx.chat_id,
                text=ctx.normalized,
            ):
                return "", False, fallback_retry_budget, False
            return "", True, fallback_retry_budget, False
        local_decision = _telegram_local_turn_decision(ctx)
        if local_decision.reply_text or local_decision.schedule_async:
            return (
                local_decision.reply_text,
                local_decision.schedule_async,
                local_decision.retry_budget,
                bool(local_decision.suppress_async_ack),
            )
        sync_timeout = 0.0
        try:
            sync_timeout = max(
                float(str(os.getenv("EA_TELEGRAM_SYNC_REAL_REPLY_TIMEOUT_SECONDS") or "6").strip() or "6"),
                1.0,
            )
        except Exception:
            sync_timeout = 6.0
        real_reply = _telegram_real_ea_reply_text(
            container=container,
            principal_id=principal_id,
            text=ctx.normalized,
            current_message_id=current_message_id,
            preferred_onemin_labels=preferred_onemin_labels,
            timeout_seconds=sync_timeout,
        ).strip()
        if real_reply:
            return real_reply, False, fallback_retry_budget, False
        if _telegram_should_async_assistant_reply(ctx.normalized):
            if _telegram_similar_async_prompt_pending(
                container,
                principal_id=principal_id,
                chat_id=ctx.chat_id,
                text=ctx.normalized,
            ):
                return "", False, fallback_retry_budget, False
            return "", True, fallback_retry_budget, False
        return general_reply, False, fallback_retry_budget, False
    return "", False, fallback_retry_budget, False


def _telegram_session_turn(
    *,
    container: AppContainer,
    principal_id: str,
    text: str,
    payload: dict[str, object] | None = None,
    bot_handle: str,
    preferred_onemin_labels: tuple[str, ...] = (),
    current_message_id: str = "",
    chat_id: str = "",
) -> TelegramTurnDecision:
    initial_ctx = build_turn_context(
        container=container,
        principal_id=principal_id,
        text=text,
        payload=dict(payload or {}),
        bot_handle=bot_handle,
        preferred_onemin_labels=preferred_onemin_labels,
        current_message_id=current_message_id,
        chat_id=chat_id,
        completion_cue_predicate=_telegram_low_signal_followup_cue,
    )
    pdf_decision = _telegram_property_pdf_turn_decision(initial_ctx)
    if pdf_decision.reply_text or pdf_decision.schedule_async:
        return pdf_decision
    reply_text, schedule_async, retry_budget, suppress_async_ack = _telegram_command_reply_text(
        container=container,
        principal_id=principal_id,
        text=text,
        payload=payload,
        bot_handle=bot_handle,
        preferred_onemin_labels=preferred_onemin_labels,
        current_message_id=current_message_id,
        chat_id=chat_id,
    )
    ctx = build_turn_context(
        container=container,
        principal_id=principal_id,
        text=text,
        payload=dict(payload or {}),
        bot_handle=bot_handle,
        preferred_onemin_labels=preferred_onemin_labels,
        current_message_id=current_message_id,
        chat_id=chat_id,
        completion_cue_predicate=_telegram_low_signal_followup_cue,
    )
    callback_decision = _telegram_callback_turn_decision(ctx)
    if callback_decision.reply_text or callback_decision.schedule_async:
        return callback_decision
    feedback_followup_decision = _telegram_notification_feedback_followup_turn_decision(ctx)
    if feedback_followup_decision.reply_text or feedback_followup_decision.schedule_async:
        return feedback_followup_decision
    return TelegramTurnDecision(
        reply_text=reply_text,
        schedule_async=schedule_async,
        retry_budget=retry_budget,
        suppress_async_ack=suppress_async_ack,
    )


class TelegramIngestOut(BaseModel):
    observation_id: str
    principal_id: str
    channel: str
    event_type: str
    created_at: str
    reply_sent: bool = False
    reply_text: str = ""


@router.post("/telegram/ingest/{bot_key}")
@router.post("/telegram/ingest")
def ingest_telegram(
    request: Request,
    body: dict[str, object] = Body(default_factory=dict),
    bot_key: str = "",
    container: AppContainer = Depends(get_container),
) -> TelegramIngestOut:
    payload = dict(body or {})
    update = dict(payload.get("update") or {}) if isinstance(payload.get("update"), dict) else payload
    header_secret = str(request.headers.get("x-telegram-bot-api-secret-token") or "")
    provided_secret = str(update.get("secret_token") or "")
    bot_config = _resolve_telegram_bot_config(bot_key=bot_key, provided_secret=provided_secret, header_secret=header_secret)
    _require_telegram_ingest_secret(
        config=bot_config,
        provided=provided_secret,
        header_value=header_secret,
    )
    fields = _telegram.to_observation_fields(update)
    chat_id = str(fields.get("chat_id") or "").strip()
    dedupe_key = str(fields.get("dedupe_key") or "")
    principal_id = _resolve_telegram_principal(
        container,
        chat_id,
        bot_key=str(bot_config.get("bot_key") or "").strip(),
        bot_handle=str(bot_config.get("handle") or "").strip(),
    )
    resolved_from_binding = bool(principal_id)
    if not principal_id:
        principal_id = _auto_bind_telegram_chat(container, chat_id, config=bot_config)
    if not principal_id:
        raise HTTPException(status_code=404, detail="telegram_binding_not_found")
    if not resolved_from_binding and not _telegram_principal_is_registered_user(
        container=container,
        principal_id=principal_id,
        accepted_default_principal_id=str(bot_config.get("default_principal_id") or "").strip(),
    ):
        raise HTTPException(status_code=403, detail="telegram_principal_not_registered")
    existing_event = (
        container.channel_runtime.find_observation_by_dedupe(dedupe_key, principal_id=principal_id) if dedupe_key else None
    )
    if existing_event is not None:
        message_payload = dict(getattr(existing_event, "payload", {}) or {})
    else:
        message_payload = resolve_telegram_message_payload(
            payload=dict(fields.get("payload") or {}),
            bot_token=str(bot_config.get("token") or "").strip(),
        )
        if message_payload:
            fields["payload"] = message_payload
    event = existing_event or container.channel_runtime.ingest_observation(
        principal_id=principal_id,
        channel=_telegram.channel,
        event_type=str(fields.get("event_type") or "telegram.update"),
        payload=message_payload,
        source_id=str(fields.get("source_id") or ""),
        external_id=str(fields.get("external_id") or ""),
        dedupe_key=dedupe_key,
    )
    decision = _telegram_session_turn(
        container=container,
        principal_id=principal_id,
        text=str(message_payload.get("text") or ""),
        payload={**message_payload, "_bot_config": dict(bot_config)},
        bot_handle=str(bot_config.get("handle") or "").strip(),
        preferred_onemin_labels=tuple(
            str(item or "").strip()
            for item in list(bot_config.get("preferred_onemin_labels") or ())
            if str(item or "").strip()
        ),
        current_message_id=str(message_payload.get("message_id") or ""),
        chat_id=chat_id,
    )
    if str(message_payload.get("kind") or "").strip().lower() == "callback_query":
        try:
            _telegram_answer_callback_query(
                bot_token=str(bot_config.get("token") or "").strip(),
                callback_query_id=str(message_payload.get("callback_query_id") or ""),
                text="Received",
            )
        except Exception:
            pass
    reply_text = decision.reply_text
    schedule_async = decision.schedule_async
    async_text = str(decision.async_text or "").strip() or str(message_payload.get("text") or "")
    async_message_id = str(decision.async_message_id or "").strip() or str(message_payload.get("message_id") or "")
    async_payload = dict(decision.async_payload or {})
    reply_sent = False
    if reply_text and chat_id and not _telegram_reply_already_sent(container, principal_id=principal_id, dedupe_key=dedupe_key):
        try:
            reply_sent = _telegram_send_and_record_reply(
                container=container,
                principal_id=principal_id,
                bot_config=bot_config,
                chat_id=chat_id,
                dedupe_key=dedupe_key,
                reply_text=reply_text,
                source_text=str(message_payload.get("text") or ""),
            )
        except Exception:
            reply_sent = False
    if schedule_async and chat_id:
        async_retry_budget = int(decision.retry_budget)
        async_text_priority = (
            _telegram_property_render_priority(container=container, principal_id=principal_id)
            if _telegram_supported_property_link(str(message_payload.get("text") or "")) else "free"
        )
        if not decision.suppress_async_ack:
            try:
                _telegram_send_processing_ack(
                    container=container,
                    principal_id=principal_id,
                    bot_config=bot_config,
                    chat_id=chat_id,
                    dedupe_key=dedupe_key,
                    source_text=async_text,
                    current_message_id=async_message_id,
                    render_priority=async_text_priority,
                )
            except Exception:
                pass
        _telegram_schedule_async_assistant_reply(
            container=container,
            principal_id=principal_id,
            bot_config=bot_config,
            chat_id=chat_id,
            dedupe_key=dedupe_key,
            text=async_text,
            current_message_id=async_message_id,
            retry_budget=async_retry_budget,
            async_payload=async_payload,
        )
    return TelegramIngestOut(
        observation_id=event.observation_id,
        principal_id=event.principal_id,
        channel=event.channel,
        event_type=event.event_type,
        created_at=event.created_at,
        reply_sent=reply_sent,
        reply_text=reply_text,
    )
