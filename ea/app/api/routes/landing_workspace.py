from __future__ import annotations

import os
import urllib.parse

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.dependencies import RequestContext, get_cloudflare_access_identity, get_container, get_request_context
from app.api.routes.landing import (
    _browser_return_to_with_params,
    _console_shell_context,
    _form_value,
    _normalize_browser_return_to,
    _property_brilliant_directories_billing_handoff,
    _render_public_template,
    _request_is_austrian_ip,
    app_shell as _app_shell,
)
from app.api.routes.landing_content import app_nav_groups_for_brand
from app.api.routes.landing_property_research import _object_detail_row, _render_console_object_detail
from app.container import AppContainer
from app.product.property_surface_state import normalize_property_search_run_snapshot
from app.product.service import build_product_service
from app.services.cloudflare_access import CloudflareAccessIdentity
from app.services import google_oauth as google_oauth_service
from app.services import id_austria_oidc as id_austria_service
from app.services.facebook_oauth import build_facebook_oauth_start
from app.services.property_billing import property_commercial_snapshot
from app.services.public_branding import request_brand

router = APIRouter(tags=["landing"])

_PROPERTY_SUPPORT_FORM_BODY_LIMIT_BYTES = 32_768


def _property_account_redirect_target(request: Request, *, settings_view: str) -> str:
    query_pairs = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(str(request.url.query or ""), keep_blank_values=True)
        if key != "settings_view"
    ]
    query_pairs.insert(0, ("settings_view", str(settings_view or "account").strip() or "account"))
    target = "/app/account"
    if query_pairs:
        target = f"{target}?{urllib.parse.urlencode(query_pairs)}"
    return f"{target}#connected-services"


def _search_item_key(item: dict[str, object]) -> tuple[str, str, str]:
    return (
        str(item.get("kind") or "").strip(),
        str(item.get("id") or "").strip(),
        str(item.get("href") or "").strip(),
    )


def _google_connect_action(sync: dict[str, object], *, return_to: str = "/app/settings/google") -> dict[str, str]:
    connected = bool(sync.get("connected"))
    token_status = str(sync.get("token_status") or "missing").strip()
    workspace_sync_supported = bool(sync.get("workspace_sync_supported"))
    if not connected:
        return {
            "detail": "Google account linking is not set up yet.",
            "label": "Connect Google",
            "href": f"/app/actions/google/connect?return_to={return_to}",
            "method": "get",
        }
    if token_status not in {"active", "unknown"}:
        return {
            "detail": str(sync.get("reauth_required_reason") or "Google access needs attention before the next loop."),
            "label": "Reconnect Google",
            "href": f"/app/actions/google/connect?return_to={return_to}",
            "method": "get",
        }
    if not workspace_sync_supported:
        return {
            "detail": "Google sign-in is connected for this account.",
            "label": "Manage Google",
            "href": return_to,
            "method": "get",
        }
    return {
        "detail": "Google is connected and ready for another sync.",
        "label": "Run sync now",
        "href": f"/app/actions/signals/google/sync?return_to={return_to}",
        "method": "get",
    }


def _google_connect_email_recipient(*, principal_id: str, access_email: str = "", primary_email: str = "") -> str:
    candidate = str(access_email or "").strip().lower()
    if "@" in candidate:
        return candidate
    normalized_principal = str(principal_id or "").strip()
    if normalized_principal.startswith("cf-email:"):
        principal_email = normalized_principal.partition(":")[2].strip().lower()
        if "@" in principal_email:
            return principal_email
    fallback = str(primary_email or "").strip().lower()
    if "@" in fallback:
        return fallback
    return ""


def _google_connect_email_href(*, recipient_email: str, return_to: str = "/app/settings/google", scope_bundle: str = "identity") -> str:
    return "/app/actions/google/email-connect-link?" + urllib.parse.urlencode(
        {
            "recipient_email": str(recipient_email or "").strip().lower(),
            "return_to": return_to,
            "scope_bundle": scope_bundle,
        }
    )


def _google_event_type(row: dict[str, object]) -> str:
    return str(row.get("event_type") or "").strip()


def _google_event_created_at(row: dict[str, object]) -> str:
    return str(row.get("created_at") or "").strip()


def _google_event_payload(row: dict[str, object]) -> dict[str, object]:
    payload = row.get("payload")
    return dict(payload) if isinstance(payload, dict) else {}


def _google_event_identity(payload: dict[str, object], *, fallback_email_key: str = "google_email") -> str:
    return (
        str(payload.get("google_subject") or "").strip().lower()
        or str(payload.get(fallback_email_key) or "").strip().lower()
        or str(payload.get("sender_email") or "").strip().lower()
        or str(payload.get("binding_id") or "").strip().lower()
    )


def _property_google_settings_sync_status(
    *,
    product,
    principal_id: str,
    google_accounts: list[google_oauth_service.GoogleOAuthAccount],
) -> dict[str, object]:
    """Build PropertyQuarry's Google page from local receipts only."""

    event_rows = [
        dict(row)
        for row in product.list_office_events(principal_id=principal_id, limit=200, channel="product")
        if isinstance(row, dict)
    ]
    sync_last_event = next(
        (row for row in event_rows if _google_event_type(row) == "google_workspace_signal_sync_completed"),
        None,
    )
    sync_last_payload = _google_event_payload(sync_last_event or {})
    sync_last_completed_at = _google_event_created_at(sync_last_event or {})

    verification_last_event = next(
        (
            row
            for row in event_rows
            if _google_event_type(row) in {"google_send_verification_completed", "google_send_verification_failed"}
        ),
        None,
    )
    verification_last_payload = _google_event_payload(verification_last_event or {})
    verification_last_type = _google_event_type(verification_last_event or {})
    verification_last_state = (
        "completed"
        if verification_last_type == "google_send_verification_completed"
        else "failed"
        if verification_last_event is not None
        else ""
    )

    account_change_last_event = next(
        (
            row
            for row in event_rows
            if _google_event_type(row)
            in {
                "google_account_connected",
                "google_account_primary_updated",
                "google_account_disconnected",
            }
        ),
        None,
    )
    account_change_last_payload = _google_event_payload(account_change_last_event or {})
    account_change_last_type = _google_event_type(account_change_last_event or {})
    account_change_last_state = account_change_last_type.replace("google_", "") if account_change_last_event else ""

    primary_binding_id = f"{principal_id}:{google_oauth_service.GOOGLE_PROVIDER_KEY}"
    primary_account = next(
        (account for account in google_accounts if str(account.binding.binding_id or "").strip() == primary_binding_id),
        None,
    )
    active_accounts = [
        account
        for account in google_accounts
        if str(account.binding.status or "").strip().lower() == "enabled"
        and str(account.token_status or "").strip().lower() != "revoked"
    ]
    fallback_account = primary_account or (active_accounts[0] if active_accounts else (google_accounts[0] if google_accounts else None))
    account_email = str(getattr(fallback_account, "google_email", "") or sync_last_payload.get("account_email") or "").strip()
    token_status = (
        str(getattr(fallback_account, "token_status", "") or "").strip()
        or ("active" if sync_last_completed_at else ("missing" if not google_accounts else "unknown"))
    )

    verification_by_identity: dict[str, dict[str, object]] = {}
    account_change_by_identity: dict[str, dict[str, object]] = {}
    for row in event_rows:
        event_type = _google_event_type(row)
        payload = _google_event_payload(row)
        if event_type in {"google_send_verification_completed", "google_send_verification_failed"}:
            identity = _google_event_identity(payload)
            if identity and identity not in verification_by_identity:
                verification_by_identity[identity] = {
                    "state": "completed" if event_type == "google_send_verification_completed" else "failed",
                    "verified_at": _google_event_created_at(row),
                    "sender_email": str(payload.get("sender_email") or "").strip(),
                    "recipient_email": str(payload.get("recipient_email") or "").strip(),
                    "error": str(payload.get("error") or "").strip(),
                }
        if event_type in {"google_account_connected", "google_account_primary_updated", "google_account_disconnected"}:
            identity = _google_event_identity(payload)
            if identity and identity not in account_change_by_identity:
                account_change_by_identity[identity] = {
                    "state": event_type.replace("google_", ""),
                    "changed_at": _google_event_created_at(row),
                    "google_email": str(payload.get("google_email") or "").strip(),
                    "error": str(payload.get("error") or "").strip(),
                }

    send_verification_accounts: list[dict[str, object]] = []
    account_change_accounts: list[dict[str, object]] = []
    for account in google_accounts:
        identity_keys = (
            str(account.google_subject or "").strip().lower(),
            str(account.google_email or "").strip().lower(),
            str(account.binding.binding_id or "").strip().lower(),
        )
        matched_verification: dict[str, object] = {}
        matched_change: dict[str, object] = {}
        for identity_key in identity_keys:
            if not identity_key:
                continue
            if not matched_verification and identity_key in verification_by_identity:
                matched_verification = dict(verification_by_identity[identity_key])
            if not matched_change and identity_key in account_change_by_identity:
                matched_change = dict(account_change_by_identity[identity_key])
        send_verification_accounts.append(
            {
                "binding_id": str(account.binding.binding_id or "").strip(),
                "google_email": str(account.google_email or "").strip(),
                "google_subject": str(account.google_subject or "").strip(),
                "is_primary": str(account.binding.binding_id or "").strip() == primary_binding_id,
                "state": str(matched_verification.get("state") or "").strip(),
                "verified_at": str(matched_verification.get("verified_at") or "").strip(),
                "sender_email": str(matched_verification.get("sender_email") or "").strip(),
                "recipient_email": str(matched_verification.get("recipient_email") or "").strip(),
                "error": str(matched_verification.get("error") or "").strip(),
            }
        )
        account_change_accounts.append(
            {
                "binding_id": str(account.binding.binding_id or "").strip(),
                "google_email": str(account.google_email or "").strip(),
                "google_subject": str(account.google_subject or "").strip(),
                "is_primary": str(account.binding.binding_id or "").strip() == primary_binding_id,
                "state": str(matched_change.get("state") or "").strip(),
                "changed_at": str(matched_change.get("changed_at") or "").strip(),
                "error": str(matched_change.get("error") or "").strip(),
            }
        )

    return {
        "generated_at": "",
        "connected": bool(google_accounts) or bool(account_email),
        "account_email": account_email,
        "account_emails": [
            str(account.google_email or "").strip().lower()
            for account in google_accounts
            if str(account.google_email or "").strip()
        ],
        "token_status": token_status,
        "last_refresh_at": str(getattr(fallback_account, "last_refresh_at", "") or sync_last_completed_at or "").strip(),
        "reauth_required_reason": str(getattr(fallback_account, "reauth_required_reason", "") or "").strip(),
        "workspace_sync_supported": bool(
            fallback_account is not None
            and google_oauth_service.google_bundle_supports_workspace_sync(scopes=fallback_account.granted_scopes)
        ),
        "sync_completed": sum(1 for row in event_rows if _google_event_type(row) == "google_workspace_signal_sync_completed"),
        "office_signal_ingested": sum(1 for row in event_rows if _google_event_type(row) == "office_signal_ingested"),
        "last_completed_at": sync_last_completed_at,
        "last_synced_total": int(sync_last_payload.get("synced_total") or 0),
        "last_deduplicated_total": int(sync_last_payload.get("deduplicated_total") or 0),
        "last_suppressed_total": int(sync_last_payload.get("suppressed_total") or 0),
        "last_gmail_total": int(sync_last_payload.get("gmail_total") or 0),
        "last_calendar_total": int(sync_last_payload.get("calendar_total") or 0),
        "age_seconds": None,
        "freshness_state": "clear" if sync_last_completed_at else "watch",
        "account_sync_accounts": [
            dict(value)
            for value in list(sync_last_payload.get("accounts") or [])
            if isinstance(value, dict)
        ],
        "last_send_verification_at": _google_event_created_at(verification_last_event or {}),
        "last_send_verification_state": verification_last_state,
        "last_send_verification_sender_email": str(verification_last_payload.get("sender_email") or "").strip(),
        "last_send_verification_recipient_email": str(verification_last_payload.get("recipient_email") or "").strip(),
        "last_send_verification_binding_id": str(verification_last_payload.get("binding_id") or "").strip(),
        "last_send_verification_error": str(verification_last_payload.get("error") or "").strip(),
        "send_verification_accounts": send_verification_accounts,
        "last_account_change_at": _google_event_created_at(account_change_last_event or {}),
        "last_account_change_state": account_change_last_state,
        "last_account_change_binding_id": str(account_change_last_payload.get("binding_id") or "").strip(),
        "last_account_change_email": str(account_change_last_payload.get("google_email") or "").strip(),
        "account_change_accounts": account_change_accounts,
        "pending_commitment_candidates": 0,
        "covered_signal_candidates": 0,
    }


