from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json
import os
import urllib.parse

import requests

_AGENT_PLAN_ALIASES = {
    "agency": "agent",
    "agency_tier": "agent",
    "agency_lifetime": "agent",
    "agency_tier_lifetime": "agent",
    "agent_lifetime": "agent",
    "agent_tier_lifetime": "agent",
}

_LIFETIME_AGENT_ENTITLEMENT_KIND = "lifetime"
_LIFETIME_AGENT_EXPIRY = datetime(2999, 1, 1, tzinfo=timezone.utc)

_PAID_BILLING_SAFE_HANDOFF_CONTRACT = (
    "propertyquarry.paid_billing_safe_handoff.v1"
)
_PAID_BILLING_SAFE_HANDOFF_MAX_AGE = timedelta(hours=24)
_PAID_BILLING_SAFE_HANDOFF_FUTURE_SKEW = timedelta(minutes=5)
_PAYPAL_API_BASES = {
    "https://api-m.paypal.com": "live",
    "https://api-m.sandbox.paypal.com": "sandbox",
}
_PAYPAL_CURRENCY = "EUR"
_PAYPAL_ORDER_BINDING_VERSION = "v1"

_PROPERTY_FURNITURE_STYLE_LIMIT_BY_PLAN = {
    "free": 5,
    "plus": 5,
    "agent": 5,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: object) -> datetime | None:
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


