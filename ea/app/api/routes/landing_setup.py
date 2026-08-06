from __future__ import annotations

import os
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

from app.api.dependencies import CloudflareAccessIdentity, get_cloudflare_access_identity, get_container
from app.api.routes.landing import (
    PUBLIC_NAV,
    _browser_form_context,
    _channel_cards,
    _form_value,
    _form_values,
    _humanize,
    _normalize_browser_return_to,
    _render_public_template,
    _workspace_plan,
)
from app.container import AppContainer
from app.product.service import build_product_service
from app.services.facebook_oauth import (
    complete_facebook_oauth_callback,
    read_facebook_oauth_state,
    read_facebook_oauth_state_unchecked,
)
from app.services.google_oauth import (
    complete_google_oauth_callback,
    google_bundle_supports_workspace_sync,
    read_google_oauth_state,
    read_google_oauth_state_unchecked,
)
from app.services import propertyquarry_google_identity
from app.services.id_austria_oidc import (
    complete_id_austria_oidc_callback,
    read_id_austria_oidc_state,
    read_id_austria_oidc_state_unchecked,
)

router = APIRouter(tags=["landing"])


def _propertyquarry_public_base_url() -> str:
    return str(os.environ.get("PROPERTYQUARRY_PUBLIC_BASE_URL") or "https://propertyquarry.com").strip().rstrip("/")


