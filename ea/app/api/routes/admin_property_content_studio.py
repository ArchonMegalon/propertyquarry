from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.api.dependencies import (
    RequestContext,
    get_request_context,
    is_operator_context,
    resolve_principal_id,
)
from app.domain.property.content_source_packet import sha256_json
from app.observability import get_runtime_metrics
from app.services.property_content_job_ledger import (
    PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID,
    PropertyContentLedgerCorruptionError,
)
from app.services.property_content_packet_builder import (
    build_product_tutorial_source_packet,
    build_synthetic_dossier_source_packet,
)
from app.services.property_content_studio import PropertyContentStudio
from app.services.property_content_validation import validate_property_content_source_packet


authenticated_router = APIRouter(tags=["property-content-studio"])
public_router = APIRouter(prefix="/internal/providers/subscribr", tags=["subscribr"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
_SUBSCRIBR_SCRIPT_COMPLETION_EVENTS = frozenset({"script.generated", "export.completed"})


class ContentPacketValidateIn(BaseModel):
    packet: dict[str, object] = Field(default_factory=dict)


class TutorialPacketIn(BaseModel):
    title: str = Field(default="How to Read a PropertyQuarry Dossier", min_length=3, max_length=240)
    language: str = Field(default="en", max_length=40)
    jurisdiction: str = Field(default="GLOBAL", max_length=40)


class ScriptReceiptIn(BaseModel):
    packet: dict[str, object] = Field(default_factory=dict)
    markdown: str = Field(default="", max_length=80_000)
    provider_channel_id: str = Field(default="", max_length=120)
    provider_idea_id: str = Field(default="", max_length=120)
    provider_script_id: str = Field(default="", max_length=120)


def _studio() -> PropertyContentStudio:
    return PropertyContentStudio()


def _signature_header(request: Request) -> str:
    return str(
        request.headers.get("x-subscribr-signature")
        or request.headers.get("x-subcribr-signature")
        or request.headers.get("subscribr-signature")
        or ""
    ).strip()


def _event_id(payload: dict[str, object]) -> str:
    return str(
        payload.get("id")
        or payload.get("event_id")
        or payload.get("eventId")
        or payload.get("webhook_event_id")
        or payload.get("webhookEventId")
        or ""
    ).strip()


def _event_type(payload: dict[str, object]) -> str:
    return str(payload.get("type") or payload.get("event") or payload.get("event_type") or "").strip()


def _content_webhook_lease_seconds() -> int:
    try:
        value = int(str(os.getenv("PROPERTYQUARRY_CONTENT_WEBHOOK_LEASE_SECONDS") or "300").strip() or "300")
    except (TypeError, ValueError):
        value = 300
    return max(30, min(value, 1800))


def _packet_run_id(packet: dict[str, object]) -> str:
    snapshot = packet.get("property_snapshot")
    return str(
        dict(snapshot).get("run_id") if isinstance(snapshot, dict) else ""
    ).strip()


def _provider_reference(payload: dict[str, object], *keys: str) -> str:
    for key in keys:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _packet_operation_ownership(
    *,
    studio: PropertyContentStudio,
    packet: dict[str, object],
    context: RequestContext,
) -> dict[str, str]:
    principal_id = resolve_principal_id(None, context)
    packet_id = str(packet.get("packet_id") or "").strip()
    if is_operator_context(context) and packet_id:
        system_job = studio.ledger.get_job(
            packet_id,
            principal_id=PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID,
            ownership_scope="system",
            search_run_id="",
        )
        if (
            system_job
            and str(system_job.get("source_packet_canonical_sha256") or "")
            == sha256_json(packet)
        ):
            return {
                "principal_id": PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID,
                "ownership_scope": "system",
                "search_run_id": "",
            }
    search_run_id = _packet_run_id(packet)
    if not search_run_id:
        raise HTTPException(
            status_code=422,
            detail="property_content_search_run_id_required",
        )
    return {
        "principal_id": principal_id,
        "ownership_scope": "search_run",
        "search_run_id": search_run_id,
    }


def _verify_subscribr_signature(*, raw_body: bytes, signature: str, timestamp: str = "") -> None:
    secret = str(os.getenv("SUBSCRIBR_PROPERTY_WEBHOOK_SECRET") or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="subscribr_webhook_secret_not_configured")
    if timestamp:
        try:
            delta = abs(time.time() - float(timestamp))
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="subscribr_webhook_timestamp_invalid") from exc
        if delta > 600:
            raise HTTPException(status_code=401, detail="subscribr_webhook_timestamp_stale")
    expected_hex = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    normalized = signature
    if normalized.startswith("sha256="):
        normalized = normalized.split("=", 1)[1]
    if not normalized or not hmac.compare_digest(normalized, expected_hex):
        raise HTTPException(status_code=401, detail="subscribr_webhook_signature_invalid")


@authenticated_router.get("/admin/property/content-studio", response_class=HTMLResponse)
def property_content_studio_home(
    request: Request,
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    principal_id = resolve_principal_id(None, context)
    snapshot_principal_id = (
        PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID
        if is_operator_context(context)
        else principal_id
    )
    return templates.TemplateResponse(
        request,
        "admin/property_content_studio.html",
        {
            "request": request,
            "studio": _studio().studio_snapshot(principal_id=snapshot_principal_id),
        },
    )


@authenticated_router.get("/admin/property/content-studio/jobs/{packet_id}", response_class=HTMLResponse)
def property_content_studio_job(
    request: Request,
    packet_id: str,
    search_run_id: str = "",
    ownership_scope: str = "search_run",
    context: RequestContext = Depends(get_request_context),
) -> HTMLResponse:
    normalized_scope = str(ownership_scope or "").strip().lower()
    if normalized_scope == "system":
        if not is_operator_context(context):
            raise HTTPException(status_code=403, detail="operator_scope_required")
        owner = PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID
        normalized_run_id = ""
    else:
        owner = resolve_principal_id(None, context)
        normalized_scope = "search_run"
        normalized_run_id = str(search_run_id or "").strip()
        if not normalized_run_id:
            raise HTTPException(
                status_code=422,
                detail="property_content_search_run_id_required",
            )
    job = _studio().ledger.get_job(
        packet_id,
        principal_id=owner,
        ownership_scope=normalized_scope,
        search_run_id=normalized_run_id,
    )
    if not job:
        raise HTTPException(status_code=404, detail="property_content_job_not_found")
    return templates.TemplateResponse(request, "admin/property_content_job.html", {"request": request, "job": job})


@authenticated_router.post("/app/api/property/content/source-packets/validate")
def validate_property_content_packet(body: ContentPacketValidateIn) -> dict[str, object]:
    return validate_property_content_source_packet(body.packet)


@authenticated_router.post("/app/api/property/content/source-packets/product-tutorial")
def create_product_tutorial_packet(
    body: TutorialPacketIn,
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    if not is_operator_context(context):
        raise HTTPException(status_code=403, detail="operator_scope_required")
    packet = build_product_tutorial_source_packet(title=body.title, language=body.language, jurisdiction=body.jurisdiction)
    job = _studio().prepare_source_packet(
        packet,
        principal_id=PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID,
        ownership_scope="system",
        search_run_id="",
    )
    return {"packet": packet, "job": job, "validation": validate_property_content_source_packet(packet)}


@authenticated_router.post("/app/api/property/content/source-packets/synthetic-dossier")
def create_synthetic_dossier_packet(
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    if not is_operator_context(context):
        raise HTTPException(status_code=403, detail="operator_scope_required")
    packet = build_synthetic_dossier_source_packet()
    job = _studio().prepare_source_packet(
        packet,
        principal_id=PROPERTY_CONTENT_SYSTEM_PRINCIPAL_ID,
        ownership_scope="system",
        search_run_id="",
    )
    return {"packet": packet, "job": job, "validation": validate_property_content_source_packet(packet)}


@authenticated_router.post("/app/api/property/content/subscribr/request-script")
def request_subscribr_script(
    body: ContentPacketValidateIn,
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    studio = _studio()
    ownership = _packet_operation_ownership(
        studio=studio,
        packet=body.packet,
        context=context,
    )
    job = studio.request_subscribr_script(body.packet, **ownership)
    return {"job": job}


@authenticated_router.post("/app/api/property/content/subscribr/script-receipt")
def materialize_subscribr_script_receipt(
    body: ScriptReceiptIn,
    context: RequestContext = Depends(get_request_context),
) -> dict[str, object]:
    studio = _studio()
    ownership = _packet_operation_ownership(
        studio=studio,
        packet=body.packet,
        context=context,
    )
    return studio.materialize_script_receipt(
        packet=body.packet,
        markdown=body.markdown,
        **ownership,
        provider_channel_id=body.provider_channel_id,
        provider_idea_id=body.provider_idea_id,
        provider_script_id=body.provider_script_id,
    )


@public_router.post("/webhook")
async def subscribr_webhook(request: Request) -> dict[str, object]:
    raw_body = await request.body()
    if len(raw_body) > 256_000:
        raise HTTPException(status_code=413, detail="subscribr_webhook_payload_too_large")
    _verify_subscribr_signature(
        raw_body=raw_body,
        signature=_signature_header(request),
        timestamp=str(request.headers.get("x-subscribr-timestamp") or ""),
    )
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="subscribr_webhook_invalid_json") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="subscribr_webhook_object_required")
    event_id = _event_id(payload)
    if not event_id:
        raise HTTPException(status_code=422, detail="subscribr_webhook_event_id_required")
    event_type = _event_type(payload)
    studio = _studio()
    metrics = get_runtime_metrics(request.app)
    packet_id = _provider_reference(payload, "packet_id", "packetId")
    inline_packet = payload.get("packet") if isinstance(payload.get("packet"), dict) else {}
    if not packet_id and inline_packet:
        packet_id = str(inline_packet.get("packet_id") or "").strip()
    provider_idea_id = _provider_reference(payload, "idea_id", "ideaId")
    provider_script_id = _provider_reference(payload, "script_id", "scriptId")
    try:
        resolution = studio.ledger.resolve_webhook_job(
            packet_id=packet_id,
            provider_idea_id=provider_idea_id,
            provider_script_id=provider_script_id,
        )
    except PropertyContentLedgerCorruptionError as exc:
        metrics.record_content_ledger_event(outcome="corruption")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    resolution_status = str(resolution.get("status") or "")
    if resolution_status != "resolved":
        metrics.record_content_ledger_event(outcome=f"webhook_{resolution_status or 'unresolved'}")
        raise HTTPException(
            status_code=409 if resolution_status == "ambiguous" else 404,
            detail=f"subscribr_webhook_job_{resolution_status or 'unresolved'}",
        )
    trusted_job = dict(resolution.get("job") or {})
    ownership = {
        "principal_id": str(trusted_job.get("principal_id") or ""),
        "ownership_scope": str(trusted_job.get("ownership_scope") or ""),
        "search_run_id": str(trusted_job.get("search_run_id") or ""),
    }
    trusted_packet_id = str(trusted_job.get("packet_id") or "").strip()
    claim_owner = f"subscribr-webhook:{os.getpid()}:{uuid4().hex}"
    try:
        claim = studio.ledger.claim_webhook_event(
            event_id=event_id,
            payload=payload,
            packet_id=trusted_packet_id,
            **ownership,
            claim_owner=claim_owner,
            lease_seconds=_content_webhook_lease_seconds(),
            extra={
                "raw_body_sha256": hashlib.sha256(raw_body).hexdigest(),
                "signature_status": "verified",
                "timestamp": str(request.headers.get("x-subscribr-timestamp") or "").strip(),
            },
        )
    except PropertyContentLedgerCorruptionError as exc:
        metrics.record_content_ledger_event(outcome="corruption")
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception:
        metrics.record_content_ledger_event(outcome="failed")
        raise
    if bool(claim.get("conflict")):
        metrics.record_content_ledger_event(outcome="replay_conflict")
        raise HTTPException(status_code=409, detail="subscribr_webhook_event_payload_conflict")
    if not bool(claim.get("claimed")):
        metrics.record_content_ledger_event(outcome="duplicate")
        return {"status": "duplicate_ignored", "event_id": event_id}
    metrics.record_content_ledger_event(outcome="recovered" if bool(claim.get("recovered")) else "claimed")

    def finish(response: dict[str, object], *, status: str) -> dict[str, object]:
        studio.ledger.complete_webhook_event(
            event_id=event_id,
            **ownership,
            claim_owner=claim_owner,
            status=status,
            extra={"response_status": str(response.get("status") or status)},
        )
        metrics.record_content_ledger_event(outcome="completed")
        return response

    try:
        if event_type and event_type not in _SUBSCRIBR_SCRIPT_COMPLETION_EVENTS:
            return finish(
                {
                    "status": "received",
                    "event_id": event_id,
                    "event_type": event_type,
                    "next": "wait_for_script_generated_or_export_completed",
                    "publication_allowed": False,
                },
                status="received",
            )
        packet = dict(trusted_job.get("source_packet_json") or {})
        markdown = str(payload.get("markdown") or payload.get("script_markdown") or payload.get("export_markdown") or "")
        if packet and markdown:
            receipt = studio.ingest_completed_script(
                packet=packet,
                event_payload=payload,
                markdown=markdown,
                **ownership,
            )
            return finish(
                {"status": "review_required", "event_id": event_id, "receipt": receipt},
                status="review_required",
            )
        if markdown and not packet:
            return finish(
                {
                    "status": "received",
                    "event_id": event_id,
                    "next": "await_local_source_packet",
                    "publication_allowed": False,
                },
                status="received",
            )
        return finish(
            {
                "status": "received",
                "event_id": event_id,
                "next": "export_script_and_validate",
                "publication_allowed": False,
            },
            status="received",
        )
    except Exception as exc:
        try:
            studio.ledger.fail_webhook_event(
                event_id=event_id,
                **ownership,
                claim_owner=claim_owner,
                error=f"webhook_processing_failed:{exc.__class__.__name__}",
            )
        except Exception:
            pass
        metrics.record_content_ledger_event(outcome="failed")
        raise