def _hash_public_identifier(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_property_plan_key(plan_key: object) -> str:
    normalized = str(plan_key or "").strip().lower().replace("-", "_").replace(" ", "_")
    return _AGENT_PLAN_ALIASES.get(normalized, normalized or "free")


def property_plan_has_unlimited_provider_results(plan_key: object, max_results_per_source: object) -> bool:
    try:
        result_cap = int(max_results_per_source or 0)
    except Exception:
        result_cap = 0
    return result_cap <= 0


def property_worker_cap(plan_key: object) -> int:
    normalized = normalize_property_plan_key(plan_key)
    return {"free": 1, "plus": 2, "agent": 4}.get(normalized, 1)


def property_furniture_style_cap(plan_key: object) -> int:
    normalized = normalize_property_plan_key(plan_key)
    return _PROPERTY_FURNITURE_STYLE_LIMIT_BY_PLAN.get(
        normalized,
        _PROPERTY_FURNITURE_STYLE_LIMIT_BY_PLAN["free"],
    )


@dataclass(frozen=True)
class PropertyPlanSpec:
    plan_key: str
    display_name: str
    checkout_label: str
    amount_eur: str
    pass_days: int
    max_platforms: int
    max_results_per_source: int
    search_agent_limit: int
    max_match_score: int
    research_depth: str
    investment_research_level: str
    furniture_style_limit: int
    magic_fit_scene_limit: int
    magic_fit_video_limit: int
    magic_fit_scene_period: str
    magic_fit_video_period: str
    auto_tour_policy: str
    features: tuple[str, ...]


_FREE_PLAN = PropertyPlanSpec(
    plan_key="free",
    display_name="Free",
    checkout_label="Free",
    amount_eur="0.00",
    pass_days=0,
    max_platforms=3,
    max_results_per_source=0,
    search_agent_limit=1,
    max_match_score=35,
    research_depth="standard",
    investment_research_level="none",
    furniture_style_limit=property_furniture_style_cap("free"),
    magic_fit_scene_limit=1,
    magic_fit_video_limit=1,
    magic_fit_scene_period="week",
    magic_fit_video_period="day",
    auto_tour_policy="hero_only",
    features=(
        "up to 3 platforms per run",
        "standard research on the shortlisted results",
        "one saved search",
        "one 3D reconstruction floor plan per week and one interior flythrough per day",
    ),
)

_PAID_PLANS = {
    "plus": PropertyPlanSpec(
        plan_key="plus",
        display_name="Plus",
        checkout_label="EUR 3 / 30 days",
        amount_eur="3.00",
        pass_days=30,
        max_platforms=8,
        max_results_per_source=0,
        search_agent_limit=3,
        max_match_score=45,
        research_depth="deep",
        investment_research_level="preview",
        furniture_style_limit=property_furniture_style_cap("plus"),
        magic_fit_scene_limit=5,
        magic_fit_video_limit=3,
        magic_fit_scene_period="day",
        magic_fit_video_period="day",
        auto_tour_policy="shortlist_opt_in",
        features=(
            "up to 8 platforms per run",
            "deep research with preview investment signals",
            "up to 3 saved searches",
            "multiple 3D reconstruction floor plans and interior flythroughs per day (3 max)",
        ),
    ),
    "agent": PropertyPlanSpec(
        plan_key="agent",
        display_name="Agent",
        checkout_label="EUR 99 / 30 days",
        amount_eur="99.00",
        pass_days=30,
        max_platforms=0,
        max_results_per_source=0,
        search_agent_limit=0,
        max_match_score=60,
        research_depth="deep",
        investment_research_level="full",
        furniture_style_limit=property_furniture_style_cap("agent"),
        magic_fit_scene_limit=0,
        magic_fit_video_limit=0,
        magic_fit_scene_period="none",
        magic_fit_video_period="none",
        auto_tour_policy="all_opt_in",
        features=(
            "all Austria listing-site coverage in one search",
            "deep research and follow-up readiness",
            "unlimited saved searches",
            "opt-in 3D reconstruction floor plans and interior flythroughs for every found property",
        ),
    ),
}


def property_plan_catalog() -> tuple[PropertyPlanSpec, ...]:
    return (_FREE_PLAN, *_PAID_PLANS.values())


def property_plan_spec(plan_key: str) -> PropertyPlanSpec:
    normalized = normalize_property_plan_key(plan_key)
    if normalized == "free":
        return _FREE_PLAN
    if normalized in _PAID_PLANS:
        return _PAID_PLANS[normalized]
    raise ValueError("unknown_property_plan")


def normalize_property_commercial(
    value: dict[str, object] | None,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    raw = dict(value or {})
    raw_requested_plan_key = str(raw.get("active_plan_key") or raw.get("plan_key") or "free").strip().lower().replace("-", "_").replace(" ", "_")
    entitlement_kind = str(raw.get("entitlement_kind") or "").strip().lower()
    lifetime_agent = entitlement_kind == _LIFETIME_AGENT_ENTITLEMENT_KIND
    requested_plan_key = normalize_property_plan_key(raw_requested_plan_key)
    if lifetime_agent:
        requested_plan_key = "agent"
    if requested_plan_key not in {"free", *tuple(_PAID_PLANS.keys())}:
        requested_plan_key = "free"
    raw_status = str(raw.get("status") or "").strip().lower()
    active_until = _parse_iso(raw.get("active_until"))
    if lifetime_agent:
        active_until = _LIFETIME_AGENT_EXPIRY
    elif raw_requested_plan_key in _AGENT_PLAN_ALIASES and active_until is None and raw_status in {"", "active", "captured", "lifetime"}:
        active_until = _LIFETIME_AGENT_EXPIRY
    evaluated_at = now or _now()
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=timezone.utc)
    else:
        evaluated_at = evaluated_at.astimezone(timezone.utc)
    expired = (
        not lifetime_agent
        and requested_plan_key != "free"
        and (active_until is None or active_until <= evaluated_at)
    )
    effective_plan_key = "free" if expired else requested_plan_key
    status = str(raw.get("status") or ("expired" if expired else ("active" if effective_plan_key != "free" else "free"))).strip().lower()
    if effective_plan_key == "free" and status not in {"expired", "free", "payment_failed", "cancelled", "refunded"}:
        status = "free"
    if effective_plan_key != "free" and status not in {"active", "pending", "captured"}:
        status = "active"
    normalized = {
        "active_plan_key": effective_plan_key,
        "status": status,
        "active_until": active_until.isoformat() if active_until is not None and effective_plan_key != "free" and not expired else "",
        "last_order_id": str(raw.get("last_order_id") or "").strip(),
        "last_capture_id": str(raw.get("last_capture_id") or "").strip(),
        "last_payment_status": str(raw.get("last_payment_status") or "").strip(),
        "last_payment_amount_eur": str(raw.get("last_payment_amount_eur") or "").strip(),
        "last_payer_email": str(raw.get("last_payer_email") or "").strip(),
        "captured_at": str(raw.get("captured_at") or "").strip(),
        "pending_order_id": str(raw.get("pending_order_id") or "").strip(),
        "pending_plan_key": normalize_property_plan_key(raw.get("pending_plan_key") or "") if str(raw.get("pending_plan_key") or "").strip() else "",
        "pending_approval_url": str(raw.get("pending_approval_url") or "").strip(),
        "plan_source": str(raw.get("plan_source") or "").strip(),
        "last_billing_event_type": str(raw.get("last_billing_event_type") or "").strip(),
        "last_billing_event_id": str(raw.get("last_billing_event_id") or "").strip(),
        "last_billing_event_at": str(raw.get("last_billing_event_at") or "").strip(),
        "billing_events_json": _normalized_property_billing_events(raw.get("billing_events_json")),
        "billing_reconciliations_json": _normalized_property_billing_reconciliations(raw.get("billing_reconciliations_json")),
    }
    if lifetime_agent:
        normalized.update(
            {
                "entitlement_kind": _LIFETIME_AGENT_ENTITLEMENT_KIND,
                "entitlement_plan_key": "agent",
                "entitlement_source": str(raw.get("entitlement_source") or "").strip(),
                "entitlement_grant_id": str(raw.get("entitlement_grant_id") or "").strip(),
                "entitlement_granted_at": str(raw.get("entitlement_granted_at") or "").strip(),
                "entitlement_reason_digest": str(raw.get("entitlement_reason_digest") or "").strip(),
            }
        )
    return normalized


def _normalized_property_billing_events(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    events: list[dict[str, object]] = []
    for item in value[-20:]:
        if not isinstance(item, dict):
            continue
        event = {
            "event_id": str(item.get("event_id") or "").strip()[:160],
            "event_type": str(item.get("event_type") or "").strip().lower()[:120],
            "provider": str(item.get("provider") or "").strip().lower()[:80],
            "plan_key": str(item.get("plan_key") or "").strip().lower()[:40],
            "order_id": str(item.get("order_id") or "").strip()[:160],
            "invoice_id": str(item.get("invoice_id") or "").strip()[:160],
            "invoice_url": str(item.get("invoice_url") or "").strip()[:500],
            "invoice_status": str(item.get("invoice_status") or "").strip().lower()[:80],
            "accounting_status": str(item.get("accounting_status") or "").strip().lower()[:80],
            "payment_status": str(item.get("payment_status") or "").strip().lower()[:80],
            "currency": str(item.get("currency") or "EUR").strip().upper()[:8],
            "amount_eur": str(item.get("amount_eur") or "").strip()[:40],
            "net_amount_eur": str(item.get("net_amount_eur") or "").strip()[:40],
            "vat_amount_eur": str(item.get("vat_amount_eur") or "").strip()[:40],
            "vat_rate": str(item.get("vat_rate") or "").strip()[:40],
            "recorded_at": str(item.get("recorded_at") or "").strip()[:80],
            "local_reconciliation_status": str(item.get("local_reconciliation_status") or "").strip().lower()[:80],
            "local_reconciliation_id": str(item.get("local_reconciliation_id") or "").strip()[:160],
            "local_reconciled_at": str(item.get("local_reconciled_at") or "").strip()[:80],
            "local_reconciled_by": str(item.get("local_reconciled_by") or "").strip()[:80],
        }
        if any(event.values()):
            events.append(event)
    return events


def _normalized_property_billing_reconciliations(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    for item in value[-20:]:
        if not isinstance(item, dict):
            continue
        row = {
            "reconciliation_id": str(item.get("reconciliation_id") or "").strip()[:160],
            "event_id": str(item.get("event_id") or "").strip()[:160],
            "provider": str(item.get("provider") or "").strip().lower()[:80],
            "decision": str(item.get("decision") or "").strip().lower()[:40],
            "status": str(item.get("status") or "").strip().lower()[:80],
            "plan_key": str(item.get("plan_key") or "").strip().lower()[:40],
            "payment_status": str(item.get("payment_status") or "").strip().lower()[:80],
            "reconciled_at": str(item.get("reconciled_at") or "").strip()[:80],
            "reconciled_by": str(item.get("reconciled_by") or "").strip()[:80],
            "note_sha256": str(item.get("note_sha256") or "").strip()[:64],
            "entitlement_mutation": str(item.get("entitlement_mutation") or "").strip().lower()[:80],
        }
        if any(row.values()):
            rows.append(row)
    return rows


def property_billing_event_updates(
    existing_commercial: dict[str, object] | None,
    *,
    provider: str,
    event_type: str,
    event_id: str = "",
    plan_key: str = "",
    order_id: str = "",
    invoice_id: str = "",
    invoice_url: str = "",
    invoice_status: str = "",
    accounting_status: str = "",
    payment_status: str = "",
    currency: str = "EUR",
    amount_eur: str = "",
    net_amount_eur: str = "",
    vat_amount_eur: str = "",
    vat_rate: str = "",
) -> dict[str, object]:
    normalized = normalize_property_commercial(existing_commercial)
    events = list(normalized.get("billing_events_json") or [])
    compact_event_type = str(event_type or "").strip().lower()
    compact_event_id = str(event_id or "").strip()
    if not compact_event_id:
        stable = "|".join(
            [
                str(provider or "").strip().lower(),
                compact_event_type,
                str(plan_key or "").strip().lower(),
                str(order_id or "").strip(),
                str(invoice_id or "").strip(),
                str(invoice_url or "").strip(),
                str(payment_status or "").strip().lower(),
                str(amount_eur or "").strip(),
            ]
        )
        compact_event_id = hashlib.sha256(stable.encode("utf-8")).hexdigest()[:24]
    if any(str(item.get("event_id") or "") == compact_event_id for item in events if isinstance(item, dict)):
        return {
            "last_billing_event_type": compact_event_type,
            "last_billing_event_id": compact_event_id,
            "last_billing_event_at": str(normalized.get("last_billing_event_at") or ""),
            "billing_events_json": events,
        }
    recorded_at = _now_iso()
    events.append(
        {
            "event_id": compact_event_id,
            "event_type": compact_event_type,
            "provider": str(provider or "").strip().lower(),
                "plan_key": str(plan_key or "").strip().lower(),
                "order_id": str(order_id or "").strip(),
                "invoice_id": str(invoice_id or "").strip(),
                "invoice_url": str(invoice_url or "").strip(),
                "invoice_status": str(invoice_status or "").strip().lower(),
                "accounting_status": str(accounting_status or "").strip().lower(),
                "payment_status": str(payment_status or "").strip().lower(),
                "currency": str(currency or "EUR").strip().upper(),
                "amount_eur": str(amount_eur or "").strip(),
                "net_amount_eur": str(net_amount_eur or "").strip(),
                "vat_amount_eur": str(vat_amount_eur or "").strip(),
                "vat_rate": str(vat_rate or "").strip(),
                "recorded_at": recorded_at,
            }
        )
    events = _normalized_property_billing_events(events)
    return {
        "last_billing_event_type": compact_event_type,
        "last_billing_event_id": compact_event_id,
        "last_billing_event_at": recorded_at,
        "billing_events_json": events,
    }


def property_billing_invoice_handoffs(property_commercial: dict[str, object] | None) -> list[dict[str, object]]:
    commercial = normalize_property_commercial(property_commercial)
    rows: list[dict[str, object]] = []
    for event in list(commercial.get("billing_events_json") or []):
        if not isinstance(event, dict):
            continue
        invoice_id = str(event.get("invoice_id") or "").strip()
        accounting_status = str(event.get("accounting_status") or "").strip().lower()
        invoice_status = str(event.get("invoice_status") or "").strip().lower()
        payment_status = str(event.get("payment_status") or "").strip().lower()
        if not invoice_id and not accounting_status and not invoice_status:
            continue
        if payment_status in {"refunded", "refund", "payment.refunded"}:
            state = "refunded"
        elif payment_status in {"failed", "payment.failed"}:
            state = "payment_failed"
        elif invoice_status in {"issued", "sent", "paid"}:
            state = invoice_status
        elif invoice_id:
            state = "pending_document"
        else:
            state = accounting_status or "pending_document"
        rows.append(
            {
                "event_id": str(event.get("event_id") or "").strip(),
                "provider": str(event.get("provider") or "").strip().lower(),
                "plan_key": str(event.get("plan_key") or "").strip().lower(),
                "order_id": str(event.get("order_id") or "").strip(),
                "invoice_id": invoice_id,
                "invoice_url": str(event.get("invoice_url") or "").strip(),
                "state": state,
                "accounting_status": accounting_status,
                "invoice_status": invoice_status,
                "payment_status": payment_status,
                "currency": str(event.get("currency") or "EUR").strip().upper(),
                "amount_eur": str(event.get("amount_eur") or "").strip(),
                "net_amount_eur": str(event.get("net_amount_eur") or "").strip(),
                "vat_amount_eur": str(event.get("vat_amount_eur") or "").strip(),
                "vat_rate": str(event.get("vat_rate") or "").strip(),
                "recorded_at": str(event.get("recorded_at") or "").strip(),
            }
        )
    return rows[-10:]


def reconcile_brilliant_directories_billing_event(
    existing_commercial: dict[str, object] | None,
    *,
    event_id: str,
    decision: str,
    reconciled_by: str,
    note: str = "",
    now: datetime | None = None,
) -> dict[str, object]:
    normalized = normalize_property_commercial(existing_commercial, now=now)
    compact_event_id = str(event_id or "").strip()
    compact_decision = str(decision or "").strip().lower()
    if compact_decision not in {"approve", "reject"}:
        raise ValueError("brilliant_directories_reconciliation_decision_invalid")
    if not compact_event_id:
        raise ValueError("brilliant_directories_reconciliation_event_id_required")
    events = [dict(item) for item in list(normalized.get("billing_events_json") or []) if isinstance(item, dict)]
    event_index = next(
        (
            index
            for index, item in enumerate(events)
            if str(item.get("event_id") or "").strip() == compact_event_id
            and str(item.get("provider") or "").strip().lower() == "brilliant_directories"
        ),
        -1,
    )
    if event_index < 0:
        raise ValueError("brilliant_directories_reconciliation_event_not_found")
    event = dict(events[event_index])
    previous_reconciliations = [
        dict(item)
        for item in list(normalized.get("billing_reconciliations_json") or [])
        if isinstance(item, dict)
    ]
    if any(str(item.get("event_id") or "").strip() == compact_event_id for item in previous_reconciliations):
        raise ValueError("brilliant_directories_reconciliation_event_already_reconciled")

    reconciled_at = (now or _now()).isoformat()
    operator_hash = _hash_public_identifier(reconciled_by)
    note_hash = hashlib.sha256(str(note or "").strip().encode("utf-8")).hexdigest() if str(note or "").strip() else ""
    plan_key = str(event.get("plan_key") or "").strip().lower()
    payment_status = str(event.get("payment_status") or "").strip().lower()
    payable_statuses = {"paid", "captured", "complete", "completed", "succeeded", "success", "active"}
    if compact_decision == "approve":
        try:
            plan = property_plan_spec(plan_key)
        except ValueError as exc:
            raise ValueError("brilliant_directories_reconciliation_plan_invalid") from exc
        if plan.plan_key == "free":
            raise ValueError("brilliant_directories_reconciliation_plan_invalid")
        if payment_status and payment_status not in payable_statuses:
            raise ValueError("brilliant_directories_reconciliation_payment_not_paid")
        status = "approved_local_entitlement"
        entitlement_mutation = "activated"
    else:
        plan = property_plan_spec("free")
        status = "rejected_no_entitlement_change"
        entitlement_mutation = "none"

    reconciliation_id = hashlib.sha256(
        "|".join(
            [
                "brilliant_directories",
                compact_event_id,
                compact_decision,
                operator_hash,
                reconciled_at,
            ]
        ).encode("utf-8")
    ).hexdigest()[:24]
    reconciliation = {
        "reconciliation_id": reconciliation_id,
        "event_id": compact_event_id,
        "provider": "brilliant_directories",
        "decision": compact_decision,
        "status": status,
        "plan_key": plan_key,
        "payment_status": payment_status,
        "reconciled_at": reconciled_at,
        "reconciled_by": operator_hash,
        "note_sha256": note_hash,
        "entitlement_mutation": entitlement_mutation,
    }
    event.update(
        {
            "accounting_status": "local_reconciled" if compact_decision == "approve" else "local_rejected",
            "local_reconciliation_status": status,
            "local_reconciliation_id": reconciliation_id,
            "local_reconciled_at": reconciled_at,
            "local_reconciled_by": operator_hash,
        }
    )
    events[event_index] = event
    updates: dict[str, object] = {
        "billing_events_json": _normalized_property_billing_events(events),
        "billing_reconciliations_json": _normalized_property_billing_reconciliations(
            [*previous_reconciliations, reconciliation]
        ),
    }
    if compact_decision == "approve":
        updates.update(
            {
                "active_plan_key": plan.plan_key,
                "status": "active",
                "active_until": paid_plan_expiry(plan_key=plan.plan_key, captured_at=now or _now()),
                "last_order_id": str(event.get("order_id") or ""),
                "last_capture_id": str(event.get("invoice_id") or ""),
                "last_payment_status": payment_status or "paid",
                "last_payment_amount_eur": str(event.get("amount_eur") or plan.amount_eur),
                "captured_at": reconciled_at,
                "pending_order_id": "",
                "pending_plan_key": "",
                "pending_approval_url": "",
                "plan_source": "brilliant_directories_local_reconciliation",
            }
        )
    return {
        "provider": "brilliant_directories",
        "event_id": compact_event_id,
        "decision": compact_decision,
        "status": status,
        "advisory_event_required": True,
        "local_reconciliation_required": False,
        "entitlement_mutation": entitlement_mutation,
        "current_plan_key": plan.plan_key if compact_decision == "approve" else str(normalized.get("active_plan_key") or "free"),
        "reconciliation": reconciliation,
        "updates": updates,
    }


def brilliant_directories_billing_webhook_secret() -> str:
    return str(
        os.getenv("PROPERTYQUARRY_BRILLIANT_DIRECTORIES_WEBHOOK_SECRET")
        or os.getenv("BRILLIANT_DIRECTORIES_WEBHOOK_SECRET")
        or ""
    ).strip()


def _brilliant_directories_timestamp_seconds(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        parsed = _parse_iso(text)
        if parsed is None:
            return None
        return int(parsed.timestamp())


def verify_brilliant_directories_billing_webhook_signature(
    *,
    body_bytes: bytes,
    signature: str,
    timestamp: object,
    now: datetime | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    secret = brilliant_directories_billing_webhook_secret()
    provided = str(signature or "").strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1].strip()
    signed_at = _brilliant_directories_timestamp_seconds(timestamp)
    if not secret or not provided or signed_at is None:
        return False
    current = int((now or _now()).timestamp())
    if abs(current - signed_at) > max(int(tolerance_seconds), 0):
        return False
    expected = hmac.new(
        secret.encode("utf-8"),
        f"{signed_at}.".encode("utf-8") + body_bytes,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


def _brilliant_directories_payload_value(payload: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def brilliant_directories_billing_webhook_receipt(
    existing_commercial: dict[str, object] | None,
    *,
    payload: dict[str, object],
    body_bytes: bytes,
    signature: str,
    timestamp: object,
    now: datetime | None = None,
) -> dict[str, object]:
    normalized = normalize_property_commercial(existing_commercial)
    event_id = _brilliant_directories_payload_value(payload, "event_id", "id", "uuid", "webhook_id")
    event_type = _brilliant_directories_payload_value(payload, "event_type", "type", "action") or "billing.event"
    plan_key = _brilliant_directories_payload_value(payload, "plan_key", "plan", "membership_level")
    order_id = _brilliant_directories_payload_value(payload, "order_id", "transaction_id", "subscription_id")
    invoice_id = _brilliant_directories_payload_value(payload, "invoice_id", "receipt_id")
    invoice_url = _brilliant_directories_payload_value(payload, "invoice_url", "receipt_url")
    invoice_status = _brilliant_directories_payload_value(payload, "invoice_status", "receipt_status")
    payment_status = _brilliant_directories_payload_value(payload, "payment_status", "status")
    amount_eur = _brilliant_directories_payload_value(payload, "amount_eur", "amount")
    currency = _brilliant_directories_payload_value(payload, "currency") or "EUR"
    signature_ok = verify_brilliant_directories_billing_webhook_signature(
        body_bytes=body_bytes,
        signature=signature,
        timestamp=timestamp,
        now=now,
    )
    event_updates = property_billing_event_updates(
        normalized,
        provider="brilliant_directories",
        event_type=event_type,
        event_id=event_id,
        plan_key=plan_key,
        order_id=order_id,
        invoice_id=invoice_id,
        invoice_url=invoice_url,
        invoice_status=invoice_status,
        accounting_status="external_advisory",
        payment_status=payment_status,
        currency=currency,
        amount_eur=amount_eur,
    )
    compact_event_id = str(event_updates.get("last_billing_event_id") or "").strip()
    replayed = any(
        str(item.get("event_id") or "") == compact_event_id
        for item in list(normalized.get("billing_events_json") or [])
        if isinstance(item, dict)
    )
    receipt_payload = {
        "provider": "brilliant_directories",
        "event_id": compact_event_id,
        "event_type": str(event_updates.get("last_billing_event_type") or "").strip(),
        "signature_verified": signature_ok,
        "replayed": replayed,
        "advisory_only": True,
        "entitlement_mutation_allowed": False,
        "local_reconciliation_required": True,
        "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "payload_keys": sorted(str(key) for key in payload.keys())[:40],
        "billing_event_updates": event_updates if signature_ok and not replayed else {},
        "status": (
            "accepted_advisory_receipt"
            if signature_ok and not replayed
            else ("replayed" if replayed else "signature_invalid")
        ),
        "recorded_at": (now or _now()).isoformat(),
    }
    # Keep private customer/payment data out of receipts while making replay and reconciliation auditable.
    return json.loads(json.dumps(receipt_payload))


def property_commercial_snapshot(property_preferences: dict[str, object] | None) -> dict[str, object]:
    preferences = dict(property_preferences or {})
    commercial = normalize_property_commercial(dict(preferences.get("property_commercial") or {}))
    current_plan = property_plan_spec(str(commercial.get("active_plan_key") or "free"))
    pending_plan_key = str(commercial.get("pending_plan_key") or "").strip().lower()
    pending_plan = _PAID_PLANS.get(pending_plan_key)
    return {
        "current_plan_key": current_plan.plan_key,
        "current_plan_label": current_plan.display_name,
        "status": str(commercial.get("status") or "free"),
        "active_until": str(commercial.get("active_until") or ""),
        "is_paid": current_plan.plan_key != "free",
        "research_depth": current_plan.research_depth,
        "investment_research_level": current_plan.investment_research_level,
        "max_platforms": current_plan.max_platforms,
        "max_results_per_source": current_plan.max_results_per_source,
        "search_agent_limit": current_plan.search_agent_limit,
        "max_match_score": current_plan.max_match_score,
        "furniture_style_limit": current_plan.furniture_style_limit,
        "magic_fit_scene_limit": current_plan.magic_fit_scene_limit,
        "magic_fit_video_limit": current_plan.magic_fit_video_limit,
        "magic_fit_scene_period": current_plan.magic_fit_scene_period,
        "magic_fit_video_period": current_plan.magic_fit_video_period,
        "auto_tour_policy": current_plan.auto_tour_policy,
        "pending_plan_key": pending_plan.plan_key if pending_plan is not None else "",
        "pending_plan_label": pending_plan.display_name if pending_plan is not None else "",
        "pending_approval_url": str(commercial.get("pending_approval_url") or ""),
        "invoice_handoffs": property_billing_invoice_handoffs(commercial),
        "plan_catalog": [
            {
                "plan_key": spec.plan_key,
                "display_name": spec.display_name,
                "checkout_label": spec.checkout_label,
                "amount_eur": spec.amount_eur,
                "pass_days": spec.pass_days,
                "max_platforms": spec.max_platforms,
                "max_results_per_source": spec.max_results_per_source,
                "search_agent_limit": spec.search_agent_limit,
                "max_match_score": spec.max_match_score,
                "furniture_style_limit": spec.furniture_style_limit,
                "research_depth": spec.research_depth,
                "investment_research_level": spec.investment_research_level,
                "magic_fit_scene_limit": spec.magic_fit_scene_limit,
                "magic_fit_video_limit": spec.magic_fit_video_limit,
                "magic_fit_scene_period": spec.magic_fit_scene_period,
                "magic_fit_video_period": spec.magic_fit_video_period,
                "auto_tour_policy": spec.auto_tour_policy,
                "features": list(spec.features),
                "is_current": spec.plan_key == current_plan.plan_key,
            }
            for spec in property_plan_catalog()
        ],
        "property_commercial": commercial,
    }


def merge_property_commercial(
    property_preferences: dict[str, object] | None,
    *,
    updates: dict[str, object],
) -> dict[str, object]:
    merged = dict(property_preferences or {})
    current = normalize_property_commercial(dict(merged.get("property_commercial") or {}))
    current.update(dict(updates or {}))
    merged["property_commercial"] = normalize_property_commercial(current)
    return merged


def enforce_property_plan_limits(
    *,
    property_preferences: dict[str, object] | None,
    selected_platforms: tuple[str, ...],
    max_results_per_source: int | None,
) -> None:
    snapshot = property_commercial_snapshot(property_preferences)
    current_plan = property_plan_spec(str(snapshot.get("current_plan_key") or "free"))
    platform_count = len([value for value in selected_platforms if str(value or "").strip()])
    if int(current_plan.max_platforms) > 0 and platform_count > current_plan.max_platforms:
        target = "plus" if current_plan.plan_key == "free" else "agent"
        raise RuntimeError(f"property_plan_upgrade_required:{target}")
    if int(current_plan.max_results_per_source) > 0 and max_results_per_source is not None and int(max_results_per_source) > int(current_plan.max_results_per_source):
        target = "plus" if current_plan.plan_key == "free" else "agent"
        raise RuntimeError(f"property_plan_upgrade_required:{target}")


def _lower_hex(value: object, *, length: int) -> str:
    normalized = str(value or "").strip()
    if len(normalized) != length or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        return ""
    return normalized


def _sha256_digest(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized.startswith("sha256:"):
        return ""
    digest = _lower_hex(normalized.removeprefix("sha256:"), length=64)
    return f"sha256:{digest}" if digest else ""


def paid_billing_safe_handoff_configured(
    *,
    provider: str,
    plan_key: str = "",
) -> bool:
    """Admit customer checkout only for a fresh, exact-release canary handoff.

    Provider credentials are necessary but deliberately insufficient.  The
    external release controller must inject the non-secret canary bindings
    after proving same-principal checkout, signed/idempotent webhook handling,
    entitlement grant, and cancellation for both paid plans.
    """

    expected_provider = str(provider or "").strip().lower()
    if expected_provider not in {"paypal", "payfunnels"}:
        return False
    if (
        str(
            os.getenv("PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_CONTRACT")
            or ""
        ).strip()
        != _PAID_BILLING_SAFE_HANDOFF_CONTRACT
    ):
        return False
    admitted_provider = str(
        os.getenv("PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PROVIDER") or ""
    ).strip().lower()
    if not hmac.compare_digest(admitted_provider, expected_provider):
        return False
    admitted_environment = str(
        os.getenv(
            "PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PROVIDER_ENVIRONMENT"
        )
        or ""
    ).strip().lower()
    # A sandbox canary is useful evidence, but it must never unlock a
    # customer-facing production checkout lane.
    if not hmac.compare_digest(admitted_environment, "live"):
        return False
    admitted_plans = {
        normalize_property_plan_key(value)
        for value in str(
            os.getenv("PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PLAN_KEYS")
            or ""
        ).split(",")
        if str(value).strip()
    }
    if admitted_plans != set(_PAID_PLANS):
        return False
    normalized_plan = (
        normalize_property_plan_key(plan_key)
        if str(plan_key or "").strip()
        else ""
    )
    if normalized_plan and normalized_plan not in admitted_plans:
        return False

    runtime_commit = _lower_hex(
        os.getenv("PROPERTYQUARRY_RELEASE_COMMIT_SHA"),
        length=40,
    )
    admitted_commit = _lower_hex(
        os.getenv("PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_RELEASE_COMMIT_SHA"),
        length=40,
    )
    runtime_image = _sha256_digest(
        os.getenv("PROPERTYQUARRY_RELEASE_IMAGE_DIGEST")
    )
    admitted_image = _sha256_digest(
        os.getenv("PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_RELEASE_IMAGE_DIGEST")
    )
    receipt_digest = _sha256_digest(
        os.getenv("PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_RECEIPT_SHA256")
    )
    principal_digest = _sha256_digest(
        os.getenv("PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_PRINCIPAL_SHA256")
    )
    if not all(
        (
            runtime_commit,
            admitted_commit,
            runtime_image,
            admitted_image,
            receipt_digest,
            principal_digest,
        )
    ):
        return False
    if not hmac.compare_digest(runtime_commit, admitted_commit):
        return False
    if not hmac.compare_digest(runtime_image, admitted_image):
        return False

    verified_at = _parse_iso(
        os.getenv("PROPERTYQUARRY_PAID_BILLING_SAFE_HANDOFF_VERIFIED_AT")
    )
    if verified_at is None:
        return False
    age = _now() - verified_at
    return (
        age >= -_PAID_BILLING_SAFE_HANDOFF_FUTURE_SKEW
        and age <= _PAID_BILLING_SAFE_HANDOFF_MAX_AGE
    )


def paypal_configured() -> bool:
    enabled = str(os.getenv("PROPERTYQUARRY_ENABLE_PAYPAL_CHECKOUT") or "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return False
    return bool(
        str(os.getenv("PAYPAL_CLIENT_ID") or "").strip()
        and str(os.getenv("PAYPAL_SECRET") or "").strip()
        and paypal_api_environment() == "live"
        and paid_billing_safe_handoff_configured(provider="paypal")
    )


def _payfunnels_checkout_env_name(plan_key: str) -> str:
    normalized = str(plan_key or "").strip().upper()
    return f"PAYFUNNELS_{normalized}_CHECKOUT_URL"


def payfunnels_api_key() -> str:
    return str(os.getenv("PAYFUNNELS_API_KEY") or "").strip()


def _payfunnels_https_url(value: str, *, setting_name: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise RuntimeError(f"{setting_name}_must_be_https")
    return normalized


def _payfunnels_api_base() -> str:
    return _payfunnels_https_url(
        str(os.getenv("PAYFUNNELS_API_BASE") or "https://api.payfunnels.com"),
        setting_name="payfunnels_api_base",
    )


def payfunnels_checkout_url(*, plan_key: str) -> str:
    return str(os.getenv(_payfunnels_checkout_env_name(plan_key)) or "").strip()


def payfunnels_configured(*, plan_key: str = "") -> bool:
    webhook_secret = str(os.getenv("PAYFUNNELS_WEBHOOK_SECRET") or "").strip()
    if not webhook_secret or not paid_billing_safe_handoff_configured(
        provider="payfunnels",
        plan_key=plan_key,
    ):
        return False
    if str(plan_key or "").strip():
        return bool(payfunnels_checkout_url(plan_key=plan_key) or payfunnels_api_key())
    return bool(payfunnels_api_key() or any(payfunnels_checkout_url(plan_key=candidate) for candidate in _PAID_PLANS))


def _payfunnels_checkout_title(*, principal_id: str, plan_key: str, checkout_ref: str) -> str:
    spec = property_plan_spec(plan_key)
    compact_principal = urllib.parse.quote(str(principal_id or "").strip(), safe="")
    compact_ref = urllib.parse.quote(str(checkout_ref or "").strip(), safe="")
    return f"PropertyQuarry {spec.display_name} | pq_principal:{compact_principal} | pq_order:{compact_ref}"


def _payfunnels_create_payment_link(
    *,
    principal_id: str,
    plan_key: str,
    checkout_ref: str,
    return_url: str,
    cancel_url: str,
) -> dict[str, object]:
    spec = property_plan_spec(plan_key)
    api_key = payfunnels_api_key()
    if not api_key:
        raise RuntimeError("payfunnels_api_key_missing")
    endpoint = "/v1/paymentlinks/recurring" if spec.pass_days >= 30 else "/v1/paymentlinks/onetime"
    title = _payfunnels_checkout_title(principal_id=principal_id, plan_key=spec.plan_key, checkout_ref=checkout_ref)
    description = (
        f"PropertyQuarry {spec.display_name} billing for principal {principal_id}. "
        f"Return URL: {return_url} Cancel URL: {cancel_url}"
    )
    payload: dict[str, object] = {
        "title": title,
        "description": description,
        "currencyCode": "EUR",
        "amount": float(spec.amount_eur),
        "isTaxable": False,
        "forwardProcessingFees": False,
        "displayBillingAddress": False,
        "displayShippingAddress": False,
        "enableTermOfService": True,
        "additionalFields": [
            {
                "label": "pq_principal",
                "type": "Textfield",
                "isRequired": False,
                "displayOnReceipt": False,
                "isHidden": True,
                "hiddenFieldValue": str(principal_id or "").strip(),
            },
            {
                "label": "pq_order",
                "type": "Textfield",
                "isRequired": False,
                "displayOnReceipt": False,
                "isHidden": True,
                "hiddenFieldValue": str(checkout_ref or "").strip(),
            },
            {
                "label": "pq_plan",
                "type": "Textfield",
                "isRequired": False,
                "displayOnReceipt": False,
                "isHidden": True,
                "hiddenFieldValue": spec.plan_key,
            },
        ],
    }
    if endpoint.endswith("/recurring"):
        payload["interval"] = "month"
    response = requests.post(
        f"{_payfunnels_api_base()}{endpoint}",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-pf-api-key": api_key,
        },
        json=payload,
        timeout=30,
        allow_redirects=False,
    )
    if 300 <= response.status_code < 400:
        raise RuntimeError(f"payfunnels_payment_link_redirect_blocked:{response.status_code}")
    if response.status_code >= 400:
        detail = response.text[:1200]
        raise RuntimeError(f"payfunnels_payment_link_create_failed:{response.status_code}:{detail}")
    body = response.json()
    approve_url = str(body.get("url") or body.get("checkoutUrl") or "").strip()
    provider_id = str(body.get("id") or body.get("paymentLinkId") or checkout_ref).strip()
    if not approve_url:
        raise RuntimeError("payfunnels_payment_link_missing_url")
    return {
        "order_id": checkout_ref,
        "provider_link_id": provider_id,
        "approve_url": approve_url,
        "status": "redirect",
        "plan_key": spec.plan_key,
        "amount_eur": spec.amount_eur,
    }


def create_payfunnels_property_checkout(
    *,
    principal_id: str,
    plan_key: str,
    return_url: str,
    cancel_url: str,
) -> dict[str, object]:
    spec = property_plan_spec(plan_key)
    if spec.plan_key == "free":
        raise RuntimeError("property_plan_free_does_not_require_checkout")
    if not str(os.getenv("PAYFUNNELS_WEBHOOK_SECRET") or "").strip():
        raise RuntimeError("payfunnels_webhook_not_configured")
    checkout_ref = f"pf-{spec.plan_key}-{hashlib.sha256(f'{principal_id}:{_now_iso()}'.encode('utf-8')).hexdigest()[:20]}"
    if payfunnels_api_key():
        return _payfunnels_create_payment_link(
            principal_id=principal_id,
            plan_key=spec.plan_key,
            checkout_ref=checkout_ref,
            return_url=return_url,
            cancel_url=cancel_url,
        )
    checkout_base = payfunnels_checkout_url(plan_key=spec.plan_key)
    if not checkout_base:
        raise RuntimeError("payfunnels_checkout_not_configured")
    checkout_base = _payfunnels_https_url(checkout_base, setting_name="payfunnels_checkout_url")
    parsed = urllib.parse.urlparse(checkout_base)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(
        [
            ("client_reference_id", principal_id),
            ("external_id", checkout_ref),
            ("plan_key", spec.plan_key),
            ("success_url", return_url),
            ("cancel_url", cancel_url),
        ]
    )
    approve_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
    return {
        "order_id": checkout_ref,
        "approve_url": approve_url,
        "status": "redirect",
        "plan_key": spec.plan_key,
        "amount_eur": spec.amount_eur,
    }


def verify_payfunnels_webhook_signature(*, body_bytes: bytes, signature: str) -> bool:
    secret = str(os.getenv("PAYFUNNELS_WEBHOOK_SECRET") or "").strip()
    provided = str(signature or "").strip()
    if provided.lower().startswith("sha256="):
        provided = provided.split("=", 1)[1].strip()
    if not secret or not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(provided, expected)


def _paypal_api_base() -> str:
    normalized = str(
        os.getenv("PAYPAL_API_BASE") or "https://api-m.paypal.com"
    ).strip().rstrip("/")
    if normalized not in _PAYPAL_API_BASES:
        raise RuntimeError("paypal_api_base_invalid")
    return normalized


def paypal_api_environment() -> str:
    try:
        return _PAYPAL_API_BASES[_paypal_api_base()]
    except (KeyError, RuntimeError):
        return ""


def _paypal_auth_header() -> str:
    client_id = str(os.getenv("PAYPAL_CLIENT_ID") or "").strip()
    secret = str(os.getenv("PAYPAL_SECRET") or "").strip()
    if not client_id or not secret:
        raise RuntimeError("paypal_not_configured")
    token = base64.b64encode(f"{client_id}:{secret}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _paypal_access_token() -> str:
    response = requests.post(
        f"{_paypal_api_base()}/v1/oauth2/token",
        headers={
            "Authorization": _paypal_auth_header(),
            "Accept": "application/json",
            "Accept-Language": "en_US",
        },
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"paypal_access_token_failed:{response.status_code}")
    payload = response.json()
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("paypal_access_token_missing")
    return token


def _paypal_principal_binding(*, principal_id: str, plan_key: str) -> str:
    normalized_principal = str(principal_id or "").strip()
    if not normalized_principal:
        raise RuntimeError("paypal_principal_required")
    spec = property_plan_spec(plan_key)
    if spec.plan_key == "free":
        raise RuntimeError("property_plan_free_does_not_require_checkout")
    principal_sha256 = hashlib.sha256(normalized_principal.encode("utf-8")).hexdigest()
    return ":".join(
        (
            "propertyquarry",
            _PAYPAL_ORDER_BINDING_VERSION,
            spec.plan_key,
            principal_sha256,
        )
    )


def _paypal_order_request_id(*, operation: str, order_id: str) -> str:
    normalized_operation = str(operation or "").strip().lower()
    normalized_order_id = str(order_id or "").strip()
    digest = hashlib.sha256(
        f"propertyquarry:{normalized_operation}:{normalized_order_id}".encode("utf-8")
    ).hexdigest()[:32]
    return f"propertyquarry-{normalized_operation}-{digest}"


def _paypal_validate_order_contract(
    body: object,
    *,
    order_id: str,
    principal_id: str,
    plan_key: str,
    require_completed_capture: bool,
) -> dict[str, object]:
    if not isinstance(body, dict):
        raise RuntimeError("paypal_order_contract_invalid")
    normalized_order_id = str(order_id or "").strip()
    provider_order_id = str(body.get("id") or "").strip()
    if not provider_order_id or not hmac.compare_digest(provider_order_id, normalized_order_id):
        raise RuntimeError("paypal_order_id_mismatch")

    spec = property_plan_spec(plan_key)
    purchase_units = list(body.get("purchase_units") or [])
    if len(purchase_units) != 1 or not isinstance(purchase_units[0], dict):
        raise RuntimeError("paypal_order_contract_invalid")
    purchase_unit = dict(purchase_units[0])
    expected_reference = f"propertyquarry-{spec.plan_key}"
    reference_id = str(purchase_unit.get("reference_id") or "").strip()
    if not hmac.compare_digest(reference_id, expected_reference):
        raise RuntimeError("paypal_order_plan_mismatch")
    expected_binding = _paypal_principal_binding(
        principal_id=principal_id,
        plan_key=spec.plan_key,
    )
    custom_id = str(purchase_unit.get("custom_id") or "").strip()
    if not hmac.compare_digest(custom_id, expected_binding):
        raise RuntimeError("paypal_order_principal_mismatch")
    amount = dict(purchase_unit.get("amount") or {})
    if (
        str(amount.get("currency_code") or "").strip().upper() != _PAYPAL_CURRENCY
        or str(amount.get("value") or "").strip() != spec.amount_eur
    ):
        raise RuntimeError("paypal_order_amount_mismatch")

    order_status = str(body.get("status") or "").strip().lower()
    if not require_completed_capture:
        return {
            "order_id": provider_order_id,
            "order_status": order_status,
            "plan_key": spec.plan_key,
            "amount_eur": spec.amount_eur,
            "currency": _PAYPAL_CURRENCY,
        }
    if order_status != "completed":
        raise RuntimeError("paypal_order_capture_not_completed")

    payments = dict(purchase_unit.get("payments") or {})
    captures = list(payments.get("captures") or [])
    if len(captures) != 1 or not isinstance(captures[0], dict):
        raise RuntimeError("paypal_order_capture_invalid")
    capture = dict(captures[0])
    capture_id = str(capture.get("id") or "").strip()
    capture_status = str(capture.get("status") or "").strip().lower()
    capture_amount = dict(capture.get("amount") or {})
    if not capture_id or capture_status != "completed":
        raise RuntimeError("paypal_order_capture_not_completed")
    if (
        str(capture_amount.get("currency_code") or "").strip().upper() != _PAYPAL_CURRENCY
        or str(capture_amount.get("value") or "").strip() != spec.amount_eur
    ):
        raise RuntimeError("paypal_order_capture_amount_mismatch")
    captured_at = _parse_iso(capture.get("create_time") or body.get("update_time")) or _now()
    payer_email = str(dict(body.get("payer") or {}).get("email_address") or "").strip()
    return {
        "order_id": provider_order_id,
        "capture_id": capture_id,
        "payment_status": capture_status,
        "payer_email": payer_email,
        "amount_eur": spec.amount_eur,
        "currency": _PAYPAL_CURRENCY,
        "plan_key": spec.plan_key,
        "captured_at": captured_at.isoformat(),
        "active_until": paid_plan_expiry(
            plan_key=spec.plan_key,
            captured_at=captured_at,
        ),
    }


def create_paypal_property_order(
    *,
    principal_id: str,
    plan_key: str,
    return_url: str,
    cancel_url: str,
) -> dict[str, object]:
    spec = property_plan_spec(plan_key)
    if spec.plan_key == "free":
        raise RuntimeError("property_plan_free_does_not_require_checkout")
    principal_binding = _paypal_principal_binding(
        principal_id=principal_id,
        plan_key=spec.plan_key,
    )
    token = _paypal_access_token()
    payload = {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "reference_id": f"propertyquarry-{spec.plan_key}",
                "description": f"PropertyQuarry {spec.display_name} 30-day pass",
                "custom_id": principal_binding,
                "amount": {
                    "currency_code": _PAYPAL_CURRENCY,
                    "value": spec.amount_eur,
                },
            }
        ],
        "application_context": {
            "brand_name": "PropertyQuarry",
            "user_action": "PAY_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
    }
    response = requests.post(
        f"{_paypal_api_base()}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"paypal_order_create_failed:{response.status_code}")
    body = response.json()
    approve_url = ""
    for link in list(body.get("links") or []):
        if str(link.get("rel") or "").strip().lower() == "approve":
            approve_url = str(link.get("href") or "").strip()
            break
    order_id = str(body.get("id") or "").strip()
    if not order_id or not approve_url:
        raise RuntimeError("paypal_order_create_invalid")
    return {
        "order_id": order_id,
        "approve_url": approve_url,
        "status": str(body.get("status") or "").strip().lower(),
        "plan_key": spec.plan_key,
        "amount_eur": spec.amount_eur,
    }


def capture_paypal_property_order(
    *,
    order_id: str,
    principal_id: str,
    plan_key: str,
) -> dict[str, object]:
    normalized_order_id = str(order_id or "").strip()
    if not normalized_order_id:
        raise RuntimeError("paypal_order_id_required")
    spec = property_plan_spec(plan_key)
    if spec.plan_key == "free":
        raise RuntimeError("property_plan_free_does_not_require_checkout")
    token = _paypal_access_token()
    order_response = requests.get(
        f"{_paypal_api_base()}/v2/checkout/orders/{normalized_order_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=30,
    )
    if order_response.status_code >= 400:
        raise RuntimeError(f"paypal_order_read_failed:{order_response.status_code}")
    order_body = order_response.json()
    preflight = _paypal_validate_order_contract(
        order_body,
        order_id=normalized_order_id,
        principal_id=principal_id,
        plan_key=spec.plan_key,
        require_completed_capture=False,
    )
    order_status = str(preflight.get("order_status") or "").strip().lower()
    if order_status == "completed":
        replayed = _paypal_validate_order_contract(
            order_body,
            order_id=normalized_order_id,
            principal_id=principal_id,
            plan_key=spec.plan_key,
            require_completed_capture=True,
        )
        replayed["replayed"] = True
        return replayed
    if order_status != "approved":
        raise RuntimeError("paypal_order_not_approved")
    response = requests.post(
        f"{_paypal_api_base()}/v2/checkout/orders/{normalized_order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "PayPal-Request-Id": _paypal_order_request_id(
                operation="capture",
                order_id=normalized_order_id,
            ),
        },
        json={},
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"paypal_order_capture_failed:{response.status_code}")
    captured = _paypal_validate_order_contract(
        response.json(),
        order_id=normalized_order_id,
        principal_id=principal_id,
        plan_key=spec.plan_key,
        require_completed_capture=True,
    )
    captured["replayed"] = False
    return captured


def paid_plan_expiry(*, plan_key: str, captured_at: datetime | None = None) -> str:
    spec = property_plan_spec(plan_key)
    if spec.plan_key == "free":
        return ""
    base = captured_at or _now()
    return (base + timedelta(days=spec.pass_days)).isoformat()
