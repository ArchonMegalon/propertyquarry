from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import hmac
import json
import os
import re
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Iterable
from uuid import uuid4

from app.product.privacy_lifecycle_storage import (
    find_privacy_request_by_idempotency,
    get_privacy_request_record,
    list_privacy_request_records,
    privacy_idempotency_key,
    privacy_subject_key,
    put_privacy_request_record,
)
from app.product.property_tour_hosting import (
    list_hosted_property_tours_for_principal,
    revoke_hosted_property_tour_bundle,
)
from app.product.service import build_product_service
from app.services.fliplink import build_fliplink_packet_service
from app.services.property_content_job_ledger import PropertyContentJobLedger
from app.settings import resolve_signing_secret

if TYPE_CHECKING:
    from app.container import AppContainer


_COLLECTION_ORDER = (
    "account_profile",
    "search_preferences",
    "saved_shortlist",
    "preference_profile",
    "searches",
    "research_packets",
    "property_content_studio",
    "workspace_sessions",
    "tours_and_private_receipts",
    "artifacts",
    "packet_publications",
    "packet_events",
    "provider_bindings",
    "delivery_logs",
    "events",
    "privacy_requests",
)
_BLOCKED_KEYS = frozenset(
    {
        "access_token",
        "access_token_hash",
        "access_launch_token",
        "access_launch_token_hash",
        "refresh_token",
        "token",
        "token_hash",
        "api_key",
        "secret",
        "client_secret",
        "password",
        "cookie",
        "authorization",
        "auth_context_json",
        "raw_payload_uri",
        "embed_code",
        "private_key",
        "credential",
        "credentials",
    }
)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "auth",
        "authorization",
        "code",
        "credential",
        "key",
        "password",
        "refresh_token",
        "secret",
        "signature",
        "sig",
        "token",
    }
)
_SIGNED_PATH_RE = re.compile(
    r"(?P<prefix>/(?:workspace-access|workspace-invites|channel-loop/deliveries)/)[^\s?#/]+",
    flags=re.IGNORECASE,
)
_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", flags=re.IGNORECASE)


class PrivacyCursorError(ValueError):
    pass