def _public_app_base_url(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-host") or "").strip().lower().rstrip(".")
    request_host = str(request.url.hostname or "").strip().lower().rstrip(".")
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip() or request.url.scheme
    effective_host = forwarded or request_host
    if effective_host in {"propertyquarry.com", "www.propertyquarry.com"}:
        if forwarded:
            return f"{forwarded_proto}://{forwarded}"
        return str(request.base_url).rstrip("/")
    explicit = str(os.environ.get("EA_PUBLIC_APP_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit
    redirect_uri = str(os.environ.get("EA_GOOGLE_OAUTH_REDIRECT_URI") or "").strip()
    if redirect_uri:
        parsed = urllib.parse.urlparse(redirect_uri)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    if forwarded:
        return f"{forwarded_proto}://{forwarded}"
    return str(request.base_url).rstrip("/")


def _append_query_value(path: str, **values: str) -> str:
    parsed = urllib.parse.urlparse(path)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    for key, value in values.items():
        normalized = str(value or "").strip()
        if normalized:
            query.append((key, normalized))
    updated = urllib.parse.urlencode(query)
    return urllib.parse.urlunparse(parsed._replace(query=updated))


def _google_post_connect_sync(
    *,
    container: AppContainer,
    principal_id: str,
    actor: str,
    granted_scopes: tuple[str, ...] | list[str] = (),
) -> dict[str, object]:
    if not google_bundle_supports_workspace_sync(scopes=tuple(granted_scopes)):
        return {"status": "identity_only"}
    product = build_product_service(container)
    try:
        result = product.sync_google_workspace_signals(
            principal_id=principal_id,
            actor=actor,
            email_limit=5,
            calendar_limit=5,
        )
    except Exception as exc:
        return {"status": "failed", "error": str(exc or "google_sync_failed")}
    return {
        "status": "completed",
        "processed_total": int(result.get("total") or 0),
        "synced_total": int(result.get("synced_total") or 0),
        "deduplicated_total": int(result.get("deduplicated_total") or 0),
        "suppressed_total": int(result.get("suppressed_total") or 0),
    }


def _google_stale_redirect_retry(state_payload: dict[str, object]) -> RedirectResponse | None:
    browser_source = str(state_payload.get("browser_source") or "").strip()
    if browser_source == "sign_in":
        return_to = _normalize_browser_return_to(
            str(state_payload.get("return_to") or ""),
            default="/app/search",
        )
        return RedirectResponse(
            "/sign-in/google?" + urllib.parse.urlencode(
                {"restart": "stale_redirect", "return_to": return_to}
            ),
            status_code=303,
        )
    if browser_source == "settings_google":
        return_to = _normalize_browser_return_to(str(state_payload.get("return_to") or ""), default="/app/settings/google")
        return RedirectResponse(
            "/app/actions/google/connect?" + urllib.parse.urlencode({"return_to": return_to}),
            status_code=303,
        )
    return None


def _google_sync_detail(sync_result: dict[str, object]) -> str:
    status = str(sync_result.get("status") or "").strip().lower()
    if status == "identity_only":
        return "Google account linking is complete. No Gmail or Calendar sync was requested."
    if status != "completed":
        error = str(sync_result.get("error") or "").strip()
        return f"Google is connected, but the first signal sync needs attention: {error or 'google_sync_failed'}."
    processed_total = int(sync_result.get("processed_total") or 0)
    synced_total = int(sync_result.get("synced_total") or 0)
    deduplicated_total = int(sync_result.get("deduplicated_total") or 0)
    suppressed_total = int(sync_result.get("suppressed_total") or 0)
    if processed_total or suppressed_total:
        return (
            f"First signal sync finished. Processed {processed_total} item"
            f"{'' if processed_total == 1 else 's'}, staged {synced_total}, "
            f"deduplicated {deduplicated_total}, suppressed {suppressed_total}."
        )
    return "First signal sync finished. No recent Gmail or Calendar signals were staged yet."


def _render_google_oauth_callback_failure(
    request: Request,
    *,
    detail: str,
    status_code: int,
) -> HTMLResponse:
    response = _render_public_template(
        request,
        "channel_detail.html",
        page_title="Google connection needs attention",
        public_nav=PUBLIC_NAV,
        current_nav="integrations",
        access_identity=None,
        principal_id="",
        channel_title="Google connection",
        channel_eyebrow="Google",
        channel={
            "status": "needs_attention",
            "detail": detail or "Google connection could not be completed on this host.",
            "capabilities": [],
            "limitations": [],
        },
        detail_points=(
            "The OAuth callback did not complete cleanly.",
            "Retry the consent flow from the latest setup page if this was an expired or cancelled sign-in.",
        ),
        body_points=(
            "PropertyQuarry keeps the browser callback fail-closed and should show the real blocker instead of a blank gateway error.",
            "If this persists, check the Google OAuth credentials, redirect URI, and provider availability for propertyquarry.com.",
        ),
    )
    response.status_code = status_code
    return response


def _normalize_google_sign_in_error(error: str) -> str:
    normalized = str(error or "").strip()
    if not normalized:
        return "google_oauth_callback_failed"
    lower = normalized.lower()
    if "identity-only" in lower or "identity only" in lower:
        return "google_identity_only"
    return normalized


def _google_sign_in_error_redirect(
    *,
    error: str,
    google_email: str = "",
    return_to: str = "/app/search",
) -> RedirectResponse:
    query = {
        "google_error": _normalize_google_sign_in_error(error),
        "return_to": _normalize_browser_return_to(return_to, default="/app/search"),
    }
    normalized_email = str(google_email or "").strip()
    if normalized_email:
        query["google_prefill_email"] = normalized_email
    return RedirectResponse("/sign-in?" + urllib.parse.urlencode(query), status_code=303)


def _render_facebook_oauth_callback_failure(
    request: Request,
    *,
    detail: str,
    status_code: int,
) -> HTMLResponse:
    response = _render_public_template(
        request,
        "channel_detail.html",
        page_title="Facebook connection needs attention",
        public_nav=PUBLIC_NAV,
        current_nav="integrations",
        access_identity=None,
        principal_id="",
        channel_title="Facebook connection",
        channel_eyebrow="Facebook",
        channel={
            "status": "needs_attention",
            "detail": detail or "Facebook connection could not be completed on this host.",
            "capabilities": [],
            "limitations": [],
        },
        detail_points=(
            "The OAuth callback did not complete cleanly.",
            "Retry the Facebook sign-in flow from the latest sign-in page if this was an expired or cancelled sign-in.",
        ),
        body_points=(
            "PropertyQuarry keeps the browser callback fail-closed and should show the real blocker instead of a blank gateway error.",
            "If this persists, check the Facebook Login credentials, redirect URI, and provider availability for propertyquarry.com.",
        ),
    )
    response.status_code = status_code
    return response


@router.post("/setup/start")
async def setup_start(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RedirectResponse:
    form_data = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    principal_id = _browser_form_context(form_data=form_data, container=container, access_identity=access_identity)
    container.onboarding.start_workspace(
        principal_id=principal_id,
        workspace_name=_form_value(form_data, "workspace_name", "PropertyQuarry account"),
        workspace_mode=_form_value(form_data, "workspace_mode", "personal"),
        region=_form_value(form_data, "region", ""),
        language=_form_value(form_data, "language", ""),
        timezone=_form_value(form_data, "timezone", ""),
        selected_channels=_form_values(form_data, "selected_channels"),
    )
    return RedirectResponse("/register", status_code=303)


@router.post("/setup/telegram")
async def setup_telegram(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RedirectResponse:
    form_data = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    principal_id = _browser_form_context(form_data=form_data, container=container, access_identity=access_identity)
    if not _workspace_plan(container, principal_id=principal_id).entitlements.messaging_channels_enabled:
        return RedirectResponse("/pricing", status_code=303)
    container.onboarding.start_telegram(
        principal_id=principal_id,
        telegram_ref=_form_value(form_data, "telegram_ref", ""),
        identity_mode=_form_value(form_data, "identity_mode", "login_widget"),
        history_mode=_form_value(form_data, "history_mode", "future_only"),
        assistant_surfaces=_form_values(form_data, "assistant_surfaces"),
    )
    return RedirectResponse("/register", status_code=303)


@router.post("/setup/telegram/link-bot")
async def setup_telegram_link_bot(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RedirectResponse:
    form_data = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    principal_id = _browser_form_context(form_data=form_data, container=container, access_identity=access_identity)
    if not _workspace_plan(container, principal_id=principal_id).entitlements.messaging_channels_enabled:
        return RedirectResponse("/pricing", status_code=303)
    container.onboarding.link_telegram_bot(
        principal_id=principal_id,
        bot_handle=_form_value(form_data, "bot_handle", ""),
        install_surfaces=_form_values(form_data, "install_surfaces"),
        default_chat_ref=_form_value(form_data, "default_chat_ref", ""),
    )
    return RedirectResponse("/register", status_code=303)


@router.post("/setup/whatsapp/business")
async def setup_whatsapp_business(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RedirectResponse:
    form_data = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    principal_id = _browser_form_context(form_data=form_data, container=container, access_identity=access_identity)
    if not _workspace_plan(container, principal_id=principal_id).entitlements.messaging_channels_enabled:
        return RedirectResponse("/pricing", status_code=303)
    container.onboarding.start_whatsapp_business(
        principal_id=principal_id,
        phone_number=_form_value(form_data, "phone_number", ""),
        business_name=_form_value(form_data, "business_name", ""),
        import_history_now=_form_value(form_data, "import_history_now", "").lower() == "true",
    )
    return RedirectResponse("/register", status_code=303)


@router.post("/setup/whatsapp/export")
async def setup_whatsapp_export(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RedirectResponse:
    form_data = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    principal_id = _browser_form_context(form_data=form_data, container=container, access_identity=access_identity)
    if not _workspace_plan(container, principal_id=principal_id).entitlements.messaging_channels_enabled:
        return RedirectResponse("/pricing", status_code=303)
    chats = tuple(chunk.strip() for chunk in _form_value(form_data, "selected_chat_labels_csv", "").split(",") if chunk.strip())
    container.onboarding.import_whatsapp_export(
        principal_id=principal_id,
        export_label=_form_value(form_data, "export_label", ""),
        selected_chat_labels=chats,
        include_media=_form_value(form_data, "include_media", "").lower() == "true",
    )
    return RedirectResponse("/register", status_code=303)


@router.post("/setup/finalize")
async def setup_finalize(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RedirectResponse:
    form_data = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    principal_id = _browser_form_context(form_data=form_data, container=container, access_identity=access_identity)
    container.onboarding.finalize(
        principal_id=principal_id,
        retention_mode=_form_value(form_data, "retention_mode", "full_bodies"),
        metadata_only_channels=_form_values(form_data, "metadata_only_channels"),
        allow_drafts=_form_value(form_data, "allow_drafts", "").lower() == "true",
        allow_action_suggestions=_form_value(form_data, "allow_action_suggestions", "").lower() == "true",
        allow_auto_briefs=_form_value(form_data, "allow_auto_briefs", "").lower() == "true",
        auto_brief_cadence=_form_value(form_data, "auto_brief_cadence", "daily_morning"),
        auto_brief_delivery_time_local=_form_value(form_data, "auto_brief_delivery_time_local", "08:00"),
        auto_brief_quiet_hours_start=_form_value(form_data, "auto_brief_quiet_hours_start", "20:00"),
        auto_brief_quiet_hours_end=_form_value(form_data, "auto_brief_quiet_hours_end", "07:00"),
        auto_brief_recipient_email=_form_value(form_data, "auto_brief_recipient_email", ""),
        auto_brief_delivery_channel=_form_value(form_data, "auto_brief_delivery_channel", "email"),
    )
    return RedirectResponse("/app/properties", status_code=303)


@router.post("/google/connect", response_model=None)
async def google_connect_browser(
    request: Request,
    container: AppContainer = Depends(get_container),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> RedirectResponse | HTMLResponse:
    form_data = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    principal_id = _browser_form_context(form_data=form_data, container=container, access_identity=access_identity)
    return_to = _normalize_browser_return_to(
        _form_value(form_data, "return_to", "/sign-in?signing_in=1"),
        default="/sign-in?signing_in=1",
    )
    result = container.onboarding.start_google(
        principal_id=principal_id,
        scope_bundle=_form_value(form_data, "scope_bundle", "identity"),
        redirect_uri_override=f"{_public_app_base_url(request)}/google/callback",
        return_to=return_to,
        browser_source="public_setup",
    )
    google_start = dict(result.get("google_start") or {})
    if bool(google_start.get("ready")) and str(google_start.get("auth_url") or "").strip():
        return RedirectResponse(str(google_start["auth_url"]), status_code=303)
    return _render_public_template(
        request,
        "channel_detail.html",
        page_title="Google onboarding status",
        public_nav=PUBLIC_NAV,
        current_nav="integrations",
        access_identity=access_identity,
        principal_id=principal_id,
        status=result,
        workspace=dict(result.get("workspace") or {}),
        channels=dict(result.get("channels") or {}),
        channel_cards=_channel_cards(dict(result.get("channels") or {})),
        selected_channels_label=", ".join(result.get("selected_channels") or []) or "Google sign-in recommended",
        workspace_mode_label=_humanize(str(dict(result.get("workspace") or {}).get("mode") or "personal")),
        brief_headline=str(dict(result.get("brief_preview") or {}).get("headline") or "Turn your channels into a prioritized day."),
        first_brief_items=[],
        suggested_actions=[],
        trust_notes=[],
        top_contacts=[],
        top_themes=[],
        channel_title="Google onboarding",
        channel_eyebrow="Google",
        channel={"status": google_start.get("detail") or "not_ready", "detail": google_start.get("detail") or "Google onboarding could not start.", "capabilities": [], "limitations": []},
        detail_points=("Google consent could not start on this host.",),
        body_points=("Check OAuth credentials, redirect URI, and provider configuration.",),
    )


@router.get("/facebook/callback", response_class=HTMLResponse, response_model=None, name="facebook_oauth_browser_callback")
def facebook_oauth_browser_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    container: AppContainer = Depends(get_container),
) -> HTMLResponse | RedirectResponse:
    state_payload: dict[str, object] = {}
    if str(error or "").strip():
        detail = str(error_description or error or "facebook_oauth_denied").strip()
        browser_source = ""
        if str(state or "").strip():
            try:
                state_payload = read_facebook_oauth_state(state)
                browser_source = str(state_payload.get("browser_source") or "").strip()
            except Exception:
                pass
        if browser_source == "sign_in":
            return_to = _normalize_browser_return_to(
                str(state_payload.get("return_to") or ""),
                default="/app/search",
            )
            return RedirectResponse(
                "/sign-in?" + urllib.parse.urlencode(
                    {"facebook_error": detail, "return_to": return_to}
                ),
                status_code=303,
            )
        return _render_facebook_oauth_callback_failure(request, detail=detail, status_code=400)
    if not str(code or "").strip() or not str(state or "").strip():
        return _render_facebook_oauth_callback_failure(
            request,
            detail="Facebook did not return a valid OAuth code and state.",
            status_code=400,
        )
    try:
        state_payload = read_facebook_oauth_state(state)
        account = complete_facebook_oauth_callback(container=container, code=code, state=state)
    except RuntimeError as exc:
        detail = str(exc or "facebook_oauth_callback_failed")
        if detail == "facebook_oauth_state_expired" and str(state or "").strip():
            try:
                expired_state = read_facebook_oauth_state_unchecked(state)
            except Exception:
                expired_state = {}
            return_to = _normalize_browser_return_to(str(expired_state.get("return_to") or ""), default="")
            if return_to:
                if str(expired_state.get("browser_source") or "").strip() == "sign_in":
                    return RedirectResponse(
                        "/sign-in?" + urllib.parse.urlencode(
                            {
                                "facebook_error": "facebook_oauth_state_expired",
                                "return_to": return_to,
                            }
                        ),
                        status_code=303,
                    )
                separator = "&" if "?" in return_to else "?"
                return RedirectResponse(
                    f"{return_to}{separator}facebook_error=facebook_oauth_state_expired",
                    status_code=303,
                )
        if str(state_payload.get("browser_source") or "").strip() == "sign_in":
            return_to = _normalize_browser_return_to(
                str(state_payload.get("return_to") or ""),
                default="/app/search",
            )
            return RedirectResponse(
                "/sign-in?" + urllib.parse.urlencode(
                    {"facebook_error": detail, "return_to": return_to}
                ),
                status_code=303,
            )
        return _render_facebook_oauth_callback_failure(request, detail=detail, status_code=400)
    except Exception as exc:
        detail = str(exc or "facebook_oauth_callback_failed")
        if str(state_payload.get("browser_source") or "").strip() == "sign_in":
            return_to = _normalize_browser_return_to(
                str(state_payload.get("return_to") or ""),
                default="/app/search",
            )
            return RedirectResponse(
                "/sign-in?" + urllib.parse.urlencode(
                    {"facebook_error": detail, "return_to": return_to}
                ),
                status_code=303,
            )
        return _render_facebook_oauth_callback_failure(request, detail=detail, status_code=502)
    product = build_product_service(container)
    product.record_surface_event(
        principal_id=account.binding.principal_id,
        event_type="facebook_account_connected",
        surface="facebook_oauth_browser_callback",
        actor=str(account.facebook_email or account.facebook_subject or account.binding.principal_id or "facebook_oauth").strip(),
        metadata={
            "binding_id": str(account.binding.binding_id or "").strip(),
            "facebook_email": str(account.facebook_email or "").strip(),
            "facebook_subject": str(account.facebook_subject or "").strip(),
        },
    )
    browser_source = str(state_payload.get("browser_source") or "").strip()
    return_to = _normalize_browser_return_to(
        str(state_payload.get("return_to") or ""),
        default="/app/search" if browser_source == "sign_in" else "",
    )
    if browser_source == "sign_in":
        onboarding_status = container.onboarding.status(principal_id=account.binding.principal_id)
        workspace_name = str(dict(onboarding_status.get("workspace") or {}).get("name") or "").strip() or str(
            account.facebook_name or account.facebook_email or account.binding.principal_id or "PropertyQuarry"
        ).strip()
        access = product.issue_workspace_access_session(
            principal_id=account.binding.principal_id,
            email=account.facebook_email,
            role="principal",
            display_name=workspace_name,
            source_kind="facebook_sign_in",
            default_target=return_to,
        )
        return RedirectResponse(str(access.get("access_url") or "/app/search"), status_code=303)
    if return_to:
        separator = "&" if "?" in return_to else "?"
        return RedirectResponse(f"{return_to}{separator}facebook_status=connected", status_code=303)
    return _render_public_template(
        request,
        "channel_detail.html",
        page_title="Facebook connected",
        public_nav=PUBLIC_NAV,
        current_nav="integrations",
        access_identity=None,
        principal_id=account.binding.principal_id,
        channel_title="Facebook connected",
        channel_eyebrow="Facebook",
        channel={
            "status": "connected",
            "detail": "Facebook Login is connected for identity-only return access.",
            "capabilities": ["Sign in with Facebook identity", "Return to the same PropertyQuarry workspace"],
            "limitations": ["No feed, page, or WhatsApp permissions are requested by this sign-in path"],
        },
        detail_points=(
            f"Connected account: {account.facebook_email or account.facebook_name or account.facebook_subject}",
            f"Return path: {return_to or '/sign-in'}",
        ),
        body_points=(
            "You can close this page or return to PropertyQuarry.",
            "Use settings later if broader Meta or WhatsApp Business permissions are added as separate explicit lanes.",
        ),
    )


def _complete_propertyquarry_google_identity_sign_in(
    *,
    request: Request,
    code: str,
    state: str,
    error: str,
    container: AppContainer,
) -> RedirectResponse:
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    secure_cookie = (
        request.url.scheme == "https"
        or forwarded_proto == "https"
        or str(request.url.hostname or "").strip().lower().rstrip(".")
        in {"propertyquarry.com", "www.propertyquarry.com"}
    )

    def finish(response: RedirectResponse) -> RedirectResponse:
        response.delete_cookie(
            propertyquarry_google_identity.GOOGLE_IDENTITY_FLOW_COOKIE_NAME,
            path="/google/callback",
            secure=secure_cookie,
            httponly=True,
            samesite="lax",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
        return response

    flow_nonce = str(
        request.cookies.get(propertyquarry_google_identity.GOOGLE_IDENTITY_FLOW_COOKIE_NAME)
        or ""
    ).strip()
    return_to = "/app/search"
    try:
        state_payload = propertyquarry_google_identity.read_propertyquarry_google_identity_state(
            state,
            flow_nonce=flow_nonce,
            require_flow_nonce=True,
        )
        return_to = _normalize_browser_return_to(
            str(state_payload.get("return_to") or ""),
            default="/app/search",
        )
    except RuntimeError as exc:
        return finish(_google_sign_in_error_redirect(error=str(exc), return_to=return_to))
    if not propertyquarry_google_identity.propertyquarry_identity_host_allowed(request.url.hostname):
        return finish(_google_sign_in_error_redirect(
            error="google_oauth_propertyquarry_host_required",
            return_to=return_to,
        ))
    if str(error or "").strip():
        try:
            propertyquarry_google_identity.consume_propertyquarry_google_identity_state(
                state=state,
                flow_nonce=flow_nonce,
                database_url=str(getattr(container.settings, "database_url", "") or "").strip(),
            )
        except RuntimeError as exc:
            return finish(_google_sign_in_error_redirect(error=str(exc), return_to=return_to))
        return finish(_google_sign_in_error_redirect(
            error="google_oauth_propertyquarry_access_denied",
            return_to=return_to,
        ))
    if not str(code or "").strip():
        try:
            propertyquarry_google_identity.consume_propertyquarry_google_identity_state(
                state=state,
                flow_nonce=flow_nonce,
                database_url=str(getattr(container.settings, "database_url", "") or "").strip(),
            )
        except RuntimeError as exc:
            return finish(_google_sign_in_error_redirect(error=str(exc), return_to=return_to))
        return finish(_google_sign_in_error_redirect(
            error="google_oauth_propertyquarry_code_missing",
            return_to=return_to,
        ))
    try:
        identity_session = propertyquarry_google_identity.complete_propertyquarry_google_identity_callback(
            code=code,
            state=state,
            flow_nonce=flow_nonce,
            database_url=str(getattr(container.settings, "database_url", "") or "").strip(),
        )
    except RuntimeError as exc:
        return finish(_google_sign_in_error_redirect(error=str(exc), return_to=return_to))
    except Exception:
        return finish(_google_sign_in_error_redirect(
            error="google_oauth_propertyquarry_sign_in_failed",
            return_to=return_to,
        ))
    if (
        identity_session.return_to
        == propertyquarry_google_identity.MOBILE_IDENTITY_RETURN_TO
    ):
        try:
            handoff = propertyquarry_google_identity.create_propertyquarry_mobile_identity_handoff(
                identity_session=identity_session,
                database_url=str(
                    getattr(container.settings, "database_url", "") or ""
                ).strip(),
            )
        except RuntimeError as exc:
            return finish(
                _google_sign_in_error_redirect(
                    error=str(exc),
                    return_to="/app/search",
                )
            )
        response = RedirectResponse(
            "propertyquarry://auth/callback?"
            + urllib.parse.urlencode({"code": handoff.code}),
            status_code=303,
        )
        return finish(response)
    response = RedirectResponse(identity_session.return_to, status_code=303)
    response.set_cookie(
        propertyquarry_google_identity.GOOGLE_IDENTITY_COOKIE_NAME,
        identity_session.token,
        max_age=identity_session.max_age_seconds,
        path="/",
        secure=secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        "ea_workspace_session",
        path="/",
        secure=secure_cookie,
        httponly=True,
        samesite="lax",
    )
    return finish(response)


@router.post("/mobile/auth/redeem", include_in_schema=False)
async def redeem_propertyquarry_mobile_identity(
    request: Request,
    container: AppContainer = Depends(get_container),
) -> JSONResponse:
    if not propertyquarry_google_identity.propertyquarry_identity_host_allowed(
        request.url.hostname
    ):
        raise HTTPException(status_code=400, detail="propertyquarry_host_required")
    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="mobile_identity_payload_invalid") from exc
    if not isinstance(body, dict) or set(body) != {"code", "pkce_verifier"}:
        raise HTTPException(status_code=400, detail="mobile_identity_payload_invalid")
    code = str(body.get("code") or "")
    pkce_verifier = str(body.get("pkce_verifier") or "")
    if code != code.strip() or pkce_verifier != pkce_verifier.strip():
        raise HTTPException(status_code=400, detail="mobile_identity_payload_invalid")
    try:
        identity_session = propertyquarry_google_identity.redeem_propertyquarry_mobile_identity_handoff(
            code=code,
            pkce_verifier=pkce_verifier,
            database_url=str(
                getattr(container.settings, "database_url", "") or ""
            ).strip(),
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    secure_cookie = (
        request.url.scheme == "https"
        or forwarded_proto == "https"
        or str(request.url.hostname or "").strip().lower().rstrip(".")
        in {"propertyquarry.com", "www.propertyquarry.com"}
    )
    response = JSONResponse(
        {
            "status": "authenticated",
            "return_to": identity_session.return_to,
        }
    )
    response.set_cookie(
        propertyquarry_google_identity.GOOGLE_IDENTITY_COOKIE_NAME,
        identity_session.token,
        max_age=identity_session.max_age_seconds,
        path="/",
        secure=secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        "ea_workspace_session",
        path="/",
        secure=secure_cookie,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


def _propertyquarry_mobile_bridge_document(*, mode: str) -> HTMLResponse:
    label = "Finishing secure sign-in" if mode == "auth" else "Adding property"
    response = HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">"
        "<meta name=\"theme-color\" content=\"#101814\">"
        "<link rel=\"stylesheet\" href=\"/mobile/bridge.css\">"
        f"<title>{label} · PropertyQuarry</title></head>"
        f"<body data-mobile-bridge=\"{mode}\"><main><div class=\"mark\" aria-hidden=\"true\"></div>"
        f"<p class=\"eyebrow\">PropertyQuarry</p><h1>{label}</h1>"
        "<p id=\"status\" role=\"status\">Checking the secure app handoff…</p>"
        "<button id=\"retry\" type=\"button\" hidden>Try again</button>"
        "</main><script src=\"/mobile/bridge.js\" defer></script></body></html>"
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive, nosnippet"
    return response


@router.get("/mobile/auth/bridge", include_in_schema=False)
def propertyquarry_mobile_auth_bridge() -> HTMLResponse:
    return _propertyquarry_mobile_bridge_document(mode="auth")


@router.get("/mobile/share/bridge", include_in_schema=False)
def propertyquarry_mobile_share_bridge() -> HTMLResponse:
    return _propertyquarry_mobile_bridge_document(mode="share")


@router.get("/mobile/bridge.css", include_in_schema=False)
def propertyquarry_mobile_bridge_css() -> PlainTextResponse:
    return PlainTextResponse(
        """
:root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;background:#101814;color:#f5f1e8}
*{box-sizing:border-box}body{min-height:100vh;min-height:100dvh;margin:0;background:radial-gradient(circle at 82% 18%,rgba(198,163,93,.24),transparent 32rem),linear-gradient(155deg,#17231d,#0d1511 66%,#080c0a)}
main{width:min(31rem,calc(100% - 3rem));margin:0 auto;padding:max(5rem,18vh) 0 env(safe-area-inset-bottom)}
.mark{width:2.2rem;height:2.2rem;margin:0 0 2rem;border:2px solid #d8bd80;border-top-color:transparent;border-radius:50%;animation:spin 1s linear infinite}.eyebrow{margin:0 0 .7rem;color:#d8bd80;font-size:.72rem;font-weight:750;letter-spacing:.2em;text-transform:uppercase}
h1{max-width:12ch;margin:0;font:400 clamp(2.5rem,10vw,4.2rem)/.96 Georgia,serif;letter-spacing:-.04em}#status{max-width:30ch;margin:1.4rem 0 0;color:#b7c1b9;line-height:1.55}button{margin-top:1.3rem;padding:.85rem 1.15rem;border:1px solid #d8bd80;border-radius:999px;background:#d8bd80;color:#111914;font:inherit;font-weight:700}@media(prefers-reduced-motion:reduce){.mark{animation:none}}@keyframes spin{to{transform:rotate(360deg)}}
""".strip(),
        media_type="text/css",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/mobile/bridge.js", include_in_schema=False)
def propertyquarry_mobile_bridge_script() -> PlainTextResponse:
    return PlainTextResponse(
        r"""
(() => {
  'use strict';
  const mode = document.body.dataset.mobileBridge;
  const status = document.querySelector('#status');
  const retry = document.querySelector('#retry');
  const native = window.Capacitor?.Plugins?.PropertyQuarryNative;
  let running = false;
  const setStatus = (message, failed = false) => {
    status.textContent = message;
    retry.hidden = !failed;
  };
  const withTimeout = (promise, timeoutMs, message) => new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error(message)), timeoutMs);
    Promise.resolve(promise).then(
      (value) => { window.clearTimeout(timeout); resolve(value); },
      (error) => { window.clearTimeout(timeout); reject(error); },
    );
  });
  const fetchWithTimeout = async (url, options, timeoutMs) => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(url, {...options, signal: controller.signal});
    } catch (error) {
      if (error?.name === 'AbortError') {
        throw new Error('The secure sign-in service took too long. Check your connection and try again.');
      }
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  };
  const parseFailure = async (response) => {
    try {
      const payload = await response.json();
      return payload?.detail || payload?.error?.message || `Request failed (${response.status})`;
    } catch (_) {
      return `Request failed (${response.status})`;
    }
  };
  const auth = async () => {
    if (!native) throw new Error('Open this page in the PropertyQuarry app.');
    setStatus('Reading the secure app handoff…');
    const pending = await withTimeout(
      native.getPendingAuth(),
      8000,
      'The app is still waking up. Try again to finish sign-in.',
    );
    if (!pending?.code || !pending?.pkceVerifier) throw new Error('The secure sign-in handoff expired. Start sign-in again.');
    setStatus('Confirming your secure sign-in…');
    const response = await fetchWithTimeout('/mobile/auth/redeem', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: pending.code, pkce_verifier: pending.pkceVerifier})
    }, 15000);
    if (!response.ok) throw new Error(await parseFailure(response));
    const payload = await response.json();
    setStatus('Opening your property search…');
    native.clearPendingAuth().catch(() => {});
    const shared = await withTimeout(native.getPendingShare(), 3000, '').catch(() => null);
    location.replace(shared?.propertyUrl ? '/mobile/share/bridge' : (payload.return_to || '/app/search'));
  };
  const share = async () => {
    if (!native) throw new Error('Open this page in the PropertyQuarry app.');
    const pending = await native.getPendingShare();
    if (!pending?.propertyUrl || !pending?.idempotencyKey) {
      location.replace('/app/shortlist');
      return;
    }
    setStatus('Evaluating the listing and preparing its shortlist card…');
    const response = await fetch('/app/api/mobile/property-links', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({property_url: pending.propertyUrl, confirmed: true, idempotency_key: pending.idempotencyKey})
    });
    if (response.status === 401) {
      setStatus('Sign in securely to add this property…');
      await native.startExternalLogin();
      return;
    }
    if (!response.ok) throw new Error(await parseFailure(response));
    const payload = await response.json();
    await native.clearPendingShare();
    location.replace(payload.shortlist_url || '/app/shortlist');
  };
  const run = () => {
    if (running) return;
    running = true;
    retry.hidden = true;
    (mode === 'auth' ? auth() : share())
      .catch((error) => setStatus(error?.message || 'The app handoff could not be completed.', true))
      .finally(() => { running = false; });
  };
  retry.addEventListener('click', run);
  window.setTimeout(run, 350);
})();
""".strip(),
        media_type="application/javascript",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/google/callback", response_class=HTMLResponse, response_model=None, name="google_oauth_browser_callback")
def google_oauth_browser_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    container: AppContainer = Depends(get_container),
) -> HTMLResponse | RedirectResponse:
    flow_cookie_present = bool(
        str(
            request.cookies.get(propertyquarry_google_identity.GOOGLE_IDENTITY_FLOW_COOKIE_NAME)
            or ""
        ).strip()
    )
    propertyquarry_host = str(request.url.hostname or "").strip().lower().rstrip(
        "."
    ) in {"propertyquarry.com", "www.propertyquarry.com"}
    propertyquarry_state = (
        propertyquarry_google_identity.is_propertyquarry_google_identity_state(state)
    )
    if propertyquarry_host or propertyquarry_state or flow_cookie_present:
        return _complete_propertyquarry_google_identity_sign_in(
            request=request,
            code=code,
            state=state,
            error=error,
            container=container,
        )
    state_payload = {}
    try:
        state_payload = read_google_oauth_state(state) if str(state or "").strip() else {}
    except Exception:
        state_payload = {}
    browser_source = str(state_payload.get("browser_source") or "").strip()
    sign_in_return_to = _normalize_browser_return_to(
        str(state_payload.get("return_to") or ""),
        default="/app/search",
    )
    if str(error or "").strip():
        detail = str(error_description or error or "google_oauth_denied").strip()
        if str(state_payload.get("oauth_lane") or "").strip() == "google_location_history" and str(error or "").strip() == "access_denied":
            detail = (
                "Google denied the Data Portability consent. "
                "Typical causes are: the account is not allowed as an OAuth test user, "
                "the Data Portability scope is not approved for this client, or the consent was cancelled."
            )
        if browser_source == "sign_in":
            return _google_sign_in_error_redirect(error=detail, return_to=sign_in_return_to)
        return _render_google_oauth_callback_failure(request, detail=detail, status_code=400)
    if not str(code or "").strip() or not str(state or "").strip():
        detail = "Google did not return a valid OAuth code and state."
        if browser_source == "sign_in":
            return _google_sign_in_error_redirect(error=detail, return_to=sign_in_return_to)
        return _render_google_oauth_callback_failure(request, detail=detail, status_code=400)
    try:
        product = build_product_service(container)
        if str(state_payload.get("oauth_lane") or "").strip() == "google_location_history":
            connected = product.complete_google_location_history_connect(code=code, state=state)
            try:
                sync_result = product.sync_google_location_history_portability(
                    principal_id=str(connected.get("principal_id") or "").strip(),
                    actor=str(connected.get("google_email") or connected.get("principal_id") or "google_location_history").strip(),
                )
            except Exception as exc:
                sync_result = {"state": "FAILED", "error": str(exc or "google_location_history_sync_failed")}
            return _render_public_template(
                request,
                "channel_detail.html",
                page_title="Google Location History connected",
                public_nav=PUBLIC_NAV,
                current_nav="integrations",
                access_identity=None,
                principal_id=str(connected.get("principal_id") or "").strip(),
                channel_title="Google Location History",
                channel_eyebrow="Google",
                channel={
                    "status": "connected",
                    "detail": (
                        "Location History connected. "
                        f"Initial sync state: {str(sync_result.get('state') or 'UNKNOWN').strip()}."
                    ),
                    "capabilities": [
                        "Maps Timeline export",
                        "Pocket recording location matching",
                        "Hospital/place search for archived audio",
                    ],
                    "limitations": [],
                },
                detail_points=(
                    f"Connected account: {str(connected.get('google_email') or '').strip()}",
                    f"Initial archive job: {str(sync_result.get('archive_job_id') or '').strip() or 'none'}",
                    f"Imported locations this pass: {int(sync_result.get('imported_total') or 0)}",
                ),
                body_points=(
                    "PropertyQuarry will continue using this lane for automatic Pocket/Timeline matching.",
                    "You can close this page.",
                ),
            )
        account = complete_google_oauth_callback(container=container, code=code, state=state)
    except RuntimeError as exc:
        detail = str(exc or "google_oauth_callback_failed")
        if detail == "google_oauth_redirect_uri_invalid":
            retry_response = _google_stale_redirect_retry(state_payload)
            if retry_response is not None:
                return retry_response
        if detail == "google_oauth_state_expired" and str(state or "").strip():
            try:
                expired_state = read_google_oauth_state_unchecked(state)
            except Exception:
                expired_state = {}
            return_to = _normalize_browser_return_to(str(expired_state.get("return_to") or ""), default="")
            if return_to:
                if str(expired_state.get("browser_source") or "").strip() == "sign_in":
                    return _google_sign_in_error_redirect(
                        error="google_oauth_state_expired",
                        return_to=return_to,
                    )
                separator = "&" if "?" in return_to else "?"
                return RedirectResponse(
                    f"{return_to}{separator}google_error=google_oauth_state_expired",
                    status_code=303,
                )
        if browser_source == "sign_in":
            return _google_sign_in_error_redirect(error=detail, return_to=sign_in_return_to)
        return _render_google_oauth_callback_failure(request, detail=detail, status_code=400)
    except Exception as exc:
        if browser_source == "sign_in":
            return _google_sign_in_error_redirect(
                error=str(exc or "google_oauth_callback_failed"),
                return_to=sign_in_return_to,
            )
        return _render_google_oauth_callback_failure(request, detail=str(exc or "google_oauth_callback_failed"), status_code=502)
    product = build_product_service(container)
    product.record_surface_event(
        principal_id=account.binding.principal_id,
        event_type="google_account_connected",
        surface="google_oauth_browser_callback",
        actor=str(account.google_email or account.binding.principal_id or "google_oauth").strip(),
        metadata={
            "binding_id": str(account.binding.binding_id or "").strip(),
            "google_email": str(account.google_email or "").strip(),
            "google_subject": str(account.google_subject or "").strip(),
        },
    )
    sync_result = _google_post_connect_sync(
        container=container,
        principal_id=account.binding.principal_id,
        actor=str(account.google_email or account.binding.principal_id or "google_oauth").strip(),
        granted_scopes=account.granted_scopes,
    )
    browser_source = str(state_payload.get("browser_source") or "").strip()
    return_to = _normalize_browser_return_to(str(state_payload.get("return_to") or ""), default="")
    if browser_source == "sign_in":
        try:
            access = product.issue_google_sign_in_workspace_session(
                google_email=account.google_email,
                fallback_principal_id=account.binding.principal_id,
                display_name=str(account.google_email or account.binding.principal_id or "PropertyQuarry").strip(),
                default_target=_normalize_browser_return_to(return_to, default="/app/search"),
            )
        except RuntimeError as exc:
            return RedirectResponse(
                "/sign-in?"
                + urllib.parse.urlencode(
                    {
                        "google_error": str(exc or "workspace_google_sign_in_not_found"),
                        "google_prefill_email": str(account.google_email or ""),
                        "return_to": _normalize_browser_return_to(return_to, default="/app/search"),
                    }
                ),
                status_code=303,
            )
        return RedirectResponse(str(access.get("access_url") or "/app/properties"), status_code=303)
    if browser_source == "settings_google" and return_to:
        redirect_values = {
            "account_status": "account_connected",
            "account_email": account.google_email,
        }
        if str(sync_result.get("status") or "").strip().lower() == "completed":
            redirect_values.update(
                {
                    "sync_status": "completed",
                    "sync_processed_total": str(int(sync_result.get("processed_total") or 0)),
                    "sync_synced_total": str(int(sync_result.get("synced_total") or 0)),
                    "sync_deduplicated_total": str(int(sync_result.get("deduplicated_total") or 0)),
                    "sync_suppressed_total": str(int(sync_result.get("suppressed_total") or 0)),
                }
            )
        elif str(sync_result.get("status") or "").strip().lower() == "identity_only":
            redirect_values["sync_error"] = "google_identity_only"
        else:
            redirect_values["sync_error"] = str(sync_result.get("error") or "google_sync_failed")
        destination = _append_query_value(return_to, **redirect_values)
        return RedirectResponse(destination, status_code=303)
    register_signal_payload: dict[str, object] | None = None
    if return_to.startswith("/register"):
        register_return_to = f"{_propertyquarry_public_base_url()}{return_to}"
        register_signal_payload = {
            "return_to": register_return_to,
            "google_connected": True,
            "account_email": account.google_email,
            "sync_status": str(sync_result.get("status") or "").strip(),
            "sync_processed_total": int(sync_result.get("processed_total") or 0),
            "sync_synced_total": int(sync_result.get("synced_total") or 0),
            "sync_deduplicated_total": int(sync_result.get("deduplicated_total") or 0),
            "sync_suppressed_total": int(sync_result.get("suppressed_total") or 0),
            "sync_error": str(sync_result.get("error") or "").strip(),
        }
    return_label = "Return to sign in"
    if return_to.startswith("/register"):
        return_label = "Return to email setup"
    elif return_to.startswith("/sign-in"):
        return_label = "Return to sign in"
    elif return_to.startswith("/get-started"):
        return_label = "Return to sign in"
    elif return_to.startswith("/app/"):
        return_label = "Return to account"
    return _render_public_template(
        request,
        "google_connected.html",
        page_title="Google connected",
        public_nav=PUBLIC_NAV,
        current_nav="integrations",
        access_identity=None,
        principal_id=account.binding.principal_id,
        account=account,
        scopes=list(account.granted_scopes),
        return_to=return_to,
        return_label=return_label,
        sync_result=sync_result,
        sync_detail=_google_sync_detail(sync_result),
        register_signal_payload=register_signal_payload,
    )


@router.get("/id-austria/callback", response_class=HTMLResponse, response_model=None, name="id_austria_oidc_browser_callback")
def id_austria_oidc_browser_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
    container: AppContainer = Depends(get_container),
) -> HTMLResponse | RedirectResponse:
    state_payload = {}
    try:
        state_payload = read_id_austria_oidc_state(state) if str(state or "").strip() else {}
    except Exception:
        state_payload = {}
    browser_source = str(state_payload.get("browser_source") or "").strip()
    sign_in_return_to = _normalize_browser_return_to(
        str(state_payload.get("return_to") or ""),
        default="/app/search",
    )
    if str(error or "").strip():
        detail = str(error_description or error or "id_austria_oidc_denied").strip()
        if browser_source == "sign_in":
            return RedirectResponse(
                "/sign-in?" + urllib.parse.urlencode(
                    {"id_austria_error": detail, "return_to": sign_in_return_to}
                ),
                status_code=303,
            )
        return _render_google_oauth_callback_failure(request, detail=detail, status_code=400)
    if not str(code or "").strip() or not str(state or "").strip():
        detail = "ID Austria did not return a valid OIDC code and state."
        if browser_source == "sign_in":
            return RedirectResponse(
                "/sign-in?" + urllib.parse.urlencode(
                    {"id_austria_error": detail, "return_to": sign_in_return_to}
                ),
                status_code=303,
            )
        return _render_google_oauth_callback_failure(request, detail=detail, status_code=400)
    try:
        account = complete_id_austria_oidc_callback(container=container, code=code, state=state)
    except RuntimeError as exc:
        detail = str(exc or "id_austria_oidc_callback_failed")
        if detail == "id_austria_state_expired" and str(state or "").strip():
            try:
                expired_state = read_id_austria_oidc_state_unchecked(state)
            except Exception:
                expired_state = {}
            return_to = _normalize_browser_return_to(str(expired_state.get("return_to") or ""), default="")
            if return_to:
                if str(expired_state.get("browser_source") or "").strip() == "sign_in":
                    return RedirectResponse(
                        "/sign-in?" + urllib.parse.urlencode(
                            {
                                "id_austria_error": "id_austria_state_expired",
                                "return_to": return_to,
                            }
                        ),
                        status_code=303,
                    )
                separator = "&" if "?" in return_to else "?"
                return RedirectResponse(f"{return_to}{separator}id_austria_error=id_austria_state_expired", status_code=303)
        if browser_source == "sign_in":
            return RedirectResponse(
                "/sign-in?" + urllib.parse.urlencode(
                    {"id_austria_error": detail, "return_to": sign_in_return_to}
                ),
                status_code=303,
            )
        return _render_google_oauth_callback_failure(request, detail=detail, status_code=400)
    except Exception as exc:
        detail = str(exc or "id_austria_oidc_callback_failed")
        if browser_source == "sign_in":
            return RedirectResponse(
                "/sign-in?" + urllib.parse.urlencode(
                    {"id_austria_error": detail, "return_to": sign_in_return_to}
                ),
                status_code=303,
            )
        return _render_google_oauth_callback_failure(request, detail=detail, status_code=502)

    product = build_product_service(container)
    actor = str(account.given_name or account.family_name or account.subject or account.bpk or "id_austria").strip()
    product.record_surface_event(
        principal_id=account.binding.principal_id,
        event_type="id_austria_identity_connected",
        surface="id_austria_oidc_browser_callback",
        actor=actor,
        metadata={
            "binding_id": str(account.binding.binding_id or "").strip(),
            "issuer": account.issuer,
            "id_austria_bpk_hash": str(dict(account.binding.probe_details_json or {}).get("id_austria_bpk_hash") or "").strip(),
        },
    )
    return_to = _normalize_browser_return_to(str(state_payload.get("return_to") or ""), default="")
    if browser_source == "sign_in":
        status = container.onboarding.status(principal_id=account.binding.principal_id)
        workspace = dict(status.get("workspace") or {})
        display_name = str(workspace.get("name") or account.given_name or "PropertyQuarry account").strip()
        access = product.issue_workspace_access_session(
            principal_id=account.binding.principal_id,
            email="",
            role="principal",
            display_name=display_name,
            source_kind="id_austria_sign_in",
            default_target=_normalize_browser_return_to(return_to, default="/app/search"),
        )
        return RedirectResponse(str(access.get("access_url") or "/app/search"), status_code=303)
    if return_to:
        separator = "&" if "?" in return_to else "?"
        return RedirectResponse(f"{return_to}{separator}id_austria_status=connected", status_code=303)
    return _render_public_template(
        request,
        "channel_detail.html",
        page_title="ID Austria connected",
        public_nav=PUBLIC_NAV,
        current_nav="integrations",
        access_identity=None,
        principal_id=account.binding.principal_id,
        channel_title="ID Austria connected",
        channel_eyebrow="ID Austria",
        channel={
            "status": "connected",
            "detail": "ID Austria is connected as a verified Austrian identity for this PropertyQuarry workspace.",
            "capabilities": ["Sign in with ID Austria", "Return to the same PropertyQuarry workspace"],
            "limitations": ["ID Austria is used for identity only; it does not change search ranking or delivery settings"],
        },
        detail_points=(
            f"Issuer: {account.issuer}",
            f"Name: {' '.join(part for part in (account.given_name, account.family_name) if part) or 'Not supplied'}",
        ),
        body_points=(
            "You can close this page or return to PropertyQuarry.",
            "Use account settings later to manage identity providers.",
        ),
    )