def _public_app_base_url(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-host") or "").strip().lower().rstrip(".")
    request_host = str(request.url.hostname or "").strip().lower().rstrip(".")
    forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").strip() or request.url.scheme
    effective_host = forwarded or request_host
    if effective_host in {"propertyquarry.com", "www.propertyquarry.com"}:
        host = forwarded or request_host
        return f"https://{host}"
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


def _google_account_status_detail(raw_status: str, *, is_property_brand: bool = False) -> str:
    normalized = str(raw_status or "").strip().lower()
    account_label = "Google account" if is_property_brand else "Inbox"
    primary_label = "Primary Google account" if is_property_brand else "Primary inbox"
    if normalized == "account_connected":
        return f"{account_label} connected."
    if normalized in {"primary_updated", "account_primary_updated"}:
        return f"{primary_label} updated."
    if normalized == "account_disconnected":
        return f"{account_label} disconnected."
    if normalized == "account_reconnected":
        return f"{account_label} reconnected."
    return normalized.replace("_", " ") if normalized else "Not recorded"


def _property_google_sign_in_status_label(token_status: str, *, connected: bool) -> str:
    normalized = str(token_status or "").strip().lower()
    if not connected:
        return "Ready to connect"
    if normalized in {"", "unknown"}:
        return "Connected"
    if normalized == "active":
        return "Ready"
    if normalized == "revoked":
        return "Access removed"
    if normalized in {"expired", "missing", "refresh_failed", "refresh_required", "invalid"}:
        return "Needs reconnect"
    return normalized.replace("_", " ").title()


def _google_scope_label(consent_stage: str) -> str:
    details = google_oauth_service.google_scope_bundle_details(consent_stage)
    return str(details.get("label") or "Google").strip() or "Google"


def _google_account_verification_detail(verification: dict[str, object] | None) -> str:
    payload = dict(verification or {})
    state = str(payload.get("state") or "").strip().lower()
    error = str(payload.get("error") or "").strip()
    verified_at = str(payload.get("verified_at") or "").strip()
    if error:
        return error
    if state == "completed":
        return f"send checked {verified_at[:19]}" if verified_at else "send checked"
    if state == "failed":
        return f"send check failed {verified_at[:19]}" if verified_at else "send check failed"
    return "send not checked yet"


def _positive_int(value: object) -> int:
    try:
        parsed = int(float(value or 0))
    except Exception:
        parsed = 0
    return parsed if parsed > 0 else 0


def _propertyquarry_copy(value: object, *, fallback: str = "") -> str:
    text = str(value or fallback or "").strip()
    replacements = {
        "morning memo": "market update",
        "Morning memo": "Market update",
        "decision queue": "review queue",
        "Decision queue": "Review queue",
        "commitment ledger": "follow-up ledger",
        "Commitment ledger": "Follow-up ledger",
        "draft queue": "draft review",
        "Draft queue": "Draft review",
        "draft review": "review workflow",
        "Draft review": "Review workflow",
        "Google-first pilot with one executive and one operator.": "PropertyQuarry pilot with one account owner and one collaborator.",
        "office loop": "property workflow",
        "Office loop": "Property workflow",
        "memo": "update",
        "Memo": "Update",
        "commitment": "follow-up",
        "Commitment": "Follow-up",
        "handoff": "support task",
        "Handoff": "Support task",
        "operator seats": "collaborator seats",
        "Operator seats": "Collaborator seats",
        "operator": "collaborator",
        "Operator": "Collaborator",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _propertyquarry_href(value: object) -> str:
    href = str(value or "").strip()
    if not href:
        return ""
    if href.startswith("/contact"):
        return "/support"
    if href == "/now":
        return "/product"
    if href == "/downloads":
        return "/app/api/property/account/export?download=1"
    if href == "/app/api/support":
        return "/app/settings/support"
    if href.startswith("/app/api/diagnostics/export"):
        return "/app/settings/support"
    return href


def _property_search_usage_state(product: object, *, principal_id: str, access_email: str = "", limit: int = 12) -> dict[str, object]:
    try:
        raw_runs = [
            normalize_property_search_run_snapshot(dict(row))
            for row in list(
                product.list_property_search_runs(  # type: ignore[attr-defined]
                    principal_id=principal_id,
                    limit=limit,
                    hydrate=False,
                    account_email=access_email,
                )
                or []
            )
            if isinstance(row, dict)
        ]
    except TypeError:
        try:
            raw_runs = [
                normalize_property_search_run_snapshot(dict(row))
                for row in list(product.list_property_search_runs(principal_id=principal_id, limit=limit, hydrate=False) or [])  # type: ignore[attr-defined]
                if isinstance(row, dict)
            ]
        except Exception:
            raw_runs = []
    except Exception:
        raw_runs = []
    terminal_statuses = {"processed", "completed", "completed_partial", "failed", "noop", "cancelled", "not started", "not_started"}
    ranked_total = 0
    filtered_total = 0
    listing_total = 0
    source_total = 0
    failed_source_total = 0
    repairing_source_total = 0
    tour_ready_total = 0
    packet_ready_total = 0
    latest_rows: list[dict[str, str]] = []
    active_total = 0
    completed_total = 0
    partial_total = 0
    failed_run_total = 0
    for run in raw_runs:
        summary = dict(run.get("summary") or {}) if isinstance(run.get("summary"), dict) else {}
        sources = [dict(row) for row in list(summary.get("sources") or []) if isinstance(row, dict)]
        ranked = [dict(row) for row in list(summary.get("ranked_candidates") or []) if isinstance(row, dict)]
        status = str(run.get("status") or summary.get("status") or "queued").strip().lower() or "queued"
        if status not in terminal_statuses:
            active_total += 1
        if status in {"processed", "completed", "completed_partial"}:
            completed_total += 1
        if status == "completed_partial":
            partial_total += 1
        if status == "failed":
            failed_run_total += 1
        ranked_total += len(ranked)
        filtered_total += (
            _positive_int(summary.get("filtered_total"))
            or _positive_int(summary.get("held_back_total"))
            or _positive_int(summary.get("filtered_out_total"))
        )
        listing_total += _positive_int(summary.get("listing_total") or summary.get("raw_listing_total"))
        source_total += _positive_int(summary.get("sources_total")) or len(sources)
        for source in sources:
            source_status = str(source.get("status") or source.get("state") or "").strip().lower()
            repair_status = str(source.get("repair_status") or "").strip().lower()
            if source_status in {"failed", "fetch_failed", "error", "timeout"}:
                failed_source_total += 1
            if repair_status in {"queued", "repairing", "retrying"} or source_status in {"repairing", "retrying"}:
                repairing_source_total += 1
        for candidate in ranked:
            if str(candidate.get("tour_url") or "").strip():
                tour_ready_total += 1
            if str(candidate.get("packet_url") or candidate.get("review_url") or "").strip():
                packet_ready_total += 1
        if len(latest_rows) < 6:
            run_id = str(run.get("run_id") or "").strip()
            latest_rows.append(
                {
                    "run_id": run_id,
                    "status": status.replace("_", " ") or "queued",
                    "ranked": str(len(ranked)),
                    "filtered": str(
                        _positive_int(summary.get("filtered_total"))
                        or _positive_int(summary.get("held_back_total"))
                        or _positive_int(summary.get("filtered_out_total"))
                    ),
                    "href": f"/app/shortlist?run_id={urllib.parse.quote(run_id)}" if run_id else "/app/properties",
                }
            )
    latest = raw_runs[0] if raw_runs else {}
    latest_summary = dict(latest.get("summary") or {}) if isinstance(latest.get("summary"), dict) else {}
    latest_run_id = str(latest.get("run_id") or "").strip()
    latest_status = str(latest.get("status") or latest_summary.get("status") or "no run yet").strip().replace("_", " ")
    repair_status = "Checking again" if repairing_source_total else ("Needs attention" if failed_source_total or failed_run_total else "Stable")
    return {
        "runs": raw_runs,
        "run_total": len(raw_runs),
        "active_total": active_total,
        "completed_total": completed_total,
        "partial_total": partial_total,
        "failed_run_total": failed_run_total,
        "ranked_total": ranked_total,
        "filtered_total": filtered_total,
        "listing_total": listing_total,
        "source_total": source_total,
        "failed_source_total": failed_source_total,
        "repairing_source_total": repairing_source_total,
        "tour_ready_total": tour_ready_total,
        "packet_ready_total": packet_ready_total,
        "latest_rows": latest_rows,
        "latest_run_id": latest_run_id,
        "latest_status": latest_status or "no run yet",
        "latest_href": f"/app/shortlist?run_id={urllib.parse.quote(latest_run_id)}" if latest_run_id else "/app/properties",
        "repair_status": repair_status,
    }


def _property_settings_commercial(status: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
    raw_preferences = dict(status.get("property_search_preferences") or {})
    raw_seed = dict(raw_preferences.get("raw_preferences") or {}) if isinstance(raw_preferences.get("raw_preferences"), dict) else {}
    snapshot = property_commercial_snapshot({**raw_seed, **raw_preferences})
    billing = dict(snapshot.get("billing") or {})
    commercial = dict(snapshot.get("property_commercial") or snapshot.get("commercial") or {})
    return billing, commercial


def _google_account_sync_detail(sync_row: dict[str, object] | None) -> str:
    payload = dict(sync_row or {})
    gmail_total = int(payload.get("gmail_total") or 0)
    calendar_total = int(payload.get("calendar_total") or 0)
    processed_total = int(payload.get("processed_total") or 0)
    synced_total = int(payload.get("synced_total") or 0)
    deduplicated_total = int(payload.get("deduplicated_total") or 0)
    suppressed_total = int(payload.get("suppressed_total") or 0)
    if not (gmail_total or calendar_total or processed_total or synced_total or deduplicated_total or suppressed_total):
        return "sync not yet run"
    return (
        f"sync gmail {gmail_total} · calendar {calendar_total} · "
        f"processed {processed_total} · synced {synced_total} · "
        f"deduplicated {deduplicated_total} · suppressed {suppressed_total}"
    )


def _google_account_change_detail(
    change_row: dict[str, object] | None,
    *,
    is_property_brand: bool = False,
) -> str:
    payload = dict(change_row or {})
    state = str(payload.get("state") or "").strip()
    changed_at = str(payload.get("changed_at") or "").strip()
    if not state:
        return "account action not yet recorded"
    detail = _google_account_status_detail(state, is_property_brand=is_property_brand)
    if changed_at:
        return f"{detail[:-1]} {changed_at[:19]}." if detail.endswith(".") else f"{detail} {changed_at[:19]}"
    return detail


def _google_account_row(
    account: google_oauth_service.GoogleOAuthAccount,
    *,
    return_to: str,
    is_property_brand: bool = False,
    verification: dict[str, object] | None = None,
    sync_row: dict[str, object] | None = None,
    change_row: dict[str, object] | None = None,
) -> dict[str, str]:
    binding = account.binding
    binding_id = str(binding.binding_id or "").strip()
    primary_binding_id = f"{binding.principal_id}:{google_oauth_service.GOOGLE_PROVIDER_KEY}"
    is_primary = binding_id == primary_binding_id
    enabled = str(binding.status or "").strip().lower() == "enabled"
    token_status = str(account.token_status or "unknown").strip().lower() or "unknown"
    active = enabled and token_status != "revoked"
    scope_label = _google_scope_label(account.consent_stage)
    role_detail = (
        ("Primary Google account" if is_primary else "Additional Google account")
        if is_property_brand
        else ("Primary inbox" if is_primary else "Additional inbox")
    )
    detail_parts = [
        role_detail,
        scope_label,
        (
            _property_google_sign_in_status_label(token_status, connected=enabled and token_status != "revoked")
            if is_property_brand
            else f"token {token_status.replace('_', ' ')}"
        ),
    ]
    if account.google_hosted_domain:
        detail_parts.append(account.google_hosted_domain)
    if account.last_refresh_at:
        detail_parts.append(f"refreshed {str(account.last_refresh_at)[:19]}")
    if account.reauth_required_reason:
        detail_parts.append(str(account.reauth_required_reason).replace("_", " "))
    detail_parts.append(_google_account_sync_detail(sync_row))
    detail_parts.append(_google_account_verification_detail(verification))
    detail_parts.append(_google_account_change_detail(change_row, is_property_brand=is_property_brand))

    encoded_binding_id = urllib.parse.quote(binding_id, safe=":@")
    encoded_return_to = urllib.parse.quote(return_to, safe="/?:=&")
    reconnect_href = (
        f"/app/actions/google/connect?return_to={encoded_return_to}"
        f"&scope_bundle={urllib.parse.quote(str(account.consent_stage or 'identity'), safe='')}"
    )
    verify_href = f"/app/actions/google/accounts/{encoded_binding_id}/verify-send"
    verify_label = "Verify again" if str(dict(verification or {}).get("state") or "").strip().lower() == "completed" else "Verify send"

    if active and not is_primary:
        return _object_detail_row(
            str(account.google_email or binding_id),
            " · ".join(part for part in detail_parts if part),
            "Connected",
            action_href=f"/app/actions/google/accounts/{encoded_binding_id}/make-primary",
            action_label="Make primary",
            action_method="post",
            return_to=return_to,
            secondary_action_href=verify_href,
            secondary_action_label=verify_label,
            secondary_action_method="post",
            secondary_return_to=return_to,
            tertiary_action_href=f"/app/actions/google/accounts/{encoded_binding_id}/disconnect",
            tertiary_action_label="Disconnect",
            tertiary_action_method="post",
            tertiary_return_to=return_to,
        )
    if active:
        return _object_detail_row(
            str(account.google_email or binding_id),
            " · ".join(part for part in detail_parts if part),
            "Primary",
            action_href=verify_href,
            action_label=verify_label,
            action_method="post",
            return_to=return_to,
            secondary_action_href=f"/app/actions/google/accounts/{encoded_binding_id}/disconnect",
            secondary_action_label="Disconnect",
            secondary_action_method="post",
            secondary_return_to=return_to,
        )
    return _object_detail_row(
        str(account.google_email or binding_id),
        " · ".join(part for part in detail_parts if part),
        "Reconnect",
        action_href=reconnect_href,
        action_label="Reconnect",
        action_method="get",
        secondary_action_href=f"/app/actions/google/accounts/{encoded_binding_id}/disconnect",
        secondary_action_label="Disconnect",
        secondary_action_method="post",
        secondary_return_to=return_to,
    )


@router.get("/app/settings/plan", response_class=HTMLResponse)
def settings_plan_detail(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    diagnostics = product.workspace_diagnostics(principal_id=context.principal_id)
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="plan_opened",
        surface="settings_plan",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    plan = dict(diagnostics.get("plan") or {})
    billing = dict(diagnostics.get("billing") or {})
    entitlements = dict(diagnostics.get("entitlements") or {})
    operators = dict(diagnostics.get("operators") or {})
    commercial = dict(diagnostics.get("commercial") or {})
    selected_channels = [str(value) for value in (diagnostics.get("selected_channels") or []) if str(value).strip()]
    feature_flags = [
        _propertyquarry_copy(str(value).replace("_", " "))
        for value in (entitlements.get("feature_flags") or [])
        if str(value).strip()
    ]
    warnings = [_propertyquarry_copy(value) for value in (commercial.get("warnings") or []) if str(value).strip()]
    raw_plan_unit = str(plan.get("unit_of_sale") or "workspace").strip().lower()
    plan_scope = "PropertyQuarry account" if raw_plan_unit in {"workspace", "account"} else _propertyquarry_copy(raw_plan_unit.replace("_", " "))
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title="PropertyQuarry plan",
        current_nav="settings",
        console_title="Plan",
        console_summary="Search access, payments, messaging, and collaborator limits for this account.",
        object_kind="Commercial boundary",
        object_title=str(plan.get("display_name") or "Pilot"),
        object_summary=_propertyquarry_copy(billing.get("contract_note"), fallback="Commercial terms are not set yet."),
        object_meta=[
            {"label": "Account scope", "value": plan_scope},
            {"label": "Billing state", "value": str(billing.get("billing_state") or "unknown")},
            {"label": "Invoice status", "value": str(billing.get("invoice_status") or "unknown")},
            {"label": "Support tier", "value": str(billing.get("support_tier") or "standard")},
            {"label": "Seats remaining", "value": str(operators.get("seats_remaining") or 0)},
            {"label": "Rules", "value": "Open settings"},
        ],
        object_sidebar_title="Why this boundary matters",
        object_sidebar_copy="Commercial scope explains what the account may connect, how many collaborators can help, and what support applies when something goes wrong.",
        object_sidebar_rows=[
            _object_detail_row("Channels", ", ".join(selected_channels) or "Google-first path", "Channels"),
            _object_detail_row("Messaging scope", "Included" if entitlements.get("messaging_channels_enabled") else "Upgrade required for messaging channels", "Entitlement"),
            _object_detail_row("Billing portal", str(billing.get("billing_portal_state") or "guided").replace("_", " "), "Billing"),
            _object_detail_row("Warnings", "; ".join(warnings) or "No current commercial warnings", "Support"),
        ],
        object_sections=[
            {
                "eyebrow": "Plan",
                "title": "Plan and payments",
                "items": [
                    _object_detail_row("Plan", str(plan.get("display_name") or "Pilot"), "Plan"),
                    _object_detail_row("Account scope", plan_scope, "Plan"),
                    _object_detail_row("Price label", str(billing.get("price_label") or "Custom"), "Billing"),
                    _object_detail_row("Billing state", str(billing.get("billing_state") or "unknown"), "Billing"),
                    _object_detail_row("Invoice status", str(billing.get("invoice_status") or "unknown"), "Billing"),
                    _object_detail_row("Renewal owner", str(billing.get("renewal_owner_role") or "principal").replace("_", " ").title(), "Billing"),
                    _object_detail_row("Contract note", _propertyquarry_copy(billing.get("contract_note"), fallback="No contract note recorded."), "Contract"),
                ],
            },
            {
                "eyebrow": "Entitlements",
                "title": "What is included",
                "items": [
                    _object_detail_row("Account owner seats", str(entitlements.get("principal_seats") or 0), "Seats"),
                    _object_detail_row("Collaborator seats", str(entitlements.get("operator_seats") or 0), "Seats"),
                    _object_detail_row("Audit retention", str(entitlements.get("audit_retention") or "standard"), "Retention"),
                    _object_detail_row("Feature flags", ", ".join(feature_flags) or "No enabled features", "Flags"),
                ],
            },
            {
                "eyebrow": "Billing and renewal",
                "title": "Invoices, portal, and renewal",
                "items": [
                    _object_detail_row("Billing cadence", str(billing.get("billing_cadence") or "custom").replace("_", " "), "Billing"),
                    _object_detail_row("Invoice window", str(billing.get("invoice_window_label") or "Not recorded"), "Billing"),
                    _object_detail_row("Renewal window", str(billing.get("renewal_window_label") or "Not recorded"), "Billing"),
                    _object_detail_row("Billing portal", str(billing.get("billing_portal_state") or "guided").replace("_", " "), "Portal"),
                    _object_detail_row("Upgrade path", str(commercial.get("upgrade_path_label") or "Stay on current plan"), "Upgrade"),
                    _object_detail_row("Blocked action message", _propertyquarry_copy(commercial.get("blocked_action_message"), fallback="No current commercial blocks."), "Commercial"),
                ],
            },
        ],
    )


@router.get("/app/settings/usage", response_class=HTMLResponse)
def settings_usage_detail(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    is_property_brand = request_brand(request)["key"] == "propertyquarry"
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    release_probe_read_only = str(context.auth_source or "").strip() == "propertyquarry_release_probe"
    if not release_probe_read_only:
        product.record_surface_event(
            principal_id=context.principal_id,
            event_type="usage_opened",
            surface="settings_usage",
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    if is_property_brand:
        property_usage = _property_search_usage_state(product, principal_id=context.principal_id, access_email=str(context.access_email or ""))
        property_billing, property_commercial = _property_settings_commercial(status)
        billing_handoff = (
            {}
            if release_probe_read_only
            else _property_brilliant_directories_billing_handoff()
        )
        billing_href = "/app/billing" if bool(billing_handoff.get("available")) else ""
        current_plan = (
            str(property_commercial.get("current_plan_key") or property_commercial.get("active_plan_key") or "").strip()
            or str(property_billing.get("current_plan_key") or property_billing.get("active_plan_key") or "free").strip()
        )
        latest_run_rows = [
            _object_detail_row(
                f"Search {row['run_id'][:8] or 'latest'}",
                f"{row['status']} · {row['ranked']} matches · {row['filtered']} outside this search",
                "Search",
                href=row["href"],
            )
            for row in list(property_usage["latest_rows"])
        ]
        results_items = [
            _object_detail_row("Matches", str(property_usage["ranked_total"]), "Shortlist"),
            _object_detail_row("Outside this search", str(property_usage["filtered_total"]), "Rules"),
            _object_detail_row("Listings reviewed", str(property_usage["listing_total"]), "Lists"),
            _object_detail_row("Lists used", str(property_usage["source_total"]), "Lists"),
        ]
        research_output_items = []
        if int(property_usage["packet_ready_total"] or 0) > 0:
            research_output_items.append(_object_detail_row("Property pages ready", str(property_usage["packet_ready_total"]), "Pages"))
        if int(property_usage["tour_ready_total"] or 0) > 0:
            research_output_items.append(_object_detail_row("3D tours ready", str(property_usage["tour_ready_total"]), "Tour"))
        research_output_items.extend(
            [
                _object_detail_row("Current plan", current_plan.replace("_", " ").title(), "Plan", href=billing_href),
                _object_detail_row("Support", "Account help and Google sign-in stay in settings.", "Support", href="/app/settings/support"),
            ]
        )
        reliability_items = [
            _object_detail_row("Search status", str(property_usage["repair_status"]), "Status"),
            _object_detail_row(
                "List health",
                "Waiting for first search" if int(property_usage["run_total"] or 0) <= 0 else "Derived from recent searches",
                "Lists",
            ),
        ]
        if int(property_usage["failed_source_total"] or 0) > 0:
            reliability_items.append(_object_detail_row("Unavailable lists", str(property_usage["failed_source_total"]), "Lists"))
        if int(property_usage["repairing_source_total"] or 0) > 0:
            reliability_items.append(_object_detail_row("Lists refreshing", str(property_usage["repairing_source_total"]), "Lists"))
        usage_sidebar_rows = []
        if int(property_usage["run_total"] or 0) <= 0:
            usage_sidebar_rows.append(
                _object_detail_row(
                    "Start first search",
                    "Usage appears after the first completed search.",
                    "Search",
                    action_href="/app/search",
                    action_label="Start search",
                    action_method="get",
                )
            )
        else:
            usage_sidebar_rows.append(
                _object_detail_row("Latest search", str(property_usage["latest_status"]), "Search", href=str(property_usage["latest_href"]))
            )
        if int(property_usage["active_total"] or 0) > 0:
            usage_sidebar_rows.append(_object_detail_row("Active searches", str(property_usage["active_total"]), "Search"))
        usage_sidebar_rows.extend(
            [
                _object_detail_row("Current plan", current_plan.replace("_", " ").title(), "Plan", href=billing_href),
                _object_detail_row("Search health", str(property_usage["repair_status"]), "Status"),
            ]
        )
        run_outcomes_items = latest_run_rows or [
            _object_detail_row(
                "No searches yet",
                "Launch a search to create the first usage record.",
                "Search",
                href="/app/properties",
            )
        ]
        usage_sections = [
            {
                "eyebrow": "Searches",
                "title": "Recent search outcomes",
                "items": run_outcomes_items,
                "open": True,
            },
        ]
        if int(property_usage["ranked_total"] or 0) + int(property_usage["filtered_total"] or 0) + int(property_usage["listing_total"] or 0) > 0:
            usage_sections.append(
                {
                    "eyebrow": "Results",
                    "title": "Shortlist and filtering volume",
                    "items": results_items,
                }
            )
        if research_output_items:
            usage_sections.append(
                {
                    "eyebrow": "Research output",
                    "title": "Property pages and 3D tours",
                    "items": research_output_items,
                }
            )
        if reliability_items:
            usage_sections.append(
                {
                    "eyebrow": "Search health",
                    "title": "Status and delivery",
                    "items": reliability_items,
                }
            )
        return _render_console_object_detail(
            request=request,
            context=context,
            workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
            page_title="PropertyQuarry usage and activation",
            current_nav="settings",
            console_title="Usage and activation",
            console_summary="Search activation, matches, outside-brief homes, property pages, and tours stay visible in one account view.",
            object_kind="Property usage",
            object_title=f"{property_usage['run_total']} recent searches",
            object_summary=(
                f"{property_usage['ranked_total']} matches · "
                f"{property_usage['filtered_total']} outside this search · "
                f"{property_usage['repair_status']}"
            ),
            object_meta=[
                {"label": "Searches opened", "value": str(property_usage["run_total"])},
                {"label": "Matches", "value": str(property_usage["ranked_total"])},
                {"label": "Outside brief", "value": str(property_usage["filtered_total"])},
                {"label": "Lists used", "value": str(property_usage["source_total"])},
                {"label": "Search health", "value": str(property_usage["repair_status"])},
            ],
            object_sidebar_title="What usage and activation mean here",
            object_sidebar_copy="Usage and activation are driven by completed searches, matching homes, and whether list refresh is still open.",
            object_sidebar_rows=usage_sidebar_rows,
            object_sections=usage_sections,
        )
    diagnostics = product.workspace_diagnostics(principal_id=context.principal_id)
    usage = {str(key): int(value or 0) for key, value in dict(diagnostics.get("usage") or {}).items()}
    analytics = dict(diagnostics.get("analytics") or {})
    reliability = dict(analytics.get("reliability") or {})
    billing = dict(diagnostics.get("billing") or {})
    operators = dict(diagnostics.get("operators") or {})
    readiness = dict(diagnostics.get("readiness") or {})
    queue_health = dict(diagnostics.get("queue_health") or {})
    providers = dict(diagnostics.get("providers") or {})
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title="PropertyQuarry usage",
        current_nav="settings",
        console_title="Usage",
        console_summary="Searches, decisions, and follow-up activity stay visible without exposing internal work queues.",
        object_kind="Usage",
        object_title="Account activity",
        object_summary=f"{usage.get('queue_items', 0)} open items · {usage.get('commitments', 0)} decisions · {usage.get('handoffs', 0)} follow-ups",
        object_meta=[
            {"label": "Saved notes", "value": str(usage.get("brief_items", 0))},
            {"label": "Open items", "value": str(usage.get("queue_items", 0))},
            {"label": "Decisions", "value": str(usage.get("commitments", 0))},
            {"label": "Follow-ups", "value": str(usage.get("handoffs", 0))},
        ],
        object_sidebar_title="Account status",
        object_sidebar_copy="A short view of whether the account is active, useful, and still moving.",
        object_sidebar_rows=[
            _object_detail_row("Active team members", str(operators.get("active_count") or 0), "Team"),
            _object_detail_row("Time to first value", str(analytics.get("time_to_first_value_seconds") or "pending"), "Analytics"),
            _object_detail_row("Churn risk", str(analytics.get("churn_risk") or "unknown").replace("_", " "), "Analytics"),
            _object_detail_row("Account status", str(readiness.get("detail") or "Status not recorded."), "Account"),
            _object_detail_row("List health", str(providers.get("risk_state") or "unknown").replace("_", " "), "Lists"),
        ],
        object_sections=[
            {
                "eyebrow": "Analytics",
                "title": "Product activity",
                "items": [
                    _object_detail_row("Search opened", str(counts.get("memo_opened") or 0), "Analytics"),
                    _object_detail_row("Results opened", str(counts.get("queue_opened") or 0), "Analytics"),
                    _object_detail_row("Reviews saved", str(counts.get("draft_approved") or 0), "Analytics"),
                    _object_detail_row("Messages sent", str(counts.get("draft_sent") or 0), "Analytics"),
                    _object_detail_row("Decisions closed", str(counts.get("commitment_closed") or 0), "Analytics"),
                    _object_detail_row("First value event", str(analytics.get("first_value_event") or "not reached").replace("_", " "), "Analytics"),
                ],
            },
            {
                "eyebrow": "Capacity",
                "title": "Current workload",
                "items": [
                    _object_detail_row("Seats used", str(operators.get("seats_used") or 0), "Team"),
                    _object_detail_row("Seats remaining", str(operators.get("seats_remaining") or 0), "Team"),
                    _object_detail_row("Pending approvals", str(counts.get("approval_requested") or 0), "Approvals"),
                    _object_detail_row("Current load", str(queue_health.get("load_score") or 0), "Delivery"),
                    _object_detail_row("Retrying delivery", str(queue_health.get("retrying_delivery") or 0), "Delivery"),
                    _object_detail_row("Delivery errors", str(queue_health.get("delivery_errors") or 0), "Delivery"),
                    _object_detail_row("Fallback sources", str(providers.get("lanes_with_fallback") or 0), "Sources"),
                    _object_detail_row("Support opened", str(counts.get("support_bundle_opened") or 0), "Support"),
                ],
            },
            {
                "eyebrow": "Reliability",
                "title": "Delivery and access",
                "items": [
                    _object_detail_row("Delivery reliability", str(reliability.get("delivery_reliability_state") or "watch"), "Delivery"),
                    _object_detail_row("Delivery success rate", str(reliability.get("delivery_success_rate") if reliability.get("delivery_success_rate") is not None else "n/a"), "Delivery"),
                    _object_detail_row("Access open rate", str(reliability.get("workspace_access_open_rate") if reliability.get("workspace_access_open_rate") is not None else "n/a"), "Access"),
                    _object_detail_row("Google sync reliability", str(reliability.get("sync_reliability_state") or "watch"), "Sync"),
                    _object_detail_row("Delivery failures", str(reliability.get("delivery_failure_total") or 0), "Delivery"),
                ],
            },
            {
                "eyebrow": "Success metrics",
                "title": "Adoption, closure, and correction signals",
                "items": [
                    _object_detail_row("Search open rate", str(analytics.get("memo_open_rate") or 0), "Analytics"),
                    _object_detail_row("Approval coverage rate", str(analytics.get("approval_coverage_rate") or 0), "Analytics"),
                    _object_detail_row("Approval send rate", str(analytics.get("approval_action_rate") or 0), "Analytics"),
                    _object_detail_row(
                        "Delivery closeout rate",
                        str(analytics.get("delivery_followup_resolution_rate") if analytics.get("delivery_followup_resolution_rate") is not None else "n/a"),
                        "Analytics",
                    ),
                    _object_detail_row(
                        "Blocked delivery rate",
                        str(analytics.get("delivery_followup_blocked_rate") if analytics.get("delivery_followup_blocked_rate") is not None else "n/a"),
                        "Analytics",
                    ),
                    _object_detail_row("Decision close rate", str(analytics.get("commitment_close_rate") or 0), "Analytics"),
                    _object_detail_row("Correction rate", str(analytics.get("correction_rate") or 0), "Analytics"),
                    _object_detail_row("Churn risk", str(analytics.get("churn_risk") or "unknown").replace("_", " "), "Analytics"),
                    _object_detail_row("Success summary", str(analytics.get("success_summary") or "No summary yet."), "Analytics"),
                ],
            },
        ],
    )


@router.post("/app/actions/support/request")
async def app_create_support_request(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    default_return_to = "/app/settings/support"
    content_length = str(request.headers.get("content-length") or "").strip()
    try:
        declared_length = int(content_length) if content_length else 0
        body_too_large = declared_length < 0 or declared_length > _PROPERTY_SUPPORT_FORM_BODY_LIMIT_BYTES
    except ValueError:
        body_too_large = True
    body_chunks: list[bytes] = []
    body_size = 0
    if not body_too_large:
        async for chunk in request.stream():
            body_size += len(chunk)
            if body_size > _PROPERTY_SUPPORT_FORM_BODY_LIMIT_BYTES:
                body_too_large = True
                body_chunks.clear()
                continue
            if not body_too_large:
                body_chunks.append(chunk)
    raw_body = b"" if body_too_large else b"".join(body_chunks)
    body = urllib.parse.parse_qs(raw_body.decode("utf-8", errors="ignore"), keep_blank_values=True)
    return_to = _normalize_browser_return_to(
        _form_value(body, "return_to", default_return_to),
        default=default_return_to,
    )
    if body_too_large:
        return RedirectResponse(
            _browser_return_to_with_params(return_to, support_error="support_request_too_large"),
            status_code=303,
        )
    if request_brand(request)["key"] != "propertyquarry":
        return RedirectResponse(
            _browser_return_to_with_params(return_to, support_error="support_request_unavailable"),
            status_code=303,
        )

    product = build_product_service(container)
    actor = str(context.operator_id or context.principal_id or "account_user").strip()
    try:
        created = product.create_support_request(
            principal_id=context.principal_id,
            actor=actor,
            category=_form_value(body, "category", ""),
            summary=_form_value(body, "summary", ""),
            details=_form_value(body, "details", ""),
            context_reference=_form_value(body, "context_reference", ""),
        )
    except ValueError as exc:
        error_code = str(exc or "support_request_invalid").strip()
        if error_code not in {
            "support_request_category_invalid",
            "support_request_summary_too_short",
            "support_request_summary_too_long",
            "support_request_details_too_short",
            "support_request_details_too_long",
            "support_request_reference_too_long",
        }:
            error_code = "support_request_invalid"
        return RedirectResponse(
            _browser_return_to_with_params(return_to, support_error=error_code),
            status_code=303,
        )
    except Exception:
        return RedirectResponse(
            _browser_return_to_with_params(return_to, support_error="support_request_store_failed"),
            status_code=303,
        )
    return RedirectResponse(
        _browser_return_to_with_params(
            return_to,
            support_status=str(created.get("status") or "recorded").strip(),
            support_request_id=str(created.get("request_id") or "").strip(),
        ),
        status_code=303,
    )


@router.get("/app/settings/support", response_class=HTMLResponse)
def settings_support_detail(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    is_property_brand = request_brand(request)["key"] == "propertyquarry"
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    if str(context.auth_source or "").strip() != "propertyquarry_release_probe":
        product.record_surface_event(
            principal_id=context.principal_id,
            event_type="support_opened",
            surface="settings_support",
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    if is_property_brand:
        property_usage = _property_search_usage_state(product, principal_id=context.principal_id, access_email=str(context.access_email or ""))
        billing, commercial = _property_settings_commercial(status)
        support_status = str(request.query_params.get("support_status") or "").strip().lower()
        support_request_id = str(request.query_params.get("support_request_id") or "").strip()
        support_error = str(request.query_params.get("support_error") or "").strip().lower()
        support_error_detail = {
            "support_request_category_invalid": "Choose one of the available support categories.",
            "support_request_summary_too_short": "Add a short summary of at least 5 characters.",
            "support_request_summary_too_long": "Keep the short summary to 120 characters or fewer.",
            "support_request_details_too_short": "Add at least 10 characters describing what happened and what you expected.",
            "support_request_details_too_long": "Keep the description to 2,000 characters or fewer.",
            "support_request_reference_too_long": "Keep the optional reference to 240 characters or fewer.",
            "support_request_too_large": "That form submission is too large. Shorten the description or reference and try again.",
            "support_request_store_failed": "The request could not be recorded. Nothing was submitted; try again shortly.",
            "support_request_unavailable": "Support intake is not available on this surface.",
            "support_request_invalid": "Check the request fields and try again.",
        }.get(support_error, "")
        recent_support_events = container.channel_runtime.list_recent_observations_matching(
            limit=8,
            principal_id=context.principal_id,
            channel="support",
            event_types=("support_request_created",),
        )
        support_history_rows: list[dict[str, str]] = []
        support_history_ids: set[str] = set()
        for event in recent_support_events:
            payload = dict(event.payload or {})
            request_id = str(payload.get("request_id") or event.source_id or "").strip()
            if not request_id or request_id in support_history_ids:
                continue
            support_history_ids.add(request_id)
            category_label = str(payload.get("category") or "other").replace("_", " ").title()
            created_at = str(payload.get("requested_at") or event.created_at or "").strip()
            created_label = created_at[:16].replace("T", " ") if created_at else "Time recorded"
            support_history_rows.append(
                _object_detail_row(
                    str(payload.get("summary") or "Support request"),
                    f"{category_label} · {created_label} · Reference {request_id}",
                    str(payload.get("status") or "recorded").replace("_", " ").title(),
                )
            )
        support_sidebar_rows = [
            _object_detail_row("Latest search", str(property_usage["latest_status"]), "", href=str(property_usage["latest_href"])),
        ]
        if support_status == "recorded" and support_request_id in support_history_ids:
            support_email_href = "mailto:property@propertyquarry.com?" + urllib.parse.urlencode(
                {"subject": f"PropertyQuarry support {support_request_id}"},
                quote_via=urllib.parse.quote,
            )
            support_sidebar_rows.insert(
                0,
                _object_detail_row(
                    "Support reference saved",
                    f"This is saved to your account; it has not sent a message. Email support and include reference {support_request_id}.",
                    "Recorded",
                    action_href=support_email_href,
                    action_label="Email support",
                    action_method="get",
                ),
            )
        elif support_error_detail:
            support_sidebar_rows.insert(0, _object_detail_row("Reference not saved", support_error_detail, "Check details"))
        support_sidebar_rows.append(
            _object_detail_row(
                "Contact or locked-out support",
                "Email property@propertyquarry.com for a reply, or use the public support page when you cannot reach this signed-in form.",
                "",
                action_href="mailto:property@propertyquarry.com?subject=PropertyQuarry%20support",
                action_label="Email support",
                action_method="get",
                secondary_action_href="/support",
                secondary_action_label="Public support page",
                secondary_action_method="get",
            )
        )
        support_sections: list[dict[str, object]] = []
        if support_history_rows:
            support_sections.append(
                {
                    "eyebrow": "Requests",
                    "title": "Recent support references",
                    "items": support_history_rows,
                    "open": True,
                }
            )
        support_sections.extend(
            [
                {
                    "eyebrow": "Search health",
                    "title": "Search health",
                    "items": [
                        _object_detail_row("Search status", str(property_usage["repair_status"]), ""),
                        _object_detail_row("Unavailable lists", str(property_usage["failed_source_total"]), ""),
                        _object_detail_row("Lists refreshing", str(property_usage["repairing_source_total"]), ""),
                        _object_detail_row("Failed searches", str(property_usage["failed_run_total"]), ""),
                        _object_detail_row("Partial searches", str(property_usage["partial_total"]), ""),
                    ],
                },
                {
                    "eyebrow": "Usable results",
                    "title": "What is ready while support works",
                    "items": [
                        _object_detail_row("Matches", str(property_usage["ranked_total"]), ""),
                        _object_detail_row("Outside brief", str(property_usage["filtered_total"]), ""),
                        _object_detail_row("Property pages ready", str(property_usage["packet_ready_total"]), ""),
                        _object_detail_row("3D tours ready", str(property_usage["tour_ready_total"]), ""),
                    ],
                },
                {
                    "eyebrow": "Account",
                    "title": "Billing and plan support",
                    "items": [
                        _object_detail_row("Support tier", str(billing.get("support_tier") or "standard").title(), ""),
                        _object_detail_row("Billing portal", str(billing.get("billing_portal_state") or "guided").replace("_", " "), ""),
                        _object_detail_row("Invoice window", str(billing.get("invoice_window_label") or "Not recorded"), ""),
                        _object_detail_row("Upgrade path", str(commercial.get("upgrade_path_label") or "Stay on current plan"), ""),
                        _object_detail_row("Blocked action message", _propertyquarry_copy(commercial.get("blocked_action_message"), fallback="No current commercial blocks."), ""),
                    ],
                },
            ]
        )
        return _render_console_object_detail(
            request=request,
            context=context,
            workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
            page_title="PropertyQuarry support",
            current_nav="settings",
            console_title="Support",
            console_summary="See what failed, what still works, and the next useful action.",
            object_kind="Support",
            object_title=str(property_usage["repair_status"]),
            object_summary=(
                f"{property_usage['failed_source_total']} list failures · "
                f"{property_usage['ranked_total']} matches · "
                f"{str(billing.get('support_tier') or 'standard').title()} support"
            ),
            object_meta=[
                {"label": "List failures", "value": str(property_usage["failed_source_total"])},
                {"label": "Lists refreshing", "value": str(property_usage["repairing_source_total"])},
                {"label": "Partial searches", "value": str(property_usage["partial_total"])},
                {"label": "Support tier", "value": str(billing.get("support_tier") or "standard").title()},
            ],
            object_sidebar_title="Support at a glance",
            object_sidebar_copy="Save a bounded account reference here, then email support when you need a reply.",
            object_sidebar_rows=support_sidebar_rows,
            object_sidebar_form={
                "eyebrow": "New request",
                "title": "Save support details",
                "copy": "This saves a private reference in your account; it does not send a message. To reach support, use Email support after saving. Do not include passwords, payment-card details, identity documents, or private access links.",
                "action": "/app/actions/support/request",
                "method": "post",
                "submit_label": "Save reference",
                "summary_label": "New reference",
                "open": bool(support_error_detail) or not support_history_rows,
                "fields": [
                    {"type": "hidden", "name": "return_to", "value": "/app/settings/support"},
                    {
                        "type": "select",
                        "name": "category",
                        "label": "Category",
                        "required": True,
                        "options": [
                            {"value": "search_results", "label": "Search or results", "selected": True},
                            {"value": "account_access", "label": "Account or access"},
                            {"value": "billing", "label": "Billing"},
                            {"value": "privacy", "label": "Privacy"},
                            {"value": "other", "label": "Other"},
                        ],
                    },
                    {
                        "type": "text",
                        "name": "summary",
                        "label": "Short summary",
                        "placeholder": "What needs attention?",
                        "required": True,
                        "minlength": 5,
                        "maxlength": 120,
                    },
                    {
                        "type": "textarea",
                        "name": "details",
                        "label": "What happened, and what did you expect?",
                        "placeholder": "Include the visible error and the last action you took.",
                        "required": True,
                        "minlength": 10,
                        "maxlength": 2000,
                    },
                    {
                        "type": "text",
                        "name": "context_reference",
                        "label": "Search or property reference (optional)",
                        "placeholder": "Paste a reference ID, not a private access link.",
                        "maxlength": 240,
                    },
                ],
            },
            object_sections=support_sections,
        )
    bundle = product.workspace_support_bundle(principal_id=context.principal_id)
    analytics = dict(bundle.get("analytics") or {})
    memo_loop = dict(analytics.get("memo_loop") or {})
    reliability = dict(analytics.get("reliability") or {})
    billing = dict(bundle.get("billing") or {})
    approvals = dict(bundle.get("approvals") or {})
    human_tasks = [dict(value) for value in (bundle.get("human_tasks") or [])]
    pending_delivery = [dict(value) for value in (bundle.get("pending_delivery") or [])]
    providers = dict(bundle.get("providers") or {})
    queue_health = dict(bundle.get("queue_health") or {})
    commercial = dict(bundle.get("commercial") or {})
    readiness = dict(bundle.get("readiness") or {})
    product_control = dict(bundle.get("product_control") or {})
    support_verification = dict(bundle.get("support_verification") or {})
    support_grounding = dict(bundle.get("support_assistant_grounding") or {})
    journey_gate = dict(product_control.get("journey_gate_health") or {})
    journey_freshness = dict(product_control.get("journey_gate_freshness") or {})
    support_fallout = dict(product_control.get("support_fallout") or {})
    public_guide_freshness = dict(product_control.get("public_guide_freshness") or {})
    route_stewardship = dict(product_control.get("provider_route_stewardship") or {})
    journey_highlights = [dict(value) for value in list(product_control.get("journey_highlights") or [])]
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title="PropertyQuarry support",
        current_nav="settings",
        console_title="Support",
        console_summary="See what failed, what still works, and the next useful action.",
        object_kind="Support",
        object_title=str(billing.get("support_tier") or "standard").title(),
        object_summary=str(billing.get("contract_note") or "Support is available for this account."),
        object_meta=[
            {"label": "Pending reviews", "value": str(len(list(approvals.get("pending") or [])))},
            {"label": "Support tasks", "value": str(len(human_tasks))},
            {"label": "Pending delivery", "value": str(len(pending_delivery))},
            {"label": "Sources", "value": str(providers.get("provider_count") or 0)},
        ],
        object_sidebar_title="What support can answer",
        object_sidebar_copy="The short version: what failed, what is still available, and what to try next.",
        object_sidebar_rows=[
            _object_detail_row("Support tier", str(billing.get("support_tier") or "standard"), "Support"),
            _object_detail_row("Plan status", str(billing.get("billing_state") or "unknown").replace("_", " "), "Billing"),
            _object_detail_row("Invoice status", str(billing.get("invoice_status") or "unknown").replace("_", " "), "Billing"),
            _object_detail_row("Churn risk", str(bundle.get("analytics", {}).get("churn_risk") or "unknown").replace("_", " "), "Analytics"),
            _object_detail_row("List health", str(providers.get("risk_state") or "unknown").replace("_", " "), "Lists"),
            _object_detail_row("Latest issue", str(memo_loop.get("last_issue_reason") or "No current blocker"), "Support"),
            _object_detail_row("Search setup", str(journey_gate.get("state") or "missing").replace("_", " "), "Product"),
            _object_detail_row("Support note", str(support_fallout.get("detail") or "No support note."), "Support"),
            _object_detail_row("Product status", str(product_control.get("launch_readiness") or "No product note."), "Product"),
            _object_detail_row("Guide freshness", str(public_guide_freshness.get("detail") or "No guide note."), "Guide"),
            _object_detail_row("Review due", str(route_stewardship.get("review_due") or "No review date published."), "Route"),
            _object_detail_row(
                "Follow-up",
                str(support_verification.get("state") or "not_requested").replace("_", " "),
                "Support",
                action_href=str(support_verification.get("request_action_href") or ""),
                action_label=str(support_verification.get("request_action_label") or ""),
                action_method=str(support_verification.get("request_action_method") or ""),
                return_to="/app/settings/support" if str(support_verification.get("request_action_href") or "").strip() else "",
            ),
            _object_detail_row(
                "Blocked actions",
                ", ".join(str(value).replace("_", " ") for value in (commercial.get("blocked_actions") or [])[:6]) or "No blocked actions",
                "Support",
            ),
            _object_detail_row(
                "Support details",
                "Open the support details in the browser or download them.",
                "Support",
                action_href="/app/api/diagnostics/export",
                action_label="Open details",
                action_method="get",
                secondary_action_href="/app/api/diagnostics/export?download=1",
                secondary_action_label="Download",
                secondary_action_method="get",
            ),
        ],
        object_sections=[
            {
                "eyebrow": "Follow-up",
                "title": "Latest support follow-up",
                "items": [
                    _object_detail_row(
                        "Summary",
                        str(support_verification.get("summary") or "No support follow-up is active."),
                        "Support",
                    ),
                    _object_detail_row("Recipient", str(support_verification.get("recipient_email") or "Recipient missing"), "Recipient"),
                    _object_detail_row("Channel status", str(support_verification.get("channel_receipt_detail") or "No channel update yet."), "Channel"),
                    _object_detail_row("Install status", str(support_verification.get("install_receipt_detail") or "No install update yet."), "Install"),
                    _object_detail_row("Confirmation", str(support_verification.get("confirmation_detail") or "No explicit confirmation recorded yet."), "Confirmation"),
                    _object_detail_row(
                        "Next action",
                        str(support_verification.get("recommended_action") or "No support verification action is recommended."),
                        "Action",
                        action_href=str(support_verification.get("request_action_href") or ""),
                        action_label=str(support_verification.get("request_action_label") or ""),
                        action_method=str(support_verification.get("request_action_method") or ""),
                        return_to="/app/settings/support" if str(support_verification.get("request_action_href") or "").strip() else "",
                        secondary_action_href=str(support_verification.get("delivery_url") or ""),
                        secondary_action_label="Open delivery link" if str(support_verification.get("delivery_url") or "").strip() else "",
                        secondary_action_method="get" if str(support_verification.get("delivery_url") or "").strip() else "",
                        secondary_return_to="/app/settings/support" if str(support_verification.get("delivery_url") or "").strip() else "",
                        tertiary_action_href=str(support_verification.get("access_url") or ""),
                        tertiary_action_label="Open access link" if str(support_verification.get("access_url") or "").strip() else "",
                        tertiary_action_method="get" if str(support_verification.get("access_url") or "").strip() else "",
                        tertiary_return_to="/app/settings/support" if str(support_verification.get("access_url") or "").strip() else "",
                    ),
                ],
            },
            {
                "eyebrow": "Support summary",
                "title": str(support_grounding.get("title") or "Support summary"),
                "items": (
                    [
                        _object_detail_row(
                            "Summary",
                            str(support_grounding.get("summary") or "Support stays connected to the latest account and site health."),
                            "Support",
                        )
                    ]
                    + [
                        _object_detail_row(f"Point {index}", str(item), "Support")
                        for index, item in enumerate(list(support_grounding.get("bullets") or [])[:3], start=1)
                    ]
                    + [
                        _object_detail_row(
                            str(action.get("label") or "Action"),
                            _propertyquarry_href(action.get("href")) if is_property_brand else str(action.get("href") or ""),
                            "Action",
                            href=_propertyquarry_href(action.get("href")) if is_property_brand else str(action.get("href") or ""),
                            action_href=_propertyquarry_href(action.get("href")) if is_property_brand else str(action.get("href") or ""),
                            action_label=str(action.get("label") or ""),
                            action_method=str(action.get("method") or "get"),
                        )
                        for action in list(support_grounding.get("actions") or [])[:2]
                    ]
                    + [
                        _object_detail_row(
                            str(source.get("label") or "Source"),
                            str(source.get("path") or ""),
                            str(source.get("as_of") or "Source"),
                        )
                        for source in list(support_grounding.get("sources") or [])[:2]
                    ]
                ),
            },
            {
                "eyebrow": "Product",
                "title": "Current product status",
                "items": [
                    _object_detail_row("Active focus", str(product_control.get("active_wave") or "No active focus."), "Product"),
                    _object_detail_row("Wave status", str(product_control.get("active_wave_status") or "unknown").replace("_", " "), "Wave"),
                    _object_detail_row("Summary", str(product_control.get("summary") or "No product summary."), "Product"),
                    _object_detail_row("Search setup", str(journey_gate.get("state") or "missing").replace("_", " "), "Product"),
                    _object_detail_row("Next action", str(journey_gate.get("recommended_action") or journey_gate.get("reason") or "No action published."), "Action"),
                    _object_detail_row("Support note", str(support_fallout.get("detail") or "No support note."), "Support"),
                    _object_detail_row("Product status", str(product_control.get("launch_readiness") or "No product note."), "Product"),
                    _object_detail_row("Route default", str(route_stewardship.get("default_status") or "No route default note."), "Route"),
                    _object_detail_row("Route status", str(route_stewardship.get("canary_status") or "No route note."), "Route"),
                    _object_detail_row("Review due", str(route_stewardship.get("review_due") or "No review date published."), "Route"),
                    _object_detail_row("Ask next", str(product_control.get("next_checkpoint_question") or "No next question."), "Product"),
                    _object_detail_row("Guide freshness", str(public_guide_freshness.get("detail") or "No guide note."), "Guide"),
                ],
            },
            {
                "eyebrow": "Approvals",
                "title": "Pending review and recent decisions",
                "items": [
                    _object_detail_row(
                        str(item.get("reason") or "Approval pending"),
                        f"{str(item.get('status') or 'pending').replace('_', ' ')} · expires {str(item.get('expires_at') or '')[:10] or 'n/a'}",
                        "Pending",
                    )
                    for item in list(approvals.get("pending") or [])[:6]
                ] or [_object_detail_row("No pending approvals", "Nothing is blocked on approval right now.", "Clear")],
            },
            {
                "eyebrow": "Support",
                "title": "Open support items",
                "items": (
                    [
                        _object_detail_row(
                            str(item.get("brief") or "Human task"),
                            f"{str(item.get('status') or 'pending').replace('_', ' ')} · {str(item.get('assignment_state') or 'unassigned').replace('_', ' ')}",
                            str(item.get("priority") or "normal").title(),
                        )
                        for item in human_tasks[:4]
                    ]
                    + [
                        _object_detail_row(
                            f"{str(item.get('channel') or 'delivery').title()} delivery",
                            f"{str(item.get('recipient') or 'unknown')} · {str(item.get('status') or 'pending').replace('_', ' ')}",
                            "Delivery",
                        )
                        for item in pending_delivery[:2]
                    ]
                )
                or [_object_detail_row("Support is clear", "No support tasks or pending delivery are currently blocking the account.", "Clear")],
            },
            {
                "eyebrow": "Commercial escalation",
                "title": "Billing path, upgrade path, and blockers",
                "items": [
                    _object_detail_row("Billing portal", str(billing.get("billing_portal_state") or "guided").replace("_", " "), "Billing"),
                    _object_detail_row("Invoice window", str(billing.get("invoice_window_label") or "Not recorded"), "Billing"),
                    _object_detail_row("Upgrade path", str(commercial.get("upgrade_path_label") or "Stay on current plan"), "Upgrade"),
                    _object_detail_row("Seat pressure", str(commercial.get("seat_pressure_label") or "No seat pressure"), "Seats"),
                    _object_detail_row("Blocked action message", str(commercial.get("blocked_action_message") or "No current commercial blocks."), "Support"),
                ],
            },
            {
                "eyebrow": "Reliability",
                "title": "Delivery, access, and sync",
                "items": [
                    _object_detail_row("Delivery reliability", str(reliability.get("delivery_reliability_state") or "watch"), "Delivery"),
                    _object_detail_row("Delivery success rate", str(reliability.get("delivery_success_rate") if reliability.get("delivery_success_rate") is not None else "n/a"), "Delivery"),
                    _object_detail_row("Latest issue", str(memo_loop.get("last_issue_reason") or "No current blocker"), "Support"),
                    _object_detail_row("Fix detail", str(memo_loop.get("last_issue_fix_detail") or "No fix needed"), "Support"),
                    _object_detail_row(
                        "Fix target",
                        str(memo_loop.get("last_issue_fix_label") or "No action needed"),
                        "Support",
                        href=str(memo_loop.get("last_issue_fix_href") or ""),
                        action_href=str(memo_loop.get("last_issue_fix_href") or ""),
                        action_label=str(memo_loop.get("last_issue_fix_label") or ""),
                        action_method="get" if str(memo_loop.get("last_issue_fix_href") or "").strip() else "",
                    ),
                    _object_detail_row("Access reliability", str(reliability.get("access_reliability_state") or "watch"), "Access"),
                    _object_detail_row("Access open rate", str(reliability.get("workspace_access_open_rate") if reliability.get("workspace_access_open_rate") is not None else "n/a"), "Access"),
                    _object_detail_row("Sync reliability", str(reliability.get("sync_reliability_state") or "watch"), "Sync"),
                ],
            },
            {
                "eyebrow": "Health",
                "title": "Success metrics and churn risk",
                "items": [
                    _object_detail_row("Search open rate", str(analytics.get("memo_open_rate") or 0), "Analytics"),
                    _object_detail_row("Approval coverage rate", str(analytics.get("approval_coverage_rate") or 0), "Analytics"),
                    _object_detail_row("Approval send rate", str(analytics.get("approval_action_rate") or 0), "Analytics"),
                    _object_detail_row(
                        "Delivery closeout rate",
                        str(analytics.get("delivery_followup_resolution_rate") if analytics.get("delivery_followup_resolution_rate") is not None else "n/a"),
                        "Analytics",
                    ),
                    _object_detail_row(
                        "Blocked delivery rate",
                        str(analytics.get("delivery_followup_blocked_rate") if analytics.get("delivery_followup_blocked_rate") is not None else "n/a"),
                        "Analytics",
                    ),
                    _object_detail_row("Decision close rate", str(analytics.get("commitment_close_rate") or 0), "Analytics"),
                    _object_detail_row("Correction rate", str(analytics.get("correction_rate") or 0), "Analytics"),
                    _object_detail_row("Churn risk", str(analytics.get("churn_risk") or "unknown").replace("_", " "), "Analytics"),
                    _object_detail_row("Success summary", str(analytics.get("success_summary") or "No summary yet."), "Analytics"),
                ],
            },
            {
                "eyebrow": "Delivery",
                "title": "Delivery and fallback",
                "items": [
                    _object_detail_row("Delivery state", str(queue_health.get("state") or "healthy").replace("_", " "), "Delivery"),
                    _object_detail_row("Current load", str(queue_health.get("load_score") or 0), "Delivery"),
                    _object_detail_row("Retrying delivery", str(queue_health.get("retrying_delivery") or 0), "Delivery"),
                    _object_detail_row("Delivery errors", str(queue_health.get("delivery_errors") or 0), "Delivery"),
                    _object_detail_row("Fallback sources", str(providers.get("lanes_with_fallback") or 0), "Sources"),
                    _object_detail_row("Backup sources", str(providers.get("failover_ready_lanes") or 0), "Sources"),
                ],
            },
        ],
    )


@router.get("/app/settings/outcomes", response_class=HTMLResponse)
def settings_outcomes_detail(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    is_property_brand = request_brand(request)["key"] == "propertyquarry"
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="outcomes_opened",
        surface="settings_outcomes",
        actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
    )
    if is_property_brand:
        property_usage = _property_search_usage_state(product, principal_id=context.principal_id, access_email=str(context.access_email or ""))
        outcomes = {
            "churn_risk": "watch",
            "correction_rate": 0,
            "first_value_event": "search ready",
        }
        return _render_console_object_detail(
            request=request,
            context=context,
            workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
            page_title="PropertyQuarry outcomes",
            current_nav="settings",
            console_title="Outcomes",
            console_summary="Outcomes track whether searches produced matching homes, whether search checks stayed bounded, and what follow-up is ready.",
            object_kind="Outcomes",
            object_title=f"{property_usage['ranked_total']} matches",
            object_summary=(
                f"{property_usage['completed_total']} completed searches · "
                f"{property_usage['partial_total']} partial · "
                f"{property_usage['failed_run_total']} failed"
            ),
            object_meta=[
                {"label": "Matches", "value": str(property_usage["ranked_total"])},
                {"label": "Completed searches", "value": str(property_usage["completed_total"])},
                {"label": "Partial searches", "value": str(property_usage["partial_total"])},
                {"label": "Search health", "value": str(property_usage["repair_status"])},
            ],
            object_sidebar_title="What a healthy search shows",
            object_sidebar_copy="A healthy PropertyQuarry loop returns matching homes quickly, keeps requirements understandable, preserves useful partial results, and keeps open details visible.",
            object_sidebar_rows=[
                _object_detail_row("Latest search", str(property_usage["latest_status"]), "Search", href=str(property_usage["latest_href"])),
                _object_detail_row("Matches", str(property_usage["ranked_total"]), "Shortlist"),
                _object_detail_row("Outside brief", str(property_usage["filtered_total"]), "Rules"),
                _object_detail_row("Unavailable lists", str(property_usage["failed_source_total"]), "Lists"),
                _object_detail_row("Search health", str(property_usage["repair_status"]), "Status"),
                _object_detail_row("Churn risk", str(outcomes.get("churn_risk") or "watch").replace("_", " "), "Account"),
            ],
            object_sections=[
                {
                    "eyebrow": "Search outcomes",
                    "title": "What the recent searches produced",
                    "items": [
                        _object_detail_row("Searches", str(property_usage["run_total"]), "Search"),
                        _object_detail_row("Completed searches", str(property_usage["completed_total"]), "Search"),
                        _object_detail_row("Active searches", str(property_usage["active_total"]), "Search"),
                        _object_detail_row("Failed searches", str(property_usage["failed_run_total"]), "Search"),
                    ],
                },
                {
                    "eyebrow": "Result quality",
                    "title": "Shortlist and rule pressure",
                    "items": [
                        _object_detail_row("Matches", str(property_usage["ranked_total"]), "Shortlist"),
                        _object_detail_row("Outside brief", str(property_usage["filtered_total"]), "Rules"),
                        _object_detail_row("Listings reviewed", str(property_usage["listing_total"]), "Lists"),
                        _object_detail_row("Lists used", str(property_usage["source_total"]), "Lists"),
                    ],
                },
                {
                    "eyebrow": "Follow-up",
                    "title": "Property pages and tours ready",
                    "items": [
                        _object_detail_row("Property pages ready", str(property_usage["packet_ready_total"]), "Pages"),
                        _object_detail_row("3D tours ready", str(property_usage["tour_ready_total"]), "Tour"),
                        _object_detail_row("Correction rate", str(outcomes.get("correction_rate") or 0), "Learning"),
                        _object_detail_row("First value event", str(outcomes.get("first_value_event") or "pending").replace("_", " "), "Activation"),
                    ],
                },
            ],
        )
    outcomes = product.workspace_outcomes(principal_id=context.principal_id)
    diagnostics = product.workspace_diagnostics(principal_id=context.principal_id)
    counts = {str(key): int(value or 0) for key, value in dict(outcomes.get("counts") or {}).items()}
    memo_loop = dict(outcomes.get("memo_loop") or {})
    office_loop_proof = dict(outcomes.get("office_loop_proof") or {})
    product_control = dict(diagnostics.get("product_control") or {})
    journey_gate = dict(product_control.get("journey_gate_health") or {})
    journey_freshness = dict(product_control.get("journey_gate_freshness") or {})
    support_fallout = dict(product_control.get("support_fallout") or {})
    public_guide_freshness = dict(product_control.get("public_guide_freshness") or {})
    route_stewardship = dict(product_control.get("provider_route_stewardship") or {})
    proof_checks = [dict(value) for value in list(office_loop_proof.get("checks") or [])]
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title="PropertyQuarry outcomes",
        current_nav="settings",
        console_title="Outcomes",
        console_summary="First value, review activity, commitment closure, and correction signals explain whether this office is actually getting value.",
        object_kind="Outcome",
        object_title=str(outcomes.get("success_summary") or "Outcomes"),
        object_summary=(
            f"Memo open rate {outcomes.get('memo_open_rate') or 0} · "
            f"Commitment close rate {outcomes.get('commitment_close_rate') or 0}"
        ),
        object_meta=[
            {"label": "First value event", "value": str(outcomes.get("first_value_event") or "pending").replace("_", " ")},
            {"label": "Time to first value", "value": str(outcomes.get("time_to_first_value_seconds") or "pending")},
            {"label": "Memo open rate", "value": str(outcomes.get("memo_open_rate") or 0)},
            {"label": "Useful loop days", "value": str(memo_loop.get("days_with_useful_loop") or 0)},
            {"label": "Churn risk", "value": str(outcomes.get("churn_risk") or "watch").replace("_", " ")},
        ],
        object_sidebar_title="What a healthy loop shows",
        object_sidebar_copy="A healthy office loop reaches first value quickly, gets the memo opened, turns approvals into actions, and closes commitments at a visible rate.",
        object_sidebar_rows=[
            _object_detail_row("Success summary", str(outcomes.get("success_summary") or "No outcome summary yet."), "Summary"),
            _object_detail_row("Approval coverage rate", str(outcomes.get("approval_coverage_rate") or 0), "Review"),
            _object_detail_row("Approval send rate", str(outcomes.get("approval_action_rate") or 0), "Review"),
            _object_detail_row(
                "Delivery closeout rate",
                str(outcomes.get("delivery_followup_resolution_rate") if outcomes.get("delivery_followup_resolution_rate") is not None else "n/a"),
                "Review",
            ),
            _object_detail_row(
                "Blocked delivery rate",
                str(outcomes.get("delivery_followup_blocked_rate") if outcomes.get("delivery_followup_blocked_rate") is not None else "n/a"),
                "Review",
            ),
            _object_detail_row("Commitment close rate", str(outcomes.get("commitment_close_rate") or 0), "Closure"),
            _object_detail_row("Correction rate", str(outcomes.get("correction_rate") or 0), "Learning"),
            _object_detail_row("Scheduled memo loop", str(memo_loop.get("state") or "watch").replace("_", " "), "Memo"),
            _object_detail_row(
                "Last memo issue",
                str(memo_loop.get("last_issue_reason") or "No current memo blocker"),
                "Memo",
                href=str(memo_loop.get("last_issue_fix_href") or "/app/settings/outcomes"),
                action_href=str(memo_loop.get("last_issue_fix_href") or ""),
                action_label=str(memo_loop.get("last_issue_fix_label") or ""),
                action_method="get" if str(memo_loop.get("last_issue_fix_href") or "").strip() else "",
            ),
        ],
        object_sections=[
            {
                "eyebrow": "Activation",
                "title": "Time to first value",
                "items": [
                    _object_detail_row("First value event", str(outcomes.get("first_value_event") or "pending").replace("_", " "), "Activation"),
                    _object_detail_row("Time to first value", str(outcomes.get("time_to_first_value_seconds") or "pending"), "Activation"),
                    _object_detail_row("Memo opened", str(counts.get("memo_opened") or 0), "Memo"),
                    _object_detail_row("Approval requested", str(counts.get("approval_requested") or 0), "Approvals"),
                ],
            },
            {
                "eyebrow": "Loop quality",
                "title": "How the daily loop is performing",
                "items": [
                    _object_detail_row("Memo open rate", str(outcomes.get("memo_open_rate") or 0), "Memo"),
                    _object_detail_row("Approval coverage rate", str(outcomes.get("approval_coverage_rate") or 0), "Approvals"),
                    _object_detail_row("Approval send rate", str(outcomes.get("approval_action_rate") or 0), "Approvals"),
                    _object_detail_row(
                        "Delivery closeout rate",
                        str(outcomes.get("delivery_followup_resolution_rate") if outcomes.get("delivery_followup_resolution_rate") is not None else "n/a"),
                        "Operators",
                    ),
                    _object_detail_row(
                        "Blocked delivery rate",
                        str(outcomes.get("delivery_followup_blocked_rate") if outcomes.get("delivery_followup_blocked_rate") is not None else "n/a"),
                        "Operators",
                    ),
                    _object_detail_row("Commitment close rate", str(outcomes.get("commitment_close_rate") or 0), "Commitments"),
                    _object_detail_row("Correction rate", str(outcomes.get("correction_rate") or 0), "Learning"),
                    _object_detail_row("Churn risk", str(outcomes.get("churn_risk") or "watch").replace("_", " "), "Risk"),
                ],
            },
            {
                "eyebrow": "Scheduled memo",
                "title": "How the recurring memo loop is proving itself",
                "items": [
                    _object_detail_row("Enabled", str(memo_loop.get("enabled") or False).lower(), "Memo"),
                    _object_detail_row("Cadence", str(memo_loop.get("cadence") or "daily_morning").replace("_", " "), "Memo"),
                    _object_detail_row("Delivery time", f"{memo_loop.get('delivery_time_local') or '08:00'} {memo_loop.get('timezone') or workspace.get('timezone') or 'UTC'}", "Memo"),
                    _object_detail_row("Recipient", str(memo_loop.get("recipient_email") or "waiting for recipient"), "Memo"),
                    _object_detail_row("Useful loop days", str(memo_loop.get("days_with_useful_loop") or 0), "Memo"),
                    _object_detail_row("Last scheduled send", str(memo_loop.get("last_scheduled_sent_at") or "not yet sent"), "Memo"),
                    _object_detail_row("Blocked sends", str(memo_loop.get("scheduled_blocked") or 0), "Memo"),
                    _object_detail_row("Failed sends", str(memo_loop.get("scheduled_failed") or 0), "Delivery"),
                    _object_detail_row("Latest issue", str(memo_loop.get("last_issue_reason") or "No current blocker"), "Support"),
                    _object_detail_row(
                        "Fix target",
                        str(memo_loop.get("last_issue_fix_label") or "No action needed"),
                        "Support",
                        href=str(memo_loop.get("last_issue_fix_href") or ""),
                        action_href=str(memo_loop.get("last_issue_fix_href") or ""),
                        action_label=str(memo_loop.get("last_issue_fix_label") or ""),
                        action_method="get" if str(memo_loop.get("last_issue_fix_href") or "").strip() else "",
                    ),
                ],
            },
            {
                "eyebrow": "Counts",
                "title": "Signals feeding outcomes",
                "items": [
                    _object_detail_row("Reviews saved", str(counts.get("draft_approved") or 0), "Reviews"),
                    _object_detail_row("Messages sent", str(counts.get("draft_sent") or 0), "Messages"),
                    _object_detail_row("Follow-ups created", str(counts.get("draft_send_followup_created") or 0), "Follow-up"),
                    _object_detail_row("Follow-ups closed", str(outcomes.get("delivery_followup_closeout_count") or 0), "Follow-up"),
                    _object_detail_row("Blocked messages", str(outcomes.get("delivery_followup_blocked_count") or 0), "Delivery"),
                    _object_detail_row("Decision created", str(counts.get("commitment_created") or 0), "Decisions"),
                    _object_detail_row("Decision closed", str(counts.get("commitment_closed") or 0), "Decisions"),
                    _object_detail_row("Follow-up completed", str(counts.get("handoff_completed") or 0), "Follow-up"),
                    _object_detail_row("Memory corrected", str(counts.get("memory_corrected") or 0), "People"),
                    _object_detail_row("Support opened", str(counts.get("support_bundle_opened") or 0), "Support"),
                ],
            },
        ],
    )


@router.get("/app/settings/google", response_class=HTMLResponse)
def settings_google_detail(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    is_property_brand = request_brand(request)["key"] == "propertyquarry"
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    if str(context.auth_source or "").strip() != "propertyquarry_release_probe":
        product.record_surface_event(
            principal_id=context.principal_id,
            event_type="google_settings_opened",
            surface="settings_google",
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    if is_property_brand:
        return RedirectResponse(_property_account_redirect_target(request, settings_view="google"), status_code=303)
    sync_error = str(request.query_params.get("sync_error") or "").strip()
    sync_status = str(request.query_params.get("sync_status") or "").strip()
    sync_processed_total = int(request.query_params.get("sync_processed_total") or 0)
    sync_synced_total = int(request.query_params.get("sync_synced_total") or 0)
    sync_deduplicated_total = int(request.query_params.get("sync_deduplicated_total") or 0)
    sync_suppressed_total = int(request.query_params.get("sync_suppressed_total") or 0)
    google_error = str(request.query_params.get("google_error") or "").strip()
    account_status = str(request.query_params.get("account_status") or "").strip()
    verify_status = str(request.query_params.get("verify_status") or "").strip()
    verify_error = str(request.query_params.get("verify_error") or "").strip()
    verify_sender = str(request.query_params.get("verify_sender") or "").strip()
    verify_recipient = str(request.query_params.get("verify_recipient") or "").strip()
    email_link_status = str(request.query_params.get("email_link_status") or "").strip()
    email_link_email = str(request.query_params.get("email_link_email") or "").strip()
    email_link_bundle = str(request.query_params.get("email_link_bundle") or "").strip()
    email_link_error = str(request.query_params.get("email_link_error") or "").strip()
    google_accounts = sorted(
        google_oauth_service.list_google_accounts(container=container, principal_id=context.principal_id),
        key=lambda account: (
            account.binding.binding_id != f"{account.binding.principal_id}:{google_oauth_service.GOOGLE_PROVIDER_KEY}",
            str(account.google_email or "").strip().lower(),
            str(account.binding.binding_id or "").strip(),
        ),
    )
    sync = (
        _property_google_settings_sync_status(
            product=product,
            principal_id=context.principal_id,
            google_accounts=google_accounts,
        )
        if is_property_brand
        else product.google_signal_sync_status(principal_id=context.principal_id)
    )
    primary_account = next(
        (
            account
            for account in google_accounts
            if str(account.binding.binding_id or "").strip()
            == f"{account.binding.principal_id}:{google_oauth_service.GOOGLE_PROVIDER_KEY}"
        ),
        google_accounts[0] if google_accounts else None,
    )
    active_account_total = sum(
        1
        for account in google_accounts
        if str(account.binding.status or "").strip().lower() == "enabled"
        and str(account.token_status or "").strip().lower() != "revoked"
    )
    connected_account_total = len(google_accounts)
    primary_email = str(
        getattr(primary_account, "google_email", "") or sync.get("account_email") or ""
    ).strip()
    connect_another_href = (
        "/app/actions/google/connect?"
        + urllib.parse.urlencode({"return_to": "/app/settings/google", "scope_bundle": "identity"})
    )
    email_connect_recipient = _google_connect_email_recipient(
        principal_id=context.principal_id,
        access_email=str(context.access_email or ""),
        primary_email=primary_email,
    )
    email_connect_href = ""
    covered_sync_candidates = int(sync.get("covered_signal_candidates") or 0)
    action = _google_connect_action(sync, return_to="/app/settings/google")
    resolved_verify_state = verify_status or str(sync.get("last_send_verification_state") or "").strip()
    resolved_verify_sender = verify_sender or str(sync.get("last_send_verification_sender_email") or "").strip()
    resolved_verify_recipient = verify_recipient or str(sync.get("last_send_verification_recipient_email") or "").strip()
    resolved_verify_error = verify_error or str(sync.get("last_send_verification_error") or "").strip()
    verification_rows = [
        dict(value)
        for value in list(sync.get("send_verification_accounts") or [])
        if isinstance(value, dict)
    ]
    account_sync_rows = [
        dict(value)
        for value in list(sync.get("account_sync_accounts") or [])
        if isinstance(value, dict)
    ]
    account_sync_by_email = {
        str(row.get("account_email") or "").strip().lower(): row
        for row in account_sync_rows
        if str(row.get("account_email") or "").strip()
    }
    account_change_rows = [
        dict(value)
        for value in list(sync.get("account_change_accounts") or [])
        if isinstance(value, dict)
    ]
    account_change_by_binding = {
        str(row.get("binding_id") or "").strip(): row
        for row in account_change_rows
        if str(row.get("binding_id") or "").strip()
    }
    verification_by_binding = {
        str(row.get("binding_id") or "").strip(): row
        for row in verification_rows
        if str(row.get("binding_id") or "").strip()
    }
    resolved_account_change_state = account_status or str(sync.get("last_account_change_state") or "").strip()
    resolved_account_change_email = str(sync.get("last_account_change_email") or "").strip()
    resolved_account_change_at = str(sync.get("last_account_change_at") or "").strip()
    verify_detail = resolved_verify_error or (
        f"Checked {resolved_verify_sender} -> {resolved_verify_recipient or resolved_verify_sender}"
        if resolved_verify_state == "completed" and resolved_verify_sender
        else "Not recorded"
    )
    account_change_detail = _google_account_status_detail(
        resolved_account_change_state,
        is_property_brand=is_property_brand,
    )
    if resolved_account_change_email and resolved_account_change_at:
        account_change_detail = f"{account_change_detail} {resolved_account_change_email} · {resolved_account_change_at[:19]}"
    elif resolved_account_change_email:
        account_change_detail = f"{account_change_detail} {resolved_account_change_email}"
    elif resolved_account_change_at and resolved_account_change_state:
        account_change_detail = f"{account_change_detail} {resolved_account_change_at[:19]}"
    if email_link_error:
        email_link_detail = email_link_error
    elif email_link_status == "sent" and email_link_email:
        bundle_label = str(google_oauth_service.google_scope_bundle_details(email_link_bundle or "identity").get("label") or "Google sign-in")
        email_link_detail = f"Sent {bundle_label} link to {email_link_email}"
    else:
        email_link_detail = "Google email links are disabled on this product surface. Use direct connect from this device."
    connected_accounts_label = "Connected Google accounts" if is_property_brand else "Connected inboxes"
    active_accounts_label = "Active Google accounts" if is_property_brand else "Active inboxes"
    primary_account_label = "Primary Google account" if is_property_brand else "Primary inbox"
    add_account_label = "Add Google account" if is_property_brand else "Add inbox"
    connected_accounts_detail = (
        f"{connected_account_total} Google account{'s' if connected_account_total != 1 else ''} connected."
        if is_property_brand
        else f"{connected_account_total} inbox{'es' if connected_account_total != 1 else ''} attached to this workspace."
    )
    accounts_section_title = "Connected Google accounts" if is_property_brand else "Connected inboxes and send defaults"
    if is_property_brand:
        property_google_sign_in_status = _property_google_sign_in_status_label(
            str(sync.get("token_status") or "missing"),
            connected=connected_account_total > 0,
        )
        if connected_account_total > 0:
            sync_summary = f"{connected_account_total} connected · {property_google_sign_in_status.lower()}"
        else:
            sync_summary = "Sign in with Google on this device. Google opens the same account and creates it if needed."
    else:
        sync_summary = (
            f"{connected_account_total} connected inbox{'es' if connected_account_total != 1 else ''} · "
            f"{str(sync.get('freshness_state') or 'watch').replace('_', ' ')} freshness · "
            f"{int(sync.get('pending_commitment_candidates') or 0)} pending candidates"
        )
    if covered_sync_candidates and not is_property_brand:
        sync_summary = f"{sync_summary} · {covered_sync_candidates} covered by drafts"
    if sync_error:
        last_manual_sync_detail = sync_error
    elif google_error:
        last_manual_sync_detail = google_error
    elif sync_status == "completed":
        if sync_processed_total or sync_suppressed_total:
            last_manual_sync_detail = (
                f"Completed · processed {sync_processed_total} · staged {sync_synced_total} · "
                f"deduplicated {sync_deduplicated_total} · suppressed {sync_suppressed_total}"
            )
        else:
            last_manual_sync_detail = "Completed · no recent Gmail or Calendar signals were staged"
    else:
        last_manual_sync_detail = "Not recorded"
    object_meta = [
        {"label": "Connected", "value": "Yes" if connected_account_total else "No"},
        {"label": primary_account_label, "value": primary_email or "Not connected"},
        {"label": "Sign-in status", "value": _property_google_sign_in_status_label(str(sync.get("token_status") or "missing"), connected=connected_account_total > 0) if is_property_brand else str(sync.get("token_status") or "missing").replace("_", " ")},
    ]
    if not is_property_brand:
        object_meta.extend([
            {"label": connected_accounts_label, "value": str(connected_account_total)},
            {"label": active_accounts_label, "value": str(active_account_total)},
        ])
    if not is_property_brand:
        object_meta.append({"label": "Sync runs", "value": str(sync.get("sync_completed") or 0)})
    if is_property_brand:
        object_sidebar_rows = [
            _object_detail_row(
                "Next step",
                action["detail"],
                "",
                href="/app/settings/google",
                action_href=action["href"],
                action_label=action["label"],
                action_method=action["method"],
                return_to="/app/settings/google",
            ),
        ]
        if connected_account_total > 0:
            object_sections = [
                {
                    "eyebrow": "Connection",
                    "title": "Google identity",
                    "items": [
                        _object_detail_row("Primary account", primary_email or "Not connected", "Google"),
                        _object_detail_row("Connected Google accounts", str(connected_account_total), "Google"),
                        _object_detail_row("Active Google accounts", str(active_account_total), "Google"),
                        _object_detail_row("Last refresh", str(sync.get("last_refresh_at") or "Not recorded"), "Auth"),
                    ],
                    "open": True,
                },
                {
                    "eyebrow": "Accounts",
                    "title": accounts_section_title,
                    "items": [
                        _google_account_row(
                            account,
                            return_to="/app/settings/google",
                            is_property_brand=is_property_brand,
                            verification=verification_by_binding.get(str(account.binding.binding_id or "").strip()),
                            sync_row=account_sync_by_email.get(str(account.google_email or "").strip().lower()),
                            change_row=account_change_by_binding.get(str(account.binding.binding_id or "").strip()),
                        )
                        for account in google_accounts[:3]
                    ],
                },
            ]
        else:
            object_meta = [
                {"label": "Google sign-in", "value": "Not connected"},
                {"label": "Connected Google accounts", "value": "0"},
                {"label": "Sign-in status", "value": "Ready to connect"},
            ]
            object_sidebar_rows = [
                _object_detail_row(
                    "Connect on this device",
                    "Connect Google on this device. Google opens the same account and creates it if needed.",
                    "",
                    href="/app/settings/google",
                    action_href=connect_another_href,
                    action_label="Connect Google",
                    action_method="get",
                ),
            ]
            object_sections = [
                {
                    "eyebrow": "What happens next",
                    "title": "Sign in with the same account",
                    "items": [
                        _object_detail_row(
                            "First-time sign-in",
                            "Google sign-in still creates the same PropertyQuarry account automatically.",
                            "Account",
                        ),
                        _object_detail_row(
                            "Scope",
                            "No extra inbox scope is required on this screen.",
                            "Privacy",
                        ),
                    ],
                    "open": True,
                },
            ]
    else:
        object_sidebar_rows = [
            _object_detail_row(
                connected_accounts_label,
                connected_accounts_detail,
                "Google",
                action_href=connect_another_href,
                action_label=add_account_label,
                action_method="get",
            ),
            _object_detail_row(primary_account_label, primary_email or "Not connected", "Google"),
            _object_detail_row("Last sync", str(sync.get("last_completed_at") or "Not yet completed"), "Sync"),
            _object_detail_row("Pending commitment candidates", str(sync.get("pending_commitment_candidates") or 0), "Queue"),
            _object_detail_row("Candidates covered by drafts", str(sync.get("covered_signal_candidates") or 0), "Queue"),
            _object_detail_row("Reauth reason", str(sync.get("reauth_required_reason") or "No reauth required"), "Auth"),
            _object_detail_row("Last send verification", verify_detail, "Verify"),
            _object_detail_row(
                "Next Google action",
                action["detail"],
                "Action",
                href="/app/settings/google",
                action_href=action["href"],
                action_label=action["label"],
                action_method=action["method"],
                return_to="/app/settings/google",
            ),
            _object_detail_row("Last manual sync", last_manual_sync_detail, "Action"),
            _object_detail_row("Last account change", account_change_detail, "Accounts"),
            _object_detail_row("Last emailed connect link", email_link_detail, "Email"),
        ]
        object_sections = [
            {
                "eyebrow": "Connection",
                "title": "Google binding and token state",
                "items": [
                    _object_detail_row("Connected", "Yes" if connected_account_total else "No", "Google"),
                    _object_detail_row(primary_account_label, primary_email or "Not connected", "Google"),
                    _object_detail_row(connected_accounts_label, str(connected_account_total), "Google"),
                    _object_detail_row(active_accounts_label, str(active_account_total), "Google"),
                    _object_detail_row("Token status", str(sync.get("token_status") or "missing").replace("_", " "), "Auth"),
                    _object_detail_row("Last refresh", str(sync.get("last_refresh_at") or "Not recorded"), "Auth"),
                    _object_detail_row("Reauth reason", str(sync.get("reauth_required_reason") or "No reauth required"), "Auth"),
                    _object_detail_row("Last send verification", verify_detail, "Verify"),
                    _object_detail_row("Google link", email_link_detail, "Access"),
                    _object_detail_row(
                        action["label"],
                        action["detail"],
                        "Action",
                        href="/app/settings/google",
                        action_href=action["href"],
                        action_label=action["label"],
                        action_method=action["method"],
                        return_to="/app/settings/google",
                    ),
                ],
            },
            {
                "eyebrow": "Accounts",
                "title": accounts_section_title,
                "items": [
                    _google_account_row(
                        account,
                        return_to="/app/settings/google",
                        is_property_brand=is_property_brand,
                        verification=verification_by_binding.get(str(account.binding.binding_id or "").strip()),
                        sync_row=account_sync_by_email.get(str(account.google_email or "").strip().lower()),
                        change_row=account_change_by_binding.get(str(account.binding.binding_id or "").strip()),
                    )
                    for account in google_accounts
                ]
                or [
                    _object_detail_row(
                        "No connected Google account",
                        "Attach a Google inbox before the memo, queue, and approval loop can use live workspace signals.",
                        "Empty",
                        action_href=connect_another_href,
                        action_label="Connect inbox",
                        action_method="get",
                    )
                ],
            },
            {
                "eyebrow": "Freshness",
                "title": "Latest sync run and queued commitment work",
                "items": [
                    _object_detail_row("Freshness", str(sync.get("freshness_state") or "watch").replace("_", " "), "Sync"),
                    _object_detail_row("Last completed", str(sync.get("last_completed_at") or "Not yet completed"), "Sync"),
                    _object_detail_row("Age seconds", str(sync.get("age_seconds") if sync.get("age_seconds") is not None else "n/a"), "Sync"),
                    _object_detail_row("Pending commitment candidates", str(sync.get("pending_commitment_candidates") or 0), "Queue"),
                    _object_detail_row("Candidates covered by drafts", str(sync.get("covered_signal_candidates") or 0), "Queue"),
                    _object_detail_row("Office signals ingested", str(sync.get("office_signal_ingested") or 0), "Signals"),
                ],
            },
            {
                "eyebrow": "Volume",
                "title": "What the latest sync actually pulled in",
                "items": [
                    _object_detail_row("Sync runs", str(sync.get("sync_completed") or 0), "Sync"),
                    _object_detail_row("Last synced total", str(sync.get("last_synced_total") or 0), "Signals"),
                    _object_detail_row("Last deduplicated total", str(sync.get("last_deduplicated_total") or 0), "Signals"),
                    _object_detail_row("Last suppressed total", str(sync.get("last_suppressed_total") or 0), "Signals"),
                    _object_detail_row("Gmail signals", str(sync.get("last_gmail_total") or 0), "Gmail"),
                    _object_detail_row("Calendar signals", str(sync.get("last_calendar_total") or 0), "Calendar"),
                ],
            },
        ]
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title="PropertyQuarry Google connection",
        current_nav="settings",
        console_title="Google connection",
        console_summary=(
            "Use Google to sign in with the same PropertyQuarry account, see which Google account is connected, and reconnect it if needed."
            if is_property_brand
            else "Google signal sync is visible in product language: primary sender, additional inboxes, freshness, staged work, and whether the office needs reauth before the next loop."
        ),
        object_kind="Connection" if is_property_brand else "Sync state",
        object_title=primary_email or "Google not connected",
        object_summary=sync_summary,
        object_meta=object_meta,
        object_sidebar_title="Google sign-in" if is_property_brand else "What this view answers",
        object_sidebar_copy=(
            "Use Google to open the same PropertyQuarry account on this device. No extra inbox scope is required here."
            if is_property_brand
            else "This view shows which inbox is primary, what additional Google inboxes are attached to the same workspace, when the last sync completed, and whether the office needs reauth before the next loop."
        ),
        object_sidebar_rows=object_sidebar_rows,
        object_sidebar_default_open=bool(is_property_brand and connected_account_total <= 0),
        object_sections=object_sections,
    )


@router.get("/app/settings/trust", response_class=HTMLResponse)
def settings_trust_detail(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    is_property_brand = request_brand(request)["key"] == "propertyquarry"
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    if str(context.auth_source or "").strip() != "propertyquarry_release_probe":
        product.record_surface_event(
            principal_id=context.principal_id,
            event_type="trust_opened",
            surface="settings_trust",
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    if is_property_brand:
        property_usage = _property_search_usage_state(product, principal_id=context.principal_id, access_email=str(context.access_email or ""))
        billing, commercial = _property_settings_commercial(status)
        readiness_status_label = "Ready"
        readiness_detail_label = "Core account, search, support, and access surfaces are available."
        workspace_summary = "Review the latest search, saved homes, and site health before the next decision."
        return _render_console_object_detail(
            request=request,
            context=context,
            workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
            page_title="PropertyQuarry search health",
            current_nav="settings",
            console_title="Search health",
            console_summary="Source health, recent searches, and account controls in one place.",
            object_kind="Search health",
            object_title=workspace_summary,
            object_summary=(
                f"{property_usage['ranked_total']} matches · "
                f"{property_usage['packet_ready_total']} property pages · "
                f"{property_usage['repair_status']}"
            ),
            object_meta=[
                {"label": "Matches", "value": str(property_usage["ranked_total"])},
                {"label": "Property pages", "value": str(property_usage["packet_ready_total"])},
                {"label": "3D tours", "value": str(property_usage["tour_ready_total"])},
                {"label": "Plan", "value": str(billing.get("current_plan_label") or billing.get("current_plan_key") or "Free").replace("_", " ").title()},
            ],
            object_sidebar_title="What this checks",
            object_sidebar_copy="List health, recent searches, and account controls stay visible here.",
            object_sidebar_rows=[
                _object_detail_row("Summary", workspace_summary, "Summary"),
                _object_detail_row("Account", readiness_detail_label, "Account"),
                _object_detail_row("List health", str(property_usage["repair_status"]), "Lists"),
                _object_detail_row("Search health", str(property_usage["repair_status"]), "Status"),
                _object_detail_row("List failures", str(property_usage["failed_source_total"]), "Lists"),
                _object_detail_row("Support tier", str(billing.get("support_tier") or "standard").title(), "Support"),
                _object_detail_row("Unavailable", ", ".join(str(value).replace("_", " ") for value in (commercial.get("blocked_actions") or [])[:4]) or "Nothing blocked", "Access"),
            ],
            object_sections=[
                {
                    "eyebrow": "Status",
                    "title": "Account and list health",
                    "items": [
                        _object_detail_row("Account", readiness_status_label, "Account"),
                        _object_detail_row("Details", readiness_detail_label, "Account"),
                        _object_detail_row("List health", str(property_usage.get("repair_status") or "unknown"), "Lists"),
                        _object_detail_row("Latest search", str(property_usage["latest_status"]), "Search", href=str(property_usage["latest_href"])),
                        _object_detail_row("Search health", str(property_usage["repair_status"]), "Status"),
                        _object_detail_row("List failures", str(property_usage["failed_source_total"]), "Lists"),
                    ],
                },
                {
                    "eyebrow": "Controls",
                    "title": "Settings and retention",
                    "items": [
                        _object_detail_row("Matches", str(property_usage["ranked_total"]), "Summary"),
                        _object_detail_row("Outside brief", str(property_usage["filtered_total"]), "Settings"),
                        _object_detail_row("Listings reviewed", str(property_usage["listing_total"]), "Lists"),
                        _object_detail_row("Property pages ready", str(property_usage["packet_ready_total"]), "Pages"),
                        _object_detail_row("Export data", "Download your account, searches, saved results, and preference records.", "Data", href="/app/api/property/account/export?download=1", action_href="/app/api/property/account/export?download=1", action_label="Export data", action_method="get"),
                    ],
                },
                {
                    "eyebrow": "Recent searches",
                    "title": "Recent search outcomes",
                    "items": [
                        _object_detail_row(
                            f"Search {row['run_id'][:8] or 'latest'}",
                            f"{row['status']} · {row['ranked']} ranked · {row['filtered']} filtered",
                            "Search",
                            href=row["href"],
                        )
                        for row in list(property_usage["latest_rows"])
                    ] or [_object_detail_row("No searches yet", "Launch a search to create the first search.", "Search", href="/app/search")],
                },
            ],
        )
    trust = product.workspace_trust_summary(principal_id=context.principal_id)
    readiness = dict(trust.get("readiness") or {})
    provider_posture = dict(trust.get("provider_posture") or {})
    reliability = dict(trust.get("reliability") or {})
    public_help_grounding = dict(trust.get("public_help_grounding") or {})
    recent_events = [dict(item) for item in (trust.get("recent_events") or [])]
    workspace_summary = str(trust.get("workspace_summary") or "Trust")
    if is_property_brand and any(token in workspace_summary.lower() for token in ("office loop", "memo", "memory")):
        workspace_summary = "Review the latest search, saved homes, and site health before the next decision."
    readiness_status_label = str(readiness.get("status") or "unknown").replace("_", " ")
    readiness_detail_label = str(readiness.get("detail") or "No readiness detail recorded.")
    if is_property_brand:
        readiness_status_label = "Ready" if readiness_status_label.strip().lower() not in {"failed", "blocked"} else readiness_status_label
        readiness_detail_label = "Core account, search, and support surfaces are available."
        return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title="PropertyQuarry search health",
        current_nav="settings",
        console_title="Search health",
        console_summary=(
            "Source health, recent searches, and account controls in one place."
            if is_property_brand
            else "Context, rules, readiness, provider state, and recent product events make the assistant legible when the office asks why something happened."
        ),
        object_kind="Search health",
        object_title=workspace_summary,
        object_summary=f"{trust.get('evidence_count') or 0} checks · {trust.get('rule_count') or 0} settings",
        object_meta=[
            {"label": "Checks", "value": str(trust.get("evidence_count") or 0)},
            {"label": "Settings", "value": str(trust.get("rule_count") or 0)},
            {"label": "Data retention", "value": str(trust.get("audit_retention") or "standard")},
        ],
        object_sidebar_title="What this checks",
        object_sidebar_copy="List health, recent activity, and account controls stay visible here.",
        object_sidebar_rows=[
            _object_detail_row("Summary", workspace_summary if workspace_summary != "Trust" else "No trust summary yet.", "Summary"),
            _object_detail_row("Account", readiness_detail_label, "Account"),
            _object_detail_row("List health", str(provider_posture.get("risk_state") or "unknown"), "Lists"),
            _object_detail_row("Lists", str(provider_posture.get("risk_state") or "unknown"), "Lists"),
            _object_detail_row("Delivery", str(reliability.get("delivery") or "watch"), "Delivery"),
            _object_detail_row("Access", str(reliability.get("access") or "watch"), "Access"),
            _object_detail_row("Sync", str(reliability.get("sync") or "watch"), "Sync"),
        ],
            object_sections=[
                {
                    "eyebrow": "Status",
                    "title": "Account and list health",
                    "items": [
                        _object_detail_row("Account", readiness_status_label, "Account"),
                        _object_detail_row("Details", readiness_detail_label, "Account"),
                        _object_detail_row("List health", str(provider_posture.get("risk_state") or "unknown"), "Lists"),
                        _object_detail_row("List detail", str(provider_posture.get("risk_detail") or "No list issue recorded."), "Lists"),
                        _object_detail_row("Fallback lists", str(provider_posture.get("lanes_with_fallback") or 0), "Lists"),
                    ],
                },
            {
                "eyebrow": "Controls",
                "title": "Settings and retention",
                "items": [
                    _object_detail_row("Checks", str(trust.get("evidence_count") or 0), "Checks"),
                    _object_detail_row("Settings", str(trust.get("rule_count") or 0), "Settings"),
                    _object_detail_row("Data retention", str(trust.get("audit_retention") or "standard"), "Data"),
                    _object_detail_row("Delivery", str(reliability.get("delivery") or "watch"), "Delivery"),
                    _object_detail_row("Access", str(reliability.get("access") or "watch"), "Access"),
                    _object_detail_row("Sync", str(reliability.get("sync") or "watch"), "Sync"),
                    _object_detail_row(
                        "Export data",
                        "Download your account, searches, saved results, and preference records.",
                        "Data",
                        href="/app/api/property/account/export?download=1",
                        action_href="/app/api/property/account/export?download=1",
                        action_label="Export data",
                        action_method="get",
                    ),
                ],
            },
            {
                "eyebrow": "Grounded help",
                "title": str(public_help_grounding.get("title") or "Grounded help packet"),
                "items": (
                    [
                        _object_detail_row(
                            "Summary",
                            str(public_help_grounding.get("summary") or "Help content is built from product notes and recent changes."),
                            "Grounding",
                        )
                    ]
                    + [
                        _object_detail_row(f"Point {index}", str(item), "Grounding")
                        for index, item in enumerate(list(public_help_grounding.get("bullets") or [])[:3], start=1)
                    ]
                    + [
                        _object_detail_row(
                            str(action.get("label") or "Action"),
                            _propertyquarry_href(action.get("href")) if is_property_brand else str(action.get("href") or ""),
                            "Action",
                            href=_propertyquarry_href(action.get("href")) if is_property_brand else str(action.get("href") or ""),
                            action_href=_propertyquarry_href(action.get("href")) if is_property_brand else str(action.get("href") or ""),
                            action_label=str(action.get("label") or ""),
                            action_method=str(action.get("method") or "get"),
                        )
                        for action in list(public_help_grounding.get("actions") or [])[:2]
                    ]
                    + (
                        []
                        if is_property_brand
                        else [
                            _object_detail_row(
                                str(source.get("label") or "Source"),
                                str(source.get("path") or ""),
                                str(source.get("as_of") or "Source"),
                            )
                            for source in list(public_help_grounding.get("sources") or [])[:2]
                        ]
                    )
                ),
            },
            {
                "eyebrow": "Recent product events",
                "title": "What the assistant recently did",
                "items": [
                    _object_detail_row(
                        str(item.get("label") or item.get("event_type") or "Product event").replace("_", " "),
                        str(item.get("summary") or item.get("detail") or item.get("object_title") or "No event detail recorded."),
                        str(item.get("event_type") or "event").replace("_", " ").title(),
                    )
                    for item in recent_events[:8]
                ] or [_object_detail_row("No recent product events", "Product event history will appear here as the workspace is used.", "History")],
            },
        ],
    )


@router.get("/app/settings/access", response_class=HTMLResponse)
def settings_access_detail(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    is_property_brand = request_brand(request)["key"] == "propertyquarry"
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    if str(context.auth_source or "").strip() != "propertyquarry_release_probe":
        product.record_surface_event(
            principal_id=context.principal_id,
            event_type="access_settings_opened",
            surface="settings_access",
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    if is_property_brand:
        return RedirectResponse(_property_account_redirect_target(request, settings_view="access"), status_code=303)
    active_sessions = [dict(item) for item in product.list_workspace_access_sessions(principal_id=context.principal_id, status="active", limit=50)]
    revoked_sessions = [dict(item) for item in product.list_workspace_access_sessions(principal_id=context.principal_id, status="revoked", limit=20)]
    total_opens = sum(
        1
        for item in product.list_office_events(principal_id=context.principal_id, limit=200)
        if str(item.get("event_type") or "").strip() == "workspace_access_session_opened"
    )
    issue_status = str(request.query_params.get("issue_status") or "").strip()
    issue_email = str(request.query_params.get("issue_email") or "").strip()
    issue_error = str(request.query_params.get("issue_error") or "").strip()
    access_status = str(request.query_params.get("access_status") or "").strip()
    access_email = str(request.query_params.get("access_email") or "").strip()
    access_detail = (
        issue_error
        or (
            f"Access link issued for {issue_email}"
            if issue_status == "issued" and issue_email
            else "Issue and revoke secure account links from this page."
        )
    )
    visible_active_sessions = active_sessions[:3] if is_property_brand else active_sessions[:12]
    hidden_active_total = max(0, len(active_sessions) - len(visible_active_sessions))
    visible_revoked_sessions = revoked_sessions[:3] if is_property_brand else revoked_sessions[:8]
    access_object_title = "Access links" if is_property_brand else f"{len(active_sessions)} active sessions"
    access_object_summary = (
        f"{len(active_sessions)} active · {len(revoked_sessions)} revoked"
        if is_property_brand
        else f"{total_opens} access opens recorded · {len(revoked_sessions)} revoked sessions"
    )
    active_access_items = [
        _object_detail_row(
            str(item.get("email") or "unknown"),
            (
                (
                    "collaborator"
                    if is_property_brand and str(item.get("role") or "principal").strip() == "operator"
                    else ("account owner" if is_property_brand else str(item.get("role") or "principal").replace("_", " "))
                )
                + f" · {('/app/agents' if is_property_brand and str(item.get('default_target') or '') == '/admin/office' else str(item.get('default_target') or '/app/properties'))} · expires {str(item.get('expires_at') or '')[:19] or 'n/a'}"
            ),
            "" if is_property_brand else str(item.get("source_kind") or "workspace_access").replace("_", " ").title(),
            action_href=f"/app/actions/access-sessions/{urllib.parse.quote(str(item.get('session_id') or '').strip(), safe='')}/revoke",
            action_label="Revoke",
            action_method="post",
            return_to="/app/settings/access",
            secondary_action_href=str(item.get("access_url") or "").strip(),
            secondary_action_label="Open link" if str(item.get("access_url") or "").strip() else "",
            secondary_action_method="get",
        )
        for item in visible_active_sessions
    ]
    if hidden_active_total:
        active_access_items.append(
            _object_detail_row(
                "More active links",
                f"{hidden_active_total} additional active access link{'s' if hidden_active_total != 1 else ''}. Use search or revoke from the full ledger when needed.",
                "" if is_property_brand else "Hidden",
            )
        )
    if not active_access_items:
        active_access_items = [_object_detail_row("No active access sessions", "Issue an account access link when someone needs direct entry into PropertyQuarry.", "" if is_property_brand else "Clear")]
    revoked_section_items = [
        _object_detail_row(
            str(item.get("email") or "unknown"),
            f"revoked by {str(item.get('revoked_by') or 'workspace')} · {str(item.get('revoked_at') or '')[:19] or 'n/a'}",
            "" if is_property_brand else "Revoked",
        )
        for item in visible_revoked_sessions
    ]
    access_sections = [
        {
            "eyebrow": "Active sessions",
            "title": "Live access links",
            "items": active_access_items,
            "open": True,
        },
    ]
    if revoked_section_items or not is_property_brand:
        access_sections.append(
            {
                "eyebrow": "Recently revoked",
                "title": "Sessions that no longer authenticate",
                "items": revoked_section_items
                or [
                    _object_detail_row(
                        "No revoked sessions",
                        "Revoked links and sessions will appear here when access is withdrawn.",
                        "History",
                    )
                ],
            }
        )
    access_sidebar_rows: list[dict[str, str]] = []
    if not is_property_brand:
        access_sidebar_rows = [
            _object_detail_row("Active links", str(len(active_sessions)), "Access"),
            _object_detail_row("Latest access action", access_detail, "Access"),
        ]
        access_sidebar_rows.insert(1, _object_detail_row("Revoked links", str(len(revoked_sessions)), "Access"))
        access_sidebar_rows.append(
            _object_detail_row(
                "Default operator target",
                "/admin/office",
                "Access",
            )
        )
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title="PropertyQuarry access",
        current_nav="settings",
        console_title="Access",
        console_summary="Give someone access, check who still has it, and revoke links without exposing an operational ledger first.",
        object_kind="Access",
        object_title=access_object_title,
        object_summary=access_object_summary,
        object_meta=[
            {"label": "Active links" if is_property_brand else "Active sessions", "value": str(len(active_sessions))},
            *([] if is_property_brand else [{"label": "Access opens", "value": str(total_opens)}]),
            {"label": "Revoked links" if is_property_brand else "Revoked sessions", "value": str(len(revoked_sessions))},
            {
                "label": "Role mix",
                "value": ", ".join(
                    sorted(
                        {
                            ("collaborator" if is_property_brand and str(item.get("role") or "principal") == "operator" else str(item.get("role") or "principal"))
                            for item in active_sessions
                        }
                        or {"account owner" if is_property_brand else "principal"}
                    )
                ),
            },
        ],
        object_sidebar_title="Access control",
        object_sidebar_copy="Create one clean link, then revoke it when the account no longer needs that entry point.",
        object_sidebar_rows=access_sidebar_rows,
        object_sections=access_sections,
        object_sidebar_form={
            "action": "/app/actions/access-sessions/issue",
            "method": "post",
            "eyebrow": "Issue access",
            "title": "Create an access link",
            "copy": "Issue a direct account or collaborator access link without dropping into the API.",
            "open": bool(issue_status or issue_error or not active_sessions),
            "summary_label": "New",
            "submit_label": "Issue access link",
            "fields": [
                {"type": "hidden", "name": "return_to", "value": "/app/settings/access"},
                {"label": "Email", "name": "email", "type": "email", "value": issue_email, "placeholder": "principal@example.com"},
                {
                    "label": "Role",
                    "name": "role",
                    "type": "select",
                    "value": "principal",
                    "options": [
                        {"label": "Account owner" if is_property_brand else "Principal", "value": "principal", "selected": True},
                        {"label": "Collaborator" if is_property_brand else "Operator", "value": "operator"},
                    ],
                },
                {"label": "Display name", "name": "display_name", "type": "text", "value": "", "placeholder": "Workspace entry"},
            ],
        },
    )


@router.get("/app/settings/invitations", response_class=HTMLResponse)
def settings_invitations_detail(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    is_property_brand = request_brand(request)["key"] == "propertyquarry"
    status = container.onboarding.status(principal_id=context.principal_id)
    workspace = dict(status.get("workspace") or {})
    product = build_product_service(container)
    if str(context.auth_source or "").strip() != "propertyquarry_release_probe":
        product.record_surface_event(
            principal_id=context.principal_id,
            event_type="invitations_opened",
            surface="settings_invitations",
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
        )
    pending = [dict(item) for item in product.list_workspace_invitations(principal_id=context.principal_id, status="pending", limit=50)]
    accepted = [dict(item) for item in product.list_workspace_invitations(principal_id=context.principal_id, status="accepted", limit=20)]
    revoked = [dict(item) for item in product.list_workspace_invitations(principal_id=context.principal_id, status="revoked", limit=20)]
    delivery_rows = [*pending, *accepted, *revoked]
    delivery_failed = sum(1 for item in delivery_rows if str(item.get("email_delivery_status") or "").strip() == "failed")
    delivery_sent = sum(1 for item in delivery_rows if str(item.get("email_delivery_status") or "").strip() == "sent")
    invite_status = str(request.query_params.get("invite_status") or "").strip()
    invite_email = str(request.query_params.get("invite_email") or "").strip()
    invite_error = str(request.query_params.get("invite_error") or "").strip()

    def _propertyquarry_role_label(role: object) -> str:
        normalized = str(role or "").strip().lower()
        if is_property_brand:
            if normalized == "operator":
                return "collaborator"
            if normalized == "principal":
                return "account owner"
        return normalized.replace("_", " ") or ("collaborator" if is_property_brand else "operator")

    invite_action_detail = (
        invite_error
        or (
            f"Invitation created for {invite_email}"
            if invite_status == "created" and invite_email
            else "Create and revoke workspace invitations from this page."
        )
    )
    revoked_email = str(request.query_params.get("revoked_email") or "").strip()
    return _render_console_object_detail(
        request=request,
        context=context,
        workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
        page_title="PropertyQuarry invitations",
        current_nav="settings",
        console_title="Invitations",
        console_summary=(
            "Pending invites, accepted collaborators, and revoked access stay visible where PropertyQuarry manages shared searches."
            if is_property_brand
            else "Pending invites, accepted roles, and revoked access stay visible where the workspace decides who joins the office loop."
        ),
        object_kind="Invitations",
        object_title=f"{len(pending)} pending invitations",
        object_summary=f"{len(accepted)} accepted · {len(revoked)} revoked",
        object_meta=[
            {"label": "Pending", "value": str(len(pending))},
            {"label": "Accepted", "value": str(len(accepted))},
            {"label": "Revoked", "value": str(len(revoked))},
            {"label": "Delivery failures", "value": str(delivery_failed)},
        ],
        object_sidebar_title="What invitation control answers",
        object_sidebar_copy="Invitation control shows who is waiting to join, which role they will enter with, and whether an old invite still needs to be withdrawn.",
        object_sidebar_rows=[
            _object_detail_row("Pending invitations", str(len(pending)), "Invites"),
            _object_detail_row("Accepted invitations", str(len(accepted)), "Access"),
            _object_detail_row("Revoked invitations", str(len(revoked)), "Invites"),
            _object_detail_row("Invite emails sent", str(delivery_sent), "Delivery"),
            _object_detail_row("Invite email failures", str(delivery_failed), "Delivery"),
            _object_detail_row(
                "Collaborator access policy" if is_property_brand else "Operator seat policy",
                "Collaborator limits are enforced at acceptance time." if is_property_brand else "Operator seats are enforced at acceptance time.",
                "Access" if is_property_brand else "Seats",
            ),
            _object_detail_row("Latest invite action", invite_action_detail, "Invites"),
            _object_detail_row(
                "Latest revoke action",
                f"Revoked invitation for {revoked_email}" if invite_status == "revoked" and revoked_email else "No invite revocation recorded from this view.",
                "Invites",
            ),
        ],
        object_sections=[
            {
                "eyebrow": "Pending",
                "title": "Invites waiting for acceptance",
                "items": [
                    _object_detail_row(
                        str(item.get("email") or "unknown"),
                        " · ".join(
                            part
                            for part in (
                                _propertyquarry_role_label(item.get("role") or "operator"),
                                f"delivery {str(item.get('email_delivery_status') or 'not attempted').replace('_', ' ')}",
                                f"expires {str(item.get('expires_at') or '')[:19] or 'n/a'}",
                            )
                            if part
                        ),
                        "Pending",
                        action_href=f"/app/actions/invitations/{urllib.parse.quote(str(item.get('invitation_id') or '').strip(), safe='')}/revoke",
                        action_label="Revoke",
                        action_method="post",
                        return_to="/app/settings/invitations",
                        secondary_action_href=str(item.get("invite_url") or "").strip(),
                        secondary_action_label="Open invite" if str(item.get("invite_url") or "").strip() else "",
                        secondary_action_method="get",
                    )
                    for item in pending[:12]
                ] or [
                    _object_detail_row(
                        "No pending invitations",
                        "Create an invite when another collaborator should help review saved searches.",
                        "Clear",
                    )
                    if is_property_brand
                    else _object_detail_row("No pending invitations", "Create an invite when the workspace needs another reviewer or operator.", "Clear")
                ],
            },
            {
                "eyebrow": "Accepted",
                "title": "People who already joined through an invite",
                "items": [
                    _object_detail_row(
                        str(item.get("email") or "unknown"),
                        " · ".join(
                            part
                            for part in (
                                _propertyquarry_role_label(item.get("role") or "operator"),
                                f"accepted {str(item.get('accepted_at') or '')[:19] or 'n/a'}",
                                f"delivery {str(item.get('email_delivery_status') or 'not attempted').replace('_', ' ')}",
                            )
                            if part
                        ),
                        "Accepted",
                    )
                    for item in accepted[:8]
                ] or [_object_detail_row("No accepted invitations", "Accepted invitations will appear here after the recipient uses the secure invite link.", "History")],
            },
            {
                "eyebrow": "Revoked",
                "title": "Invitations that no longer grant access",
                "items": [
                    _object_detail_row(
                        str(item.get("email") or "unknown"),
                        " · ".join(
                            part
                            for part in (
                                _propertyquarry_role_label(item.get("role") or "operator"),
                                f"revoked {str(item.get('revoked_at') or '')[:19] or 'n/a'}",
                                f"delivery {str(item.get('email_delivery_status') or 'not attempted').replace('_', ' ')}",
                            )
                            if part
                        ),
                        "Revoked",
                    )
                    for item in revoked[:8]
                ] or [_object_detail_row("No revoked invitations", "Revoked invitations will appear here when a pending invite is withdrawn.", "History")],
            },
        ],
        object_sidebar_form={
            "action": "/app/actions/invitations/create",
            "method": "post",
            "eyebrow": "Create invite",
            "title": "Invite another person",
            "copy": (
                "Create an account-owner or collaborator invite without leaving this page."
                if is_property_brand
                else "Create a principal or operator invitation without leaving the product surface."
            ),
            "open": bool(invite_error or (not pending and not accepted and not revoked)),
            "submit_label": "Create invitation",
            "fields": [
                {"type": "hidden", "name": "return_to", "value": "/app/settings/invitations"},
                {
                    "label": "Email",
                    "name": "email",
                    "type": "email",
                    "value": invite_email,
                    "placeholder": "collaborator@example.com" if is_property_brand else "operator@example.com",
                },
                {
                    "label": "Role",
                    "name": "role",
                    "type": "select",
                    "value": "operator",
                    "options": [
                        {"label": "Collaborator" if is_property_brand else "Operator", "value": "operator", "selected": True},
                        {"label": "Account owner" if is_property_brand else "Principal", "value": "principal"},
                    ],
                },
                {
                    "label": "Display name",
                    "name": "display_name",
                    "type": "text",
                    "value": "",
                    "placeholder": "Collaborator" if is_property_brand else "Operator One",
                },
            ],
        },
    )


@router.post("/app/actions/invitations/create")
async def app_create_workspace_invitation(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    body = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    return_to = _normalize_browser_return_to(_form_value(body, "return_to", "/app/settings/invitations"), default="/app/settings/invitations")
    email = str(_form_value(body, "email", "")).strip().lower()
    role = str(_form_value(body, "role", "operator")).strip().lower() or "operator"
    display_name = str(_form_value(body, "display_name", "")).strip()
    product = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    try:
        product.create_workspace_invitation(
            principal_id=context.principal_id,
            email=email,
            role=role,
            display_name=display_name,
            invited_by=actor,
        )
    except Exception as exc:
        error_value = str(exc or "workspace_invitation_create_failed").strip()
        return RedirectResponse(
            _browser_return_to_with_params(
                return_to,
                invite_error=error_value,
                invite_email=email,
            ),
            status_code=303,
        )
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="workspace_invitation_requested",
        surface="settings_invitations",
        actor=actor,
        metadata={"email": email, "role": role},
    )
    return RedirectResponse(
        _browser_return_to_with_params(
            return_to,
            invite_status="created",
            invite_email=email,
        ),
        status_code=303,
    )


@router.post("/app/actions/invitations/{invitation_id}/revoke")
async def app_revoke_workspace_invitation(
    invitation_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    body = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    return_to = _normalize_browser_return_to(_form_value(body, "return_to", "/app/settings/invitations"), default="/app/settings/invitations")
    product = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    current = product.get_workspace_invitation(principal_id=context.principal_id, invitation_id=invitation_id)
    if current is None:
        error_value = "workspace_invitation_not_found"
        return RedirectResponse(_browser_return_to_with_params(return_to, invite_error=error_value), status_code=303)
    revoked = product.revoke_workspace_invitation(
        principal_id=context.principal_id,
        invitation_id=invitation_id,
        actor=actor,
    )
    if revoked is None:
        error_value = "workspace_invitation_not_found"
        return RedirectResponse(_browser_return_to_with_params(return_to, invite_error=error_value), status_code=303)
    email = str(revoked.get("email") or current.get("email") or "").strip().lower()
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="workspace_invitation_revocation_requested",
        surface="settings_invitations",
        actor=actor,
        metadata={"invitation_id": invitation_id, "email": str(revoked.get("email") or current.get("email") or "").strip().lower()},
    )
    return RedirectResponse(
        _browser_return_to_with_params(return_to, invite_status="revoked", revoked_email=email),
        status_code=303,
    )


@router.post("/app/actions/access-sessions/issue")
async def app_issue_workspace_access_session(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    body = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    return_to = _normalize_browser_return_to(_form_value(body, "return_to", "/app/settings/access"), default="/app/settings/access")
    email = str(_form_value(body, "email", "")).strip().lower()
    role = str(_form_value(body, "role", "principal")).strip().lower() or "principal"
    display_name = str(_form_value(body, "display_name", "")).strip()
    is_property_brand = request_brand(request)["key"] == "propertyquarry"
    product = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    try:
        product.issue_workspace_access_session(
            principal_id=context.principal_id,
            email=email,
            role=role,
            display_name=display_name,
            source_kind="settings_access",
            default_target="/app/agents" if is_property_brand and role == "operator" else "",
        )
    except Exception as exc:
        error_value = str(exc or "workspace_access_issue_failed").strip()
        return RedirectResponse(
            _browser_return_to_with_params(
                return_to,
                issue_error=error_value,
                issue_email=email,
            ),
            status_code=303,
        )
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="workspace_access_requested",
        surface="settings_access",
        actor=actor,
        metadata={"email": email, "role": role},
    )
    return RedirectResponse(
        _browser_return_to_with_params(
            return_to,
            issue_status="issued",
            issue_email=email,
        ),
        status_code=303,
    )


@router.post("/app/actions/access-sessions/{session_id}/revoke")
async def app_revoke_workspace_access_session(
    session_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    body = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    return_to = _normalize_browser_return_to(_form_value(body, "return_to", "/app/settings/access"), default="/app/settings/access")
    product = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    current = product.get_workspace_access_session(principal_id=context.principal_id, session_id=session_id)
    if current is None:
        error_value = "workspace_access_session_not_found"
        return RedirectResponse(_browser_return_to_with_params(return_to, issue_error=error_value), status_code=303)
    revoked = product.revoke_workspace_access_session(
        principal_id=context.principal_id,
        session_id=session_id,
        actor=actor,
    )
    if revoked is None:
        error_value = "workspace_access_session_not_found"
        return RedirectResponse(_browser_return_to_with_params(return_to, issue_error=error_value), status_code=303)
    email = str(revoked.get("email") or current.get("email") or "").strip().lower()
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="workspace_access_revocation_requested",
        surface="settings_access",
        actor=actor,
        metadata={"session_id": session_id, "email": str(revoked.get("email") or current.get("email") or "").strip().lower()},
    )
    return RedirectResponse(
        _browser_return_to_with_params(return_to, access_status="revoked", access_email=email),
        status_code=303,
    )


@router.get("/app/actions/signals/google/sync")
def app_google_signal_sync(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    product = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    return_to = _normalize_browser_return_to(request.query_params.get("return_to"), default="/app/settings/google")
    diagnostics = product.workspace_diagnostics(principal_id=context.principal_id)
    sync = dict(dict(diagnostics.get("analytics") or {}).get("sync") or {})
    if not bool(sync.get("google_workspace_sync_supported")):
        return RedirectResponse(_browser_return_to_with_params(return_to, sync_error="google_identity_only"), status_code=303)
    try:
        product.sync_google_workspace_signals(
            principal_id=context.principal_id,
            actor=actor,
            email_limit=5,
            calendar_limit=5,
        )
    except RuntimeError as exc:
        error_value = str(exc or "google_sync_failed").strip()
        return RedirectResponse(_browser_return_to_with_params(return_to, sync_error=error_value), status_code=303)
    return RedirectResponse(_browser_return_to_with_params(return_to, sync_status="completed"), status_code=303)


@router.api_route("/app/actions/google/connect", methods=["GET", "HEAD"], include_in_schema=False)
def app_google_connect(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    return_to = _normalize_browser_return_to(request.query_params.get("return_to"), default="/app/settings/google")
    scope_bundle = str(request.query_params.get("scope_bundle") or "identity").strip() or "identity"
    try:
        started = container.onboarding.start_google(
            principal_id=context.principal_id,
            scope_bundle=scope_bundle,
            redirect_uri_override=f"{_public_app_base_url(request)}/google/callback",
            return_to=return_to,
            browser_source="settings_google",
        )
    except RuntimeError as exc:
        error_value = str(exc or "google_connect_failed").strip()
        return RedirectResponse(_browser_return_to_with_params(return_to, google_error=error_value), status_code=303)
    google_start = dict(started.get("google_start") or {})
    auth_url = str(google_start.get("auth_url") or google_start.get("start_url") or "").strip()
    if bool(google_start.get("ready")) and auth_url:
        return RedirectResponse(auth_url, status_code=303)
    detail = str(google_start.get("detail") or "google_oauth_not_ready").strip()
    return RedirectResponse(_browser_return_to_with_params(return_to, google_error=detail), status_code=303)


@router.api_route("/app/actions/id-austria/connect", methods=["GET", "HEAD"], include_in_schema=False)
def app_id_austria_connect(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    return_to = _normalize_browser_return_to(request.query_params.get("return_to"), default="/app/account")
    if not _request_is_austrian_ip(request):
        return RedirectResponse(
            _browser_return_to_with_params(return_to, id_austria_error="id_austria_austria_ip_required"),
            status_code=303,
        )
    try:
        packet = id_austria_service.build_id_austria_oidc_start(
            principal_id=context.principal_id,
            redirect_uri_override=f"{_public_app_base_url(request)}/id-austria/callback",
            return_to=return_to,
            browser_source="settings_id_austria",
        )
    except RuntimeError as exc:
        error_value = str(exc or "id_austria_connect_failed").strip()
        return RedirectResponse(_browser_return_to_with_params(return_to, id_austria_error=error_value), status_code=303)
    return RedirectResponse(str(packet.auth_url), status_code=303)


@router.api_route("/app/actions/facebook/connect", methods=["GET", "HEAD"], include_in_schema=False)
def app_facebook_connect(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    return_to = _normalize_browser_return_to(request.query_params.get("return_to"), default="/app/account")
    try:
        packet = build_facebook_oauth_start(
            principal_id=context.principal_id,
            redirect_uri_override=f"{_public_app_base_url(request)}/facebook/callback",
            return_to=return_to,
            browser_source="settings_facebook",
        )
    except RuntimeError as exc:
        error_value = str(exc or "facebook_connect_failed").strip()
        return RedirectResponse(_browser_return_to_with_params(return_to, facebook_error=error_value), status_code=303)
    return RedirectResponse(str(packet.auth_url), status_code=303)


@router.post("/app/actions/google/email-connect-link")
async def app_google_email_connect_link(
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    body = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    query = request.query_params
    return_to = _normalize_browser_return_to(
        _form_value(body, "return_to", str(query.get("return_to") or "/app/settings/google")),
        default="/app/settings/google",
    )
    recipient_email = (
        _form_value(body, "recipient_email", "")
        or str(query.get("recipient_email") or "").strip()
        or _google_connect_email_recipient(
            principal_id=context.principal_id,
            access_email=str(context.access_email or ""),
        )
    )
    scope_bundle = _form_value(body, "scope_bundle", str(query.get("scope_bundle") or "identity"))
    product = build_product_service(container)
    try:
        result = product.send_google_connect_email_link(
            principal_id=context.principal_id,
            recipient_email=recipient_email,
            scope_bundle=scope_bundle,
            base_url=_public_app_base_url(request),
        )
    except (RuntimeError, ValueError) as exc:
        error_value = str(exc or "google_connect_email_failed").strip()
        return RedirectResponse(
            _browser_return_to_with_params(
                return_to,
                email_link_error=error_value,
                email_link_email=str(recipient_email or "").strip().lower(),
            ),
            status_code=303,
        )
    return RedirectResponse(
        _browser_return_to_with_params(
            return_to,
            email_link_status="sent",
            email_link_email=str(result.get("recipient_email") or "").strip().lower(),
            email_link_bundle=str(result.get("scope_bundle") or "").strip(),
        ),
        status_code=303,
    )


@router.post("/app/actions/google/accounts/{binding_id:path}/make-primary")
async def app_google_make_primary(
    binding_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    body = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    return_to = _normalize_browser_return_to(_form_value(body, "return_to", "/app/settings/google"), default="/app/settings/google")
    product = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    try:
        account = google_oauth_service.promote_google_account(
            container=container,
            principal_id=context.principal_id,
            binding_id=binding_id,
        )
    except RuntimeError as exc:
        error_value = str(exc or "google_account_promotion_failed").strip()
        return RedirectResponse(_browser_return_to_with_params(return_to, google_error=error_value), status_code=303)
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="google_account_primary_updated",
        surface="settings_google",
        actor=actor,
        metadata={
            "binding_id": str(account.binding.binding_id or "").strip(),
            "google_email": str(account.google_email or "").strip(),
            "google_subject": str(account.google_subject or "").strip(),
        },
    )
    return RedirectResponse(_browser_return_to_with_params(return_to, account_status="primary_updated"), status_code=303)


@router.post("/app/actions/google/accounts/{binding_id:path}/disconnect")
async def app_google_disconnect_account(
    binding_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    body = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    return_to = _normalize_browser_return_to(_form_value(body, "return_to", "/app/settings/google"), default="/app/settings/google")
    product = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    try:
        binding = google_oauth_service.disconnect_google_account(
            container=container,
            principal_id=context.principal_id,
            binding_id=binding_id,
        )
    except RuntimeError as exc:
        error_value = str(exc or "google_account_disconnect_failed").strip()
        return RedirectResponse(_browser_return_to_with_params(return_to, google_error=error_value), status_code=303)
    metadata = dict(binding.auth_metadata_json or {})
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="google_account_disconnected",
        surface="settings_google",
        actor=actor,
        metadata={
            "binding_id": str(binding.binding_id or "").strip(),
            "google_email": str(metadata.get("google_email") or "").strip(),
            "google_subject": str(metadata.get("google_subject") or "").strip(),
        },
    )
    return RedirectResponse(
        _browser_return_to_with_params(return_to, account_status="account_disconnected"),
        status_code=303,
    )


@router.post("/app/actions/google/accounts/{binding_id:path}/verify-send")
async def app_google_verify_send(
    binding_id: str,
    request: Request,
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
) -> RedirectResponse:
    body = urllib.parse.parse_qs((await request.body()).decode("utf-8", errors="ignore"), keep_blank_values=True)
    return_to = _normalize_browser_return_to(_form_value(body, "return_to", "/app/settings/google"), default="/app/settings/google")
    product = build_product_service(container)
    actor = str(context.operator_id or context.access_email or context.principal_id or "browser").strip()
    try:
        result = google_oauth_service.run_google_gmail_smoke_test(
            container=container,
            principal_id=context.principal_id,
            binding_id=binding_id,
        )
    except RuntimeError as exc:
        product.record_surface_event(
            principal_id=context.principal_id,
            event_type="google_send_verification_failed",
            surface="settings_google",
            actor=actor,
            metadata={
                "binding_id": binding_id,
                "error": str(exc or "google_send_verification_failed").strip(),
            },
        )
        error_value = str(exc or "google_send_verification_failed").strip()
        return RedirectResponse(_browser_return_to_with_params(return_to, verify_error=error_value), status_code=303)
    product.record_surface_event(
        principal_id=context.principal_id,
        event_type="google_send_verification_completed",
        surface="settings_google",
        actor=actor,
        metadata={
            "binding_id": str(result.binding.binding_id or "").strip(),
            "sender_email": str(result.sender_email or "").strip(),
            "recipient_email": str(result.recipient_email or "").strip(),
            "google_email": str(dict(result.binding.auth_metadata_json or {}).get("google_email") or result.sender_email or "").strip(),
            "google_subject": str(dict(result.binding.auth_metadata_json or {}).get("google_subject") or "").strip(),
            "gmail_message_id": str(result.gmail_message_id or "").strip(),
        },
    )
    sender = str(result.sender_email or "").strip()
    recipient = str(result.recipient_email or "").strip()
    return RedirectResponse(
        _browser_return_to_with_params(
            return_to,
            verify_status="completed",
            verify_sender=sender,
            verify_recipient=recipient,
        ),
        status_code=303,
    )


@router.get("/app/search", response_class=HTMLResponse)
def app_search(
    request: Request,
    query: str = Query(default=""),
    limit: int = Query(default=20, ge=1, le=100),
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
    container: AppContainer = Depends(get_container),
    context: RequestContext = Depends(get_request_context),
    access_identity: CloudflareAccessIdentity | None = Depends(get_cloudflare_access_identity),
) -> HTMLResponse:
    if request_brand(request)["key"] == "propertyquarry":
        return _app_shell(
            section="search",
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
    workspace = dict(container.onboarding.status(principal_id=context.principal_id).get("workspace") or {})
    product = build_product_service(container)
    normalized_query = str(query or "").strip()
    items = list(
        product.search_workspace(
            principal_id=context.principal_id,
            query=normalized_query,
            limit=limit,
            operator_id=str(context.operator_id or "").strip(),
        )
    ) if normalized_query else []
    if normalized_query:
        search_return_to = f"/app/search?{urllib.parse.urlencode({'query': normalized_query, 'limit': limit})}"
        items = [
            {
                **item,
                **({"return_to": search_return_to} if str(item.get("action_href") or "").strip() else {}),
            }
            for item in items
        ]
    if normalized_query:
        product.record_surface_event(
            principal_id=context.principal_id,
            event_type="workspace_search_opened",
            surface="search_browser",
            actor=str(context.operator_id or context.access_email or context.principal_id or "browser").strip(),
            metadata={"query": normalized_query[:80], "result_total": len(items)},
        )
    kind_counts: dict[str, int] = {}
    grouped: dict[str, list[dict[str, object]]] = {}
    for item in items:
        kind = str(item.get("kind") or "workspace").strip() or "workspace"
        kind_counts[kind] = int(kind_counts.get(kind) or 0) + 1
        grouped.setdefault(kind, []).append(item)
    primary_items = items[:12]
    primary_keys = {_search_item_key(item) for item in primary_items}
    stats = [
        {"label": "Results", "value": str(len(items))},
        {"label": "People", "value": str(kind_counts.get("person") or 0)},
        {"label": "Decisions", "value": str(kind_counts.get("decision") or 0)},
        {"label": "Commitments", "value": str(kind_counts.get("commitment") or 0)},
        {"label": "Deadlines", "value": str(kind_counts.get("deadline") or 0)},
    ] if normalized_query else []
    cards = [
        {
            "eyebrow": "Workspace search",
            "title": f"Results for “{normalized_query}”" if normalized_query else "Search",
            "body": (
                f"{len(items)} results across people, threads, commitments, decisions, deadlines, context, and rules."
                if normalized_query
                else "Search people, threads, commitments, decisions, deadlines, context, rules, and handoffs."
            ),
            "items": primary_items if normalized_query else [
                {
                    "title": "Start with a name or topic",
                    "detail": "Search Sofia, board, investor, renewal, or a commitment title.",
                    "tag": "Hint",
                },
                {
                    "title": "Open the next record directly",
                    "detail": "Results keep their native actions when the underlying record supports them.",
                    "tag": "Action",
                },
            ],
        },
        {
            "eyebrow": "How to use it",
            "title": "Jump to the record you need",
            "body": "Use a concrete name, topic, or record label. Open the result and continue from there.",
            "items": (
                [
                    {
                        "title": f"{kind.title()} results",
                        "detail": f"{count} matched item{'s' if count != 1 else ''}.",
                        "tag": "Kind",
                    }
                    for kind, count in sorted(kind_counts.items(), key=lambda item: (-item[1], item[0]))[:6]
                ]
                if normalized_query
                else [
                    {"title": "People", "detail": "Search names, roles, themes, or relationship signals.", "tag": "Kind"},
                    {"title": "Decisions and deadlines", "detail": "Search a board item, commitment, due task, or review object directly.", "tag": "Kind"},
                    {"title": "Context and rules", "detail": "Search the explanation layer when you need to check why something happened.", "tag": "Kind"},
                ]
            ),
        },
    ]
    if normalized_query:
        for kind, rows in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))[:4]:
            overflow_rows = [row for row in rows if _search_item_key(row) not in primary_keys][:6]
            if not overflow_rows:
                continue
            cards.append(
                {
                    "eyebrow": "Kind slice",
                    "title": f"{kind.title()} matches",
                    "body": f"Top {kind} hits for “{normalized_query}”.",
                    "items": overflow_rows,
                }
            )
    return _render_public_template(
        request,
        "console_shell.html",
        **_console_shell_context(
            request=request,
            page_title="PropertyQuarry account search",
            current_nav="settings",
            context=context,
            console_title="Search",
            console_summary="Search the workspace and open the next record directly.",
            nav_groups=app_nav_groups_for_brand(request_brand(request)["key"]),
            workspace_label=str(workspace.get("name") or "PropertyQuarry account"),
            cards=cards,
            stats=stats,
            console_form={
                "method": "get",
                "action": "/app/search",
                "submit_label": "Search",
                "fields": [
                    {
                        "type": "text",
                        "name": "query",
                        "label": "Search",
                        "value": normalized_query,
                        "placeholder": "Sofia, board, investor, renewal",
                    },
                    {
                        "type": "number",
                        "name": "limit",
                        "label": "Limit",
                        "value": str(limit),
                        "min": "1",
                        "max": "100",
                    },
                ],
            },
        ),
    )