class PrivacyLifecycleConflict(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    observed = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return observed.astimezone(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _privacy_secret(container: "AppContainer") -> str:
    settings = getattr(container, "settings", None)
    configured = str(
        os.getenv("PROPERTYQUARRY_PRIVACY_EXPORT_SECRET")
        or os.getenv("EA_SIGNING_SECRET")
        or getattr(settings, "api_token", "")
        or ""
    ).strip()
    if configured:
        return configured
    try:
        resolved = str(resolve_signing_secret(settings, purpose="propertyquarry-privacy-lifecycle") or "").strip()
    except Exception:
        resolved = ""
    if resolved:
        return resolved
    if str(getattr(settings, "runtime_mode", "") or "").strip().lower() == "prod":
        raise RuntimeError("propertyquarry_privacy_lifecycle_secret_required")
    return "propertyquarry-local-privacy-cursor"


def _database_url(container: "AppContainer") -> str:
    return str(getattr(getattr(container, "settings", None), "database_url", "") or "").strip()


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return "[redacted-url]"
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return _SIGNED_PATH_RE.sub(r"\g<prefix>[REDACTED]", raw)
    hostname = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    netloc = f"{hostname}{port}"
    query: list[tuple[str, str]] = []
    for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if str(key or "").strip().lower() in _SENSITIVE_QUERY_KEYS:
            query.append((key, "[REDACTED]"))
        else:
            query.append((key, item[:500]))
    path = _SIGNED_PATH_RE.sub(r"\g<prefix>[REDACTED]", parsed.path)
    return urllib.parse.urlunsplit((parsed.scheme.lower(), netloc, path, urllib.parse.urlencode(query), ""))


def _sanitize_string(value: str) -> str:
    text = str(value or "")
    text = _SIGNED_PATH_RE.sub(r"\g<prefix>[REDACTED]", text)
    text = _HTTP_URL_RE.sub(lambda match: _safe_url(match.group(0)), text)
    if re.fullmatch(r"eyJ[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{8,}){1,2}", text.strip()):
        return "[REDACTED_TOKEN]"
    return text[:20_000]


def redact_privacy_export(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return redact_privacy_export(dataclasses.asdict(value))
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key or "").strip()[:160]
            lowered = key.lower()
            if lowered == "next_cursor":
                result[key] = str(raw_value or "")[:4000]
                continue
            if lowered in _BLOCKED_KEYS or any(
                lowered.endswith(f"_{marker}") for marker in ("secret", "password", "credential", "api_key")
            ):
                result[key] = "[REDACTED]"
                continue
            if isinstance(raw_value, str) and (lowered.endswith("_url") or lowered.endswith("_uri")):
                result[key] = _safe_url(raw_value)
                continue
            result[key] = redact_privacy_export(raw_value)
        return result
    if isinstance(value, (list, tuple, set)):
        return [redact_privacy_export(item) for item in value]
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return redact_privacy_export(vars(value))
    return _sanitize_string(str(value))


def privacy_export_has_secret_markers(payload: object) -> bool:
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str).lower()
    forbidden = (
        "telegram-secret-token",
        "bearer ",
        '"access_token": "eyj',
        '"refresh_token": "',
        '"client_secret": "',
        '"password": "',
    )
    return any(marker in encoded for marker in forbidden)


def _cursor_signature(secret: str, encoded_payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).hexdigest()


def _encode_cursor(*, secret: str, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{encoded}.{_cursor_signature(secret, encoded)}"


def _decode_cursor(*, secret: str, cursor: str) -> dict[str, object]:
    raw_cursor = str(cursor or "").strip()
    try:
        encoded, supplied_signature = raw_cursor.split(".", 1)
    except ValueError as exc:
        raise PrivacyCursorError("privacy_export_cursor_invalid") from exc
    expected_signature = _cursor_signature(secret, encoded)
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise PrivacyCursorError("privacy_export_cursor_invalid")
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except Exception as exc:
        raise PrivacyCursorError("privacy_export_cursor_invalid") from exc
    if not isinstance(payload, dict) or int(payload.get("v") or 0) != 1:
        raise PrivacyCursorError("privacy_export_cursor_invalid")
    return dict(payload)


def _record_time(record: dict[str, object]) -> datetime | None:
    for key in (
        "created_at",
        "updated_at",
        "recorded_at",
        "generated_at",
        "issued_at",
        "revoked_at",
        "published_at",
        "queued_at",
    ):
        parsed = _parse_iso(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _record_id(collection: str, record: dict[str, object], ordinal: int) -> str:
    for key in (
        "run_id",
        "session_id",
        "slug",
        "artifact_id",
        "publication_id",
        "event_id",
        "observation_id",
        "delivery_id",
        "binding_id",
        "request_id",
        "person_id",
        "property_ref",
    ):
        value = str(record.get(key) or "").strip()
        if value:
            return f"{collection}:{value}"[:500]
    material = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return f"{collection}:{ordinal}:{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


def _row(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if hasattr(value, "__dict__"):
        return copy.deepcopy(vars(value))
    return {"value": str(value)}


def _workspace_session_export(row: dict[str, object]) -> dict[str, object]:
    return {
        "session_id": str(row.get("session_id") or "").strip(),
        "email": str(row.get("email") or "").strip(),
        "role": str(row.get("role") or "").strip(),
        "status": str(row.get("status") or "").strip(),
        "source_kind": str(row.get("source_kind") or "").strip(),
        "default_target": str(row.get("default_target") or "").strip(),
        "expires_at": str(row.get("expires_at") or "").strip(),
        "issued_at": str(row.get("issued_at") or "").strip(),
        "revoked_at": str(row.get("revoked_at") or "").strip(),
    }


def _provider_binding_export(binding: object) -> dict[str, object]:
    row = _row(binding)
    auth_metadata = dict(row.get("auth_metadata_json") or {}) if isinstance(row.get("auth_metadata_json"), dict) else {}
    return {
        "binding_id": str(row.get("binding_id") or "").strip(),
        "provider_key": str(row.get("provider_key") or "").strip(),
        "status": str(row.get("status") or "").strip(),
        "priority": int(row.get("priority") or 0),
        "probe_state": str(row.get("probe_state") or "").strip(),
        "scope": dict(row.get("scope_json") or {}) if isinstance(row.get("scope_json"), dict) else {},
        "auth_metadata": {
            key: auth_metadata.get(key)
            for key in ("account_email", "account_id", "token_status", "scope_bundle", "connected_at", "revoked_at")
            if auth_metadata.get(key) not in (None, "")
        },
    }


def _privacy_request_export(record: dict[str, object]) -> dict[str, object]:
    return {
        key: copy.deepcopy(value)
        for key, value in record.items()
        if key not in {"principal_key", "subject_ref_digest", "idempotency_key_hash"}
    }


def _export_collections(
    *,
    container: "AppContainer",
    principal_id: str,
    account_email: str = "",
) -> tuple[dict[str, list[dict[str, object]]], dict[str, object]]:
    product = build_product_service(container)
    packet_service = build_fliplink_packet_service(container)
    status = container.onboarding.status(principal_id=principal_id)
    workspace = dict(status.get("workspace") or {}) if isinstance(status.get("workspace"), dict) else {}
    searches: list[dict[str, object]] = []
    try:
        search_rows = product.list_property_search_runs(
            principal_id=principal_id,
            limit=50_000,
            account_email=str(account_email or "").strip(),
        )
    except TypeError:
        search_rows = product.list_property_search_runs(principal_id=principal_id, limit=50_000)
    searches = [_row(item) for item in search_rows]
    research_packets = [
        _row(item)
        for item in product.export_property_research_packet_data(
            principal_id=principal_id,
            account_email=str(account_email or "").strip(),
        )
    ]
    content_export = PropertyContentJobLedger(
        database_url=_database_url(container),
    ).export_principal_data(
        principal_id=principal_id,
        limit=250,
    )
    property_content_studio = [
        {"record_type": "job", **_row(item)}
        for item in list(content_export.get("jobs") or [])
        if isinstance(item, dict)
    ]
    property_content_studio.extend(
        {"record_type": "job_event", **_row(item)}
        for item in list(content_export.get("job_events") or [])
        if isinstance(item, dict)
    )
    property_content_studio.extend(
        {"record_type": "webhook_event", **_row(item)}
        for item in list(content_export.get("webhook_events") or [])
        if isinstance(item, dict)
    )
    sessions = [
        _workspace_session_export(_row(item))
        for item in product.list_workspace_access_sessions(principal_id=principal_id, status="", limit=50_000)
    ]
    preference_profile: list[dict[str, object]] = []
    try:
        preference_profile = [_row(container.preference_profiles.export_principal(principal_id))]
    except Exception:
        preference_profile = []
    shortlist = [
        _row(item)
        for item in product.list_property_saved_shortlist_candidates(principal_id=principal_id, status=status)
    ]
    tours = [_row(item) for item in list_hosted_property_tours_for_principal(principal_id=principal_id)]
    packet_export = packet_service.export_principal_data(principal_id=principal_id)
    packet_publications = [_row(item) for item in list(packet_export.get("publications") or [])]
    packet_events = [_row(item) for item in list(packet_export.get("events") or [])]
    artifacts = [
        _row(dict(item.get("payload_json") or {}))
        for item in packet_events
        if str(item.get("event_type") or "") == "property_summary_artifact_generated"
        and isinstance(item.get("payload_json"), dict)
    ]
    provider_bindings = [
        _provider_binding_export(item)
        for item in container.provider_registry.list_persisted_binding_records(principal_id=principal_id, limit=50_000)
    ]
    observations = [
        _row(item)
        for item in container.channel_runtime.list_observations_for_principal(principal_id, limit=50_000)
    ]
    delivery_logs = [
        _row(item)
        for item in container.channel_runtime.list_delivery_records(principal_id, limit=50_000)
    ]
    principal_key = privacy_subject_key(principal_id, secret=_privacy_secret(container))
    privacy_requests = [
        _privacy_request_export(item)
        for item in list_privacy_request_records(
            principal_key=principal_key,
            limit=100,
            database_url=_database_url(container),
        )
    ]
    collections = {
        "account_profile": [
            {
                "principal_id": principal_id,
                "workspace": workspace,
                "selected_channels": list(status.get("selected_channels") or []),
                "privacy": dict(status.get("privacy") or {}) if isinstance(status.get("privacy"), dict) else {},
                "delivery_preferences": dict(status.get("delivery_preferences") or {})
                if isinstance(status.get("delivery_preferences"), dict)
                else {},
            }
        ],
        "search_preferences": [
            dict(status.get("property_search_preferences") or {})
            if isinstance(status.get("property_search_preferences"), dict)
            else {}
        ],
        "saved_shortlist": shortlist,
        "preference_profile": preference_profile,
        "searches": searches,
        "research_packets": research_packets,
        "property_content_studio": property_content_studio,
        "workspace_sessions": sessions,
        "tours_and_private_receipts": tours,
        "artifacts": artifacts,
        "packet_publications": packet_publications,
        "packet_events": packet_events,
        "provider_bindings": provider_bindings,
        "delivery_logs": delivery_logs,
        "events": observations,
        "privacy_requests": privacy_requests,
    }
    legacy = {
        "principal_id": principal_id,
        "workspace": workspace,
        "selected_channels": list(status.get("selected_channels") or []),
        "privacy": dict(status.get("privacy") or {}) if isinstance(status.get("privacy"), dict) else {},
        "delivery_preferences": dict(status.get("delivery_preferences") or {})
        if isinstance(status.get("delivery_preferences"), dict)
        else {},
        "property_search_preferences": dict(status.get("property_search_preferences") or {})
        if isinstance(status.get("property_search_preferences"), dict)
        else {},
        "recent_property_search_runs": searches,
        "workspace_access_sessions": sessions,
    }
    return collections, legacy


def build_property_account_export_page(
    *,
    container: "AppContainer",
    principal_id: str,
    account_email: str = "",
    cursor: str = "",
    limit: int = 100,
) -> dict[str, object]:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise ValueError("principal_id_required")
    secret = _privacy_secret(container)
    subject_key = privacy_subject_key(normalized_principal, secret=secret)
    offset = 0
    snapshot_at = _now()
    if str(cursor or "").strip():
        cursor_payload = _decode_cursor(secret=secret, cursor=cursor)
        if not hmac.compare_digest(str(cursor_payload.get("subject") or ""), subject_key):
            raise PrivacyCursorError("privacy_export_cursor_wrong_account")
        offset = max(0, int(cursor_payload.get("offset") or 0))
        parsed_snapshot = _parse_iso(cursor_payload.get("snapshot_at"))
        if parsed_snapshot is None:
            raise PrivacyCursorError("privacy_export_cursor_invalid")
        snapshot_at = parsed_snapshot
    collections, legacy = _export_collections(
        container=container,
        principal_id=normalized_principal,
        account_email=account_email,
    )
    items: list[dict[str, object]] = []
    collection_counts: dict[str, int] = {}
    for collection in _COLLECTION_ORDER:
        source_rows = list(collections.get(collection) or [])
        rendered_rows: list[dict[str, object]] = []
        for ordinal, source_row in enumerate(source_rows):
            sanitized = redact_privacy_export(source_row)
            if not isinstance(sanitized, dict):
                sanitized = {"value": sanitized}
            observed_at = _record_time(sanitized)
            if observed_at is not None and observed_at > snapshot_at:
                continue
            rendered_rows.append(
                {
                    "collection": collection,
                    "record_id": _record_id(collection, sanitized, ordinal),
                    "data": sanitized,
                }
            )
        rendered_rows.sort(key=lambda row: str(row.get("record_id") or ""))
        collection_counts[collection] = len(rendered_rows)
        items.extend(rendered_rows)
    bounded_limit = max(1, min(int(limit or 100), 50_000))
    page_items = items[offset : offset + bounded_limit]
    next_offset = offset + len(page_items)
    next_cursor = ""
    if next_offset < len(items):
        next_cursor = _encode_cursor(
            secret=secret,
            payload={
                "v": 1,
                "subject": subject_key,
                "offset": next_offset,
                "snapshot_at": _iso(snapshot_at),
            },
        )
    export_id = hmac.new(
        secret.encode("utf-8"),
        f"{subject_key}|{_iso(snapshot_at)}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:32]
    bundle: dict[str, object] = {
        "export_type": "propertyquarry_account_data",
        "export_version": "2.0",
        "export_id": f"dsar_{export_id}",
        "generated_at": _iso(snapshot_at),
        "snapshot_at": _iso(snapshot_at),
        "items": page_items,
        "collections": collection_counts,
        "pagination": {
            "offset": offset,
            "limit": bounded_limit,
            "returned": len(page_items),
            "total_records": len(items),
            "next_cursor": next_cursor,
            "complete": not bool(next_cursor),
        },
        "redaction_contract": {
            "secrets_removed": True,
            "signed_link_tokens_removed": True,
            "provider_credentials_removed": True,
            "private_tour_receipts_included_for_owner": True,
            "research_packet_memberships_included_for_owner": True,
            "property_content_source_and_receipt_metadata_included_for_owner": True,
            "raw_property_content_webhook_payloads_removed": True,
        },
        **legacy,
    }
    sanitized_bundle = redact_privacy_export(bundle)
    if not isinstance(sanitized_bundle, dict):
        raise RuntimeError("privacy_export_render_failed")
    if privacy_export_has_secret_markers(sanitized_bundle):
        raise RuntimeError("privacy_export_secret_guard_failed")
    return sanitized_bundle


def _phase(name: str, *, state: str = "pending", detail: str = "") -> dict[str, object]:
    return {"name": name, "state": state, "detail": detail, "updated_at": _iso(_now())}


def _actor_digest(actor: object) -> str:
    normalized = str(actor or "").strip()
    return _digest(normalized) if normalized else ""


def _binding_receipt(binding: object) -> dict[str, object]:
    row = _row(binding)
    return {
        "provider_key": str(row.get("provider_key") or "unknown").strip() or "unknown",
        "binding_ref_digest": _digest(row.get("binding_id")),
        "status": "queued_for_provider_deletion",
        "local_binding_deleted": False,
        "provider_invoked": False,
        "attempt_count": 0,
        "last_attempt_at": "",
        "provider_receipt_ref": "",
        "next_action": "provider_worker_delete_then_attach_receipt",
    }


class PropertyAccountPrivacyLifecycle:
    def __init__(self, container: "AppContainer") -> None:
        self._container = container
        self._database_url = _database_url(container)
        self._secret = _privacy_secret(container)

    def _principal_key(self, principal_id: str) -> str:
        return privacy_subject_key(principal_id, secret=self._secret)

    def _save(self, record: dict[str, object]) -> dict[str, object]:
        record = copy.deepcopy(record)
        record["updated_at"] = _iso(_now())
        return put_privacy_request_record(record, database_url=self._database_url)

    def _load(self, *, principal_id: str, request_id: str) -> dict[str, object] | None:
        return get_privacy_request_record(
            principal_key=self._principal_key(principal_id),
            request_id=request_id,
            database_url=self._database_url,
        )

    @staticmethod
    def public_record(record: dict[str, object] | None) -> dict[str, object]:
        if not record:
            return {
                "status": "not_requested",
                "status_label": "No deletion request",
                "detail": "Your account remains active.",
                "can_confirm": False,
                "can_cancel": False,
                "can_retry": False,
            }
        payload = {
            key: copy.deepcopy(value)
            for key, value in record.items()
            if key not in {"principal_key", "subject_ref_digest", "idempotency_key_hash"}
        }
        status = str(payload.get("status") or "").strip()
        labels = {
            "awaiting_confirmation": ("Waiting for confirmation", "Nothing has been deleted. Confirm or cancel before the confirmation link expires."),
            "processing": ("Removing account data", "Access and public shares are being revoked before stored account data is removed."),
            "completed": ("Deletion complete", "Customer-accessible account data has been removed."),
            "completed_with_provider_followup": (
                "Local deletion complete",
                "PropertyQuarry data is removed. Connected-provider deletions remain queued until provider receipts arrive.",
            ),
            "cancelled": ("Request cancelled", "No account data was deleted by this request."),
            "expired": ("Confirmation expired", "Start a new request if you still want the account removed."),
            "failed": ("Deletion needs attention", "Completed steps stay complete. Retry resumes the remaining local steps without restoring data."),
        }
        label, detail = labels.get(status, (status.replace("_", " ").title(), "Check the request before taking another action."))
        payload.update(
            {
                "status_label": label,
                "detail": detail,
                "can_confirm": status in {"awaiting_confirmation", "failed"},
                "can_cancel": status == "awaiting_confirmation",
                "can_retry": status in {"failed", "completed_with_provider_followup"},
                "recovery_state": (
                    "cancel_available"
                    if status == "awaiting_confirmation"
                    else "retry_available"
                    if status in {"failed", "completed_with_provider_followup"}
                    else "irreversible"
                    if status in {"completed", "processing"}
                    else "closed"
                ),
            }
        )
        sanitized = redact_privacy_export(payload)
        return dict(sanitized) if isinstance(sanitized, dict) else {}

    def latest(self, *, principal_id: str) -> dict[str, object]:
        rows = list_privacy_request_records(
            principal_key=self._principal_key(principal_id),
            limit=1,
            database_url=self._database_url,
        )
        return self.public_record(dict(rows[0])) if rows else self.public_record(None)

    def get(self, *, principal_id: str, request_id: str) -> dict[str, object] | None:
        record = self._load(principal_id=principal_id, request_id=request_id)
        return self.public_record(record) if record else None

    def request_erasure(
        self,
        *,
        principal_id: str,
        idempotency_key: str,
        actor: str = "",
    ) -> dict[str, object]:
        principal_key = self._principal_key(principal_id)
        if not principal_key:
            raise ValueError("principal_id_required")
        normalized_idempotency = privacy_idempotency_key(idempotency_key or "default", secret=self._secret)
        existing = find_privacy_request_by_idempotency(
            principal_key=principal_key,
            idempotency_key_hash=normalized_idempotency,
            database_url=self._database_url,
        )
        if existing:
            return self.public_record(existing)
        now = _now()
        backup_days = max(1, min(int(os.getenv("PROPERTYQUARRY_BACKUP_ERASURE_MAX_DAYS") or "35"), 365))
        record: dict[str, object] = {
            "request_id": f"erase_{uuid4().hex}",
            "principal_key": principal_key,
            "subject_ref_digest": principal_key,
            "idempotency_key_hash": normalized_idempotency,
            "status": "awaiting_confirmation",
            "created_at": _iso(now),
            "updated_at": _iso(now),
            "confirmation_expires_at": _iso(now + timedelta(hours=72)),
            "confirmed_at": "",
            "cancelled_at": "",
            "completed_at": "",
            "requested_by_digest": _actor_digest(actor),
            "phases": [
                _phase("confirmation", state="waiting", detail="Type DELETE to begin irreversible removal."),
                _phase("session_revocation"),
                _phase("searches_shortlists_and_preferences"),
                _phase("property_content_jobs_and_receipts"),
                _phase("tour_revocation_and_cache_purge"),
                _phase("provider_binding_closeout"),
                _phase("artifacts_events_and_delivery_logs"),
                _phase("retention_tombstone"),
            ],
            "provider_deletion_receipts": [],
            "local_deletion_receipts": {},
            "retention_tombstone": {
                "policy": "propertyquarry_account_erasure_v1",
                "customer_data_access_blocked_at": "",
                "backup_delete_by": _iso(now + timedelta(days=backup_days)),
                "backup_restore_action": "reapply_tombstone_before_service_start",
                "contains_raw_account_identifier": False,
                "legal_hold": "none_recorded",
            },
            "last_error_code": "",
        }
        return self.public_record(self._save(record))

    def cancel(self, *, principal_id: str, request_id: str, actor: str = "") -> dict[str, object]:
        record = self._load(principal_id=principal_id, request_id=request_id)
        if record is None:
            raise KeyError("privacy_erasure_request_not_found")
        status = str(record.get("status") or "").strip()
        if status == "cancelled":
            return self.public_record(record)
        if status != "awaiting_confirmation":
            raise PrivacyLifecycleConflict("privacy_erasure_cannot_cancel_after_confirmation")
        record["status"] = "cancelled"
        record["cancelled_at"] = _iso(_now())
        record["cancelled_by_digest"] = _actor_digest(actor)
        phases = list(record.get("phases") or [])
        if phases:
            phases[0] = _phase("confirmation", state="cancelled", detail="The request was cancelled before deletion began.")
        record["phases"] = phases
        return self.public_record(self._save(record))

    def _set_phase(self, record: dict[str, object], name: str, *, state: str, detail: str) -> None:
        phases = [dict(item) for item in list(record.get("phases") or []) if isinstance(item, dict)]
        replacement = _phase(name, state=state, detail=detail)
        for index, item in enumerate(phases):
            if str(item.get("name") or "") == name:
                phases[index] = replacement
                break
        else:
            phases.append(replacement)
        record["phases"] = phases

    def confirm_and_erase(
        self,
        *,
        principal_id: str,
        request_id: str,
        confirmation_phrase: str,
        actor: str = "",
        account_email: str = "",
    ) -> dict[str, object]:
        if not hmac.compare_digest(str(confirmation_phrase or "").strip(), "DELETE"):
            raise PrivacyLifecycleConflict("privacy_erasure_confirmation_phrase_invalid")
        record = self._load(principal_id=principal_id, request_id=request_id)
        if record is None:
            raise KeyError("privacy_erasure_request_not_found")
        status = str(record.get("status") or "").strip()
        if status in {"completed", "completed_with_provider_followup"}:
            return self.public_record(record)
        if status not in {"awaiting_confirmation", "failed"}:
            raise PrivacyLifecycleConflict("privacy_erasure_confirmation_not_available")
        expires_at = _parse_iso(record.get("confirmation_expires_at"))
        if status == "awaiting_confirmation" and expires_at is not None and expires_at <= _now():
            record["status"] = "expired"
            self._set_phase(record, "confirmation", state="expired", detail="The confirmation window expired before deletion began.")
            self._save(record)
            raise PrivacyLifecycleConflict("privacy_erasure_confirmation_expired")
        record["status"] = "processing"
        record["confirmed_at"] = str(record.get("confirmed_at") or _iso(_now()))
        record["confirmed_by_digest"] = _actor_digest(actor)
        record["last_error_code"] = ""
        self._set_phase(record, "confirmation", state="completed", detail="Irreversible deletion was explicitly confirmed.")
        record = self._save(record)
        product = build_product_service(self._container)
        packet_service = build_fliplink_packet_service(self._container)
        receipts = dict(record.get("local_deletion_receipts") or {})
        try:
            if not receipts.get("session_revocation"):
                revoked_sessions: list[str] = []
                for session in product.list_workspace_access_sessions(principal_id=principal_id, status="", limit=50_000):
                    session_row = _row(session)
                    session_id = str(session_row.get("session_id") or "").strip()
                    if not session_id or str(session_row.get("status") or "").strip().lower() == "revoked":
                        continue
                    revoked = product.revoke_workspace_access_session(
                        principal_id=principal_id,
                        session_id=session_id,
                        actor="account_erasure",
                    )
                    if revoked:
                        revoked_sessions.append(session_id)
                receipts["session_revocation"] = {"revoked_count": len(revoked_sessions), "session_refs": [_digest(value) for value in revoked_sessions]}
                record["local_deletion_receipts"] = receipts
                self._set_phase(record, "session_revocation", state="completed", detail=f"Revoked {len(revoked_sessions)} active access sessions.")
                record = self._save(record)

            # Establish the durable account-erasure fence before enumerating and
            # revoking published tours. Publication paths use this authority to
            # reject work that was already in flight when erasure was confirmed.
            # Reassert it on retries even when an earlier phase receipt exists;
            # the storage operation is idempotent and also removes any late rows.
            search_counts = product.erase_property_search_account_data(
                principal_id=principal_id,
                account_email=str(account_email or "").strip(),
            )
            content_counts = PropertyContentJobLedger(
                database_url=self._database_url,
            ).erase_principal_data(
                principal_id=principal_id,
            )
            if not receipts.get("property_content_studio"):
                receipts["property_content_studio"] = {
                    "jobs_deleted": int(content_counts.get("jobs_deleted") or 0),
                    "job_events_deleted": int(
                        content_counts.get("job_events_deleted") or 0
                    ),
                    "webhook_events_deleted": int(
                        content_counts.get("webhook_events_deleted") or 0
                    ),
                    "receipt_files_deleted": int(
                        content_counts.get("receipt_files_deleted") or 0
                    ),
                }
                record["local_deletion_receipts"] = receipts
                self._set_phase(
                    record,
                    "property_content_jobs_and_receipts",
                    state="completed",
                    detail=(
                        "Removed governed content jobs, nonpublic script receipts, "
                        "and redacted provider-event ledger rows."
                    ),
                )
                record = self._save(record)
            if not receipts.get("search_and_preferences"):
                legal_hold_retained = int(
                    search_counts.get("packet_links_legal_hold_retained") or 0
                )
                preference_counts = self._container.preference_profiles.erase_principal(principal_id)
                onboarding_deleted = self._container.onboarding.erase_principal(principal_id)
                receipts["search_and_preferences"] = {
                    "search_runs_deleted": int(search_counts.get("runs_deleted") or 0),
                    "search_work_jobs_deleted": int(
                        search_counts.get("work_jobs_deleted") or 0
                    ),
                    "research_packet_links_deleted": int(search_counts.get("packet_links_deleted") or 0),
                    "research_packet_links_legal_hold_retained": legal_hold_retained,
                    "search_principals_erased": int(search_counts.get("principal_count") or 0),
                    "preference_records_deleted": preference_counts,
                    "onboarding_and_shortlist_deleted": bool(onboarding_deleted),
                }
                record["local_deletion_receipts"] = receipts
                self._set_phase(
                    record,
                    "searches_shortlists_and_preferences",
                    state="completed",
                    detail=(
                        "Removed searches, non-held research packets, saved shortlist state, "
                        "onboarding data, and learned preference records. "
                        + (
                            f"Retained {legal_hold_retained} research packet link(s) "
                            "exclusively as explicit legal-hold evidence."
                            if legal_hold_retained
                            else "No research packet evidence was retained under legal hold."
                        )
                    ),
                )
                record = self._save(record)

            if not receipts.get("tour_revocation"):
                tour_receipts: list[dict[str, object]] = []
                for tour in list_hosted_property_tours_for_principal(principal_id=principal_id):
                    if str(tour.get("status") or "") != "active":
                        continue
                    result = revoke_hosted_property_tour_bundle(
                        slug=str(tour.get("slug") or ""),
                        principal_id=principal_id,
                        actor="account_erasure",
                    )
                    tour_receipts.append(
                        {
                            "slug_digest": _digest(tour.get("slug")),
                            "status": str(result.get("status") or ""),
                            "removed_file_count": int(result.get("removed_file_count") or 0),
                            "cdn_purge": dict(result.get("cdn_purge") or {}),
                        }
                    )
                receipts["tour_revocation"] = {"tour_count": len(tour_receipts), "receipts": tour_receipts}
                record["local_deletion_receipts"] = receipts
                self._set_phase(
                    record,
                    "tour_revocation_and_cache_purge",
                    state="completed",
                    detail=f"Revoked {len(tour_receipts)} public tours and queued cache purges without invoking a CDN.",
                )
                record = self._save(record)

            provider_receipts = [
                dict(item) for item in list(record.get("provider_deletion_receipts") or []) if isinstance(item, dict)
            ]
            if not provider_receipts:
                bindings = list(
                    self._container.provider_registry.list_persisted_binding_records(
                        principal_id=principal_id,
                        limit=50_000,
                    )
                )
                provider_receipts = [_binding_receipt(binding) for binding in bindings]
                for index, binding in enumerate(bindings):
                    row = _row(binding)
                    deleted = self._container.provider_registry.delete_persisted_binding_record(
                        binding_id=str(row.get("binding_id") or ""),
                        principal_id=principal_id,
                    )
                    provider_receipts[index]["local_binding_deleted"] = bool(deleted)
                record["provider_deletion_receipts"] = provider_receipts
                self._set_phase(
                    record,
                    "provider_binding_closeout",
                    state="followup" if provider_receipts else "completed",
                    detail=(
                        f"Removed {len(provider_receipts)} local bindings; provider deletions are queued and no provider was invoked."
                        if provider_receipts
                        else "No connected-provider bindings required deletion."
                    ),
                )
                record = self._save(record)

            if not receipts.get("artifacts_events_delivery"):
                packet_counts = packet_service.erase_principal_data(principal_id=principal_id)
                runtime_counts = self._container.channel_runtime.erase_principal_data(principal_id)
                receipts["artifacts_events_delivery"] = {
                    "packet_and_artifact_records": packet_counts,
                    "events_and_delivery_records": runtime_counts,
                }
                record["local_deletion_receipts"] = receipts
                self._set_phase(
                    record,
                    "artifacts_events_and_delivery_logs",
                    state="completed",
                    detail="Removed PropertyQuarry packet, artifact, event, and delivery records from primary storage.",
                )
                record = self._save(record)

            tombstone = dict(record.get("retention_tombstone") or {})
            tombstone["customer_data_access_blocked_at"] = str(tombstone.get("customer_data_access_blocked_at") or _iso(_now()))
            tombstone["primary_storage_erasure_completed_at"] = _iso(_now())
            tombstone["subject_ref_digest"] = str(record.get("subject_ref_digest") or "")
            tombstone["contains_raw_account_identifier"] = False
            record["retention_tombstone"] = tombstone
            self._set_phase(
                record,
                "retention_tombstone",
                state="completed",
                detail="A digest-only tombstone blocks backup restoration; rolling backups expire by the recorded deadline.",
            )
            record["completed_at"] = _iso(_now())
            record["status"] = "completed_with_provider_followup" if provider_receipts else "completed"
            return self.public_record(self._save(record))
        except Exception as exc:
            record["status"] = "failed"
            record["last_error_code"] = f"local_erasure_{exc.__class__.__name__.lower()}"
            return self.public_record(self._save(record))

    def retry_provider_deletions(
        self,
        *,
        principal_id: str,
        request_id: str,
        actor: str = "",
    ) -> dict[str, object]:
        record = self._load(principal_id=principal_id, request_id=request_id)
        if record is None:
            raise KeyError("privacy_erasure_request_not_found")
        if str(record.get("status") or "") not in {"completed_with_provider_followup", "failed"}:
            raise PrivacyLifecycleConflict("privacy_provider_retry_not_available")
        now = _iso(_now())
        receipts = [dict(item) for item in list(record.get("provider_deletion_receipts") or []) if isinstance(item, dict)]
        for receipt in receipts:
            if str(receipt.get("status") or "") in {"deleted", "not_applicable"}:
                continue
            receipt["status"] = "queued_for_provider_deletion"
            receipt["attempt_count"] = int(receipt.get("attempt_count") or 0) + 1
            receipt["last_attempt_at"] = now
            receipt["provider_invoked"] = False
            receipt["queued_by_digest"] = _actor_digest(actor)
            receipt["next_action"] = "provider_worker_delete_then_attach_receipt"
        record["provider_deletion_receipts"] = receipts
        self._set_phase(
            record,
            "provider_binding_closeout",
            state="followup",
            detail="Provider deletion was re-queued. This request did not invoke any provider.",
        )
        return self.public_record(self._save(record))


def build_property_account_privacy_lifecycle(container: "AppContainer") -> PropertyAccountPrivacyLifecycle:
    return PropertyAccountPrivacyLifecycle(container)
